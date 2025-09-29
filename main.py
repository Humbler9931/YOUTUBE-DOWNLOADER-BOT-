"""
main.py

Fixed version of the user's bot code:
- Separates FastAPI app (web_app) and Pyrogram Client (bot).
- Starts the bot in background when FastAPI starts (so uvicorn can serve).
- Handles YouTube/Instagram URLs and song-name -> mp3 search/download.
- Cleans up downloaded files after sending.
- Uses asyncio.to_thread for blocking operations (yt-dlp, search).
- Ready to run with: uvicorn main:web_app --host 0.0.0.0 --port 10000
"""

import os
import re
import logging
import asyncio
import time
from dotenv import load_dotenv

from fastapi import FastAPI
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import MessageNotModified
from pyrogram.errors.exceptions.flood_420 import FloodWait

from yt_dlp import YoutubeDL
from youtubesearchpython import VideosSearch

# --- Load .env ---
load_dotenv()

# --- Config ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DOWNLOAD_DIR = "downloads"
YTDL_TIMEOUT = 1800  # seconds

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# --- Pyrogram client (rename from `app` to `bot`) ---
bot = Client(
    "ytdl_downloader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    # You can add other params if needed
)

# --- FastAPI web app (exported as `web_app` for uvicorn) ---
web_app = FastAPI()

@web_app.get("/")
async def root():
    return {"status": "✅ Bot is running!"}

# --- Helpers ---

URL_REGEX = r'(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|instagram\.com/(?:reel|p|tv)/|tiktok\.com/)'

def humanbytes(size: int) -> str:
    if not size:
        return "0 B"
    power = 2**10
    n = 0
    power_labels = {0: '', 1: 'Ki', 2: 'Mi', 3: 'Gi', 4: 'Ti'}
    while size > power and n < 4:
        size /= power
        n += 1
    return f"{round(size,2)} {power_labels[n]}B"

async def progress_for_pyrogram(current, total, ud_message, start_time):
    """
    Progress callback for uploads. `ud_message` is the message being edited for status.
    """
    try:
        now = int(time.time())
        if (now - start_time) < 1:
            return

        if total == 0:
            percentage = 0
        else:
            percentage = current * 100 / total
        # Build simple progress bar
        done_blocks = int(percentage // 10)
        progress_bar = '▓' * done_blocks + '░' * (10 - done_blocks)

        elapsed = max(1, now - start_time)
        speed = current / elapsed

        text = (
            f"**Uploading:** `[{round(percentage)}%]`\n"
            f"**Progress:** `[ {progress_bar} ]`\n"
            f"**Size:** `{humanbytes(current)} / {humanbytes(total)}`\n"
            f"**Speed:** `{humanbytes(int(speed))}/s`"
        )
        try:
            await ud_message.edit_text(text)
        except MessageNotModified:
            pass

    except FloodWait as e:
        await asyncio.sleep(e.value)
    except Exception:
        # Don't crash on progress errors
        logger.exception("Progress callback error")

# --- Core download/send logic ---

async def download_and_send(client: Client, message: Message, url: str, is_audio: bool = False):
    status_msg = await message.reply_text("⏳ **Processing request...**")
    # Build ytdl options dynamically
    ydl_opts = {
        "format": "bestaudio/best" if is_audio else "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title)s-%(id)s.%(ext)s"),
        "nocheckcertificate": True,
        "ignoreerrors": True,
        "no_warnings": True,
        "socket_timeout": YTDL_TIMEOUT,
        "quiet": True,
    }

    if is_audio:
        # Use ffmpeg postprocessor for audio extraction
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    temp_file = None
    info_dict = None

    try:
        await status_msg.edit_text("📥 **Downloading content... Please wait.**")

        # run blocking yt-dlp in thread
        def run_extract():
            with YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=True)

        info_dict = await asyncio.to_thread(run_extract)

        # Prepare filename (yt-dlp's prepare_filename returns the *downloaded* path before postprocessing)
        if info_dict is None:
            raise RuntimeError("yt-dlp returned no info.")

        # Try to get filename - prefer ydl's prepare_filename method by re-instantiating minimal YDL
        def prepare_name():
            with YoutubeDL({}) as ydl:
                return ydl.prepare_filename(info_dict)
        temp_file = await asyncio.to_thread(prepare_name)

        # If audio extraction happened, change extension to .mp3
        if is_audio:
            temp_file = os.path.splitext(temp_file)[0] + ".mp3"

        # If file doesn't exist (postprocessors may have changed name), attempt common alternatives
        if not os.path.exists(temp_file):
            # try common alternatives
            base = os.path.splitext(temp_file)[0]
            candidates = [base + ext for ext in [".mp3", ".m4a", ".mp4", ".webm", ".mkv"]]
            found = None
            for c in candidates:
                if os.path.exists(c):
                    found = c
                    break
            if found:
                temp_file = found
            else:
                raise FileNotFoundError(f"Downloaded file not found. Tried {temp_file} and candidates.")

        title = info_dict.get("title", os.path.basename(temp_file))
        await status_msg.edit_text(f"⬆️ **Uploading file:** `{title}`")

        start_time = time.time()

        # Upload
        if is_audio:
            await client.send_audio(
                chat_id=message.chat.id,
                audio=temp_file,
                caption=f"🎵 Downloaded by @{(await client.get_me()).username if (await client.get_me()) else ''}",
                file_name=os.path.basename(temp_file),
                progress=progress_for_pyrogram,
                progress_args=(status_msg, start_time)
            )
        else:
            await client.send_video(
                chat_id=message.chat.id,
                video=temp_file,
                caption=f"🎬 Downloaded by @{(await client.get_me()).username if (await client.get_me()) else ''}",
                file_name=os.path.basename(temp_file),
                progress=progress_for_pyrogram,
                progress_args=(status_msg, start_time)
            )

        await status_msg.delete()
        await message.reply_text("✅ **Download and upload successful!**")

    except Exception as e:
        logger.exception("Error in download_and_send")
        try:
            await status_msg.edit_text(f"❌ **An error occurred!**\n\n`{e}`")
        except Exception:
            pass
    finally:
        # cleanup
        try:
            if temp_file and os.path.exists(temp_file):
                logger.info(f"Deleting file: {temp_file}")
                os.remove(temp_file)
        except Exception:
            logger.exception("Error deleting file")

# --- Bot Handlers ---

@bot.on_message(filters.command("start") & filters.private)
async def start_command(client: Client, message: Message):
    await message.reply_text(
        "👋 **Welcome to the Media Downloader Bot!**\n\n"
        "1. **Video/Reel:** Send a YouTube or Instagram link to download the video.\n"
        "2. **MP3 Audio:** Send the name of any song/video (e.g., `Shershaah Raatan Lambiyan`) to get the MP3 file."
    )

@bot.on_message(filters.text & filters.private)
async def handle_user_input(client: Client, message: Message):
    text = message.text.strip()

    # If it's a link -> direct download
    if re.search(URL_REGEX, text, flags=re.IGNORECASE):
        logger.info(f"Link received: {text}")
        await download_and_send(client, message, text, is_audio=False)
        return

    # Otherwise treat as search query for song -> download audio
    logger.info(f"Song request received: {text}")
    status_msg = await message.reply_text(f"🔎 **Searching YouTube for:** `{text}`")

    try:
        # Use to_thread to avoid blocking
        def do_search():
            s = VideosSearch(text, limit=1)
            return s.result()

        result = await asyncio.to_thread(do_search)

        if result and result.get("result"):
            first = result["result"][0]
            vid_id = first.get("id")
            title = first.get("title", "Unknown")
            video_url = f"https://www.youtube.com/watch?v={vid_id}"

            await status_msg.edit_text(f"✅ **Found:** `{title}`\n\nStarting MP3 download...")
            await download_and_send(client, message, video_url, is_audio=True)
        else:
            await status_msg.edit_text("❌ **Sorry, no results found** for your search.")
    except Exception as e:
        logger.exception("Error in song search")
        try:
            await status_msg.edit_text(f"❌ **An error occurred during search!**\n\n`{e}`")
        except Exception:
            pass

# --- FastAPI startup/shutdown to run bot in background ---

@web_app.on_event("startup")
async def on_startup():
    logger.info("Starting bot in background...")
    # Start bot as background task (do not block uvicorn)
    asyncio.create_task(bot.start())
    # Optionally wait until bot is ready (small delay)
    # await asyncio.sleep(1)
    logger.info("Bot start requested.")

@web_app.on_event("shutdown")
async def on_shutdown():
    logger.info("Stopping bot...")
    try:
        await bot.stop()
    except Exception:
        logger.exception("Error stopping bot")

# --- Allow running main.py directly for local testing ---
if __name__ == "__main__":
    import uvicorn
    logger.info("Running UVicorn directly for local testing.")
    uvicorn.run("main:web_app", host="0.0.0.0", port=10000, reload=False)
