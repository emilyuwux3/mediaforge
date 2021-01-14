import asyncio
import glob
import logging
import os
import random
import string
import subprocess
import sys
import discord.ext
from PIL import Image
from winmagic import magic
from multiprocessing import Pool
import captionfunctions
import humanize

options = {
    "enable-local-file-access": None,
    "format": "png",
    "transparent": None,
    "width": 1,
    "quiet": None
}


def filetostring(f):
    with open(f, 'r') as file:
        data = file.read()
    return data


def get_random_string(length):
    return ''.join(random.choice(string.ascii_letters) for i in range(length))


def temp_file(extension="png"):
    while True:
        name = f"temp/{get_random_string(8)}.{extension}"
        if not os.path.exists(name):
            return name


# https://fredrikaverpil.github.io/2017/06/20/async-and-await-with-subprocesses/
async def run_command(*args):
    # Create subprocess
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    # Status
    logging.info(f"Started: {args}, pid={process.pid}", )

    # Wait for the subprocess to finish
    stdout, stderr = await process.communicate()

    # Progress
    if process.returncode == 0:
        logging.info(
            f"Done: {args}, pid={process.pid}, result: {stdout.decode().strip()}",
        )
    else:
        logging.error(
            f"Failed: {args}, pid={process.pid}, result: {stderr.decode().strip()}",
        )
    result = stdout.decode().strip() + stderr.decode().strip()
    # Result

    # Return stdout
    return result


# https://askubuntu.com/questions/110264/how-to-find-frames-per-second-of-any-video-file
def get_frame_rate(filename):
    logging.info("[improcessing] Getting FPS...")
    if not os.path.exists(filename):
        logging.error("ERROR: filename %r was not found!" % (filename,))
        return -1
    out = subprocess.check_output(
        ["ffprobe", filename, "-v", "0", "-select_streams", "v", "-print_format", "flat", "-show_entries",
         "stream=r_frame_rate"])
    rate = out.split(b'=')[1].strip()[1:-1].split(b'/')  # had to change to byte for some reason lol!
    if len(rate) == 1:
        return float(rate[0])
    if len(rate) == 2:
        return float(rate[0]) / float(rate[1])
    return -1


async def ffmpegsplit(image):
    logging.info("[improcessing] Splitting frames...")
    await run_command("ffmpeg", "-i", image, "-vsync", "1", "-vf", "scale='max(200,iw)':-1",
                      f"{image.split('.')[0]}%09d.png")
    files = glob.glob(f"{image.split('.')[0]}*.png")

    return files, f"{image.split('.')[0]}%09d.png"


async def splitaudio(video):  # TODO: change this to ffprobe to avoid console spam from error
    ifaudio = await run_command("ffprobe", "-i", video, "-show_streams", "-select_streams", "a", "-loglevel", "error")
    if ifaudio:
        logging.info("[improcessing] Splitting audio...")
        name = temp_file("aac")
        result = await run_command("ffmpeg", "-hide_banner", "-i", video, "-vn", "-acodec", "copy", name)
        return name
    else:
        logging.info("[improcessing] No audio detected.")
        return False


async def compresspng(png):
    outname = temp_file("png")
    await run_command("pngquant", "--quality=0-80", "--o", outname, png)
    os.remove(png)
    return outname


async def assurefilesize(image: str, ctx: discord.ext.commands.Context):
    for i in range(5):
        size = os.path.getsize(image)
        logging.info(f"Resulting file is {humanize.naturalsize(size)}")
        # https://www.reddit.com/r/discordapp/comments/aflp3p/the_truth_about_discord_file_upload_limits/
        if size >= 8388119:
            logging.info("Image too big!")
            msg = await ctx.send(f"⚠ Resulting file too big! ({humanize.naturalsize(size)}) Downsizing result...")
            await ctx.trigger_typing()
            image = await handleanimated(image, "", captionfunctions.halfsize)
            await msg.delete()
        if os.path.getsize(image) < 8388119:
            return image
    await ctx.send(f"⚠ Max downsizes reached. File is way too big.")
    return False


def minimagesize(image, minsize):
    im = Image.open(image)
    if im.size[0] < minsize:
        logging.info(f"[improcessing] Image is {im.size}, Upscaling image...")
        im = im.resize((minsize, round(im.size[1] * (minsize / im.size[0]))), Image.BICUBIC)
        name = temp_file("png")
        im.save(name)
        return name
    else:
        return image


async def imagetype(image):
    mime = magic.from_file(image, mime=True)
    if "video" in mime:
        return "VIDEO"
    elif "image" in mime:
        with Image.open(image) as im:
            anim = getattr(im, "is_animated", False)
        if anim:
            return "GIF"  # gifs dont have to be animated but if they aren't its easier to treat them like pngs
        else:
            return "IMAGE"
    return None


async def handleanimated(image: str, caption, capfunction):
    try:
        with Image.open(image) as im:
            anim = getattr(im, "is_animated", False)
    except IOError:  # either invalid file or a valid video
        # https://stackoverflow.com/a/56526114/9044183
        mime = magic.Magic(mime=True)
        filename = mime.from_file(image)
        if filename.find('video') != -1:
            logging.info("[improcessing] Video detected.")
            frames, name = await ffmpegsplit(image)
            audio = await splitaudio(image)
            fps = get_frame_rate(image)
            logging.info(f"[improcessing] Processing {len(frames)} frames...")
            capargs = []
            for i, frame in enumerate(frames):
                capargs.append((frame, caption, frame.replace('.png', '_rendered.png')))
            pool = Pool(32)
            pool.starmap_async(capfunction, capargs)
            pool.close()
            pool.join()
            logging.info(f"[improcessing] Joining {len(frames)} frames...")
            outname = temp_file("mp4")
            if audio:
                await run_command("ffmpeg", "-r", str(fps), "-start_number", "1", "-i",
                                  name.replace('.png', '_rendered.png'),
                                  "-i", audio, "-c:a", "aac", "-shortest",
                                  "-c:v", "libx264", "-crf", "25", "-pix_fmt", "yuv420p",
                                  "-vf", "crop=trunc(iw/2)*2:trunc(ih/2)*2", outname)
                os.remove(audio)
            else:
                await run_command("ffmpeg", "-r", str(fps), "-start_number", "1", "-i",
                                  name.replace('.png', '_rendered.png'),
                                  "-c:v", "libx264", "-crf", "25", "-pix_fmt", "yuv420p",
                                  "-vf", "crop=trunc(iw/2)*2:trunc(ih/2)*2", outname)
            # cleanup
            logging.info("[improcessing] Cleaning files...")
            for f in glob.glob(name.replace('%09d', '*')):
                os.remove(f)

            return outname
        else:
            raise Exception("File given is not valid image or video.")
    else:
        if anim:  # gif
            logging.info("[improcessing] GIF detected.")
            frames, name = await ffmpegsplit(image)
            fps = get_frame_rate(image)
            logging.info(f"[improcessing] Processing {len(frames)} frames...")
            capargs = []
            for i, frame in enumerate(frames):
                capargs.append((frame, caption, frame.replace('.png', '_rendered.png')))
            pool = Pool(32)
            pool.starmap(capfunction, capargs)
            pool.close()
            pool.join()
            logging.info(f"[improcessing] Joining {len(frames)} frames...")
            outname = temp_file("gif")
            await run_command(
                "gifski", "--quiet", "-o", outname, "--fps", str(fps), "--width", "1000",
                name.replace('.png', '_rendered.png').replace('%09d', '*'))
            # logging.info("[improcessing] Cleaning files...")
            # for f in glob.glob(name.replace('%09d', '*')):
            #     os.remove(f)
            return outname
        else:  # normal image
            return await compresspng(capfunction(minimagesize(image, 200), caption))


async def mp4togif(mp4):
    mime = magic.Magic(mime=True)
    filename = mime.from_file(mp4)
    if filename.find('video') == -1:
        return False
    frames, name = await ffmpegsplit(mp4)
    fps = get_frame_rate(mp4)
    outname = temp_file("gif")
    await run_command("gifski", "--quiet", "-o", outname, "--fps", str(fps), name.replace('%09d', '*'))
    logging.info("[improcessing] Cleaning files...")
    for f in glob.glob(name.replace('%09d', '*')):
        os.remove(f)
    return outname


async def giftomp4(gif):
    outname = temp_file("mp4")
    await run_command("ffmpeg", "-i", gif, "-movflags", "faststart", "-pix_fmt", "yuv420p", "-vf",
                      "scale=trunc(iw/2)*2:trunc(ih/2)*2", outname)

    return outname


async def mediatopng(media):
    outname = temp_file("png")
    await run_command("ffmpeg", "-i", media, "-frames:v", "1", outname)

    return outname


async def ffprobe(file):
    return [await run_command("ffprobe", "-hide_banner", file), magic.from_file(file, mime=False),
            magic.from_file(file, mime=True)]
