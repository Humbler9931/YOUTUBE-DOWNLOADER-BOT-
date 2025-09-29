import os
import re
import logging
import asyncio
from dotenv import load_dotenv

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import MessageNotModified
from pyrogram.errors.exceptions.flood_420 import FloodWait

from yt_dlp import YoutubeDL
from youtubesearchpython import VideosSearch

# Load environment variables from .env file
load_dotenv()

# --- Configuration & Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Environment variables
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DOWNLOAD_DIR = "downloads/"
YTDL_TIMEOUT = 1800 # 30 minutes

# Create download directory if it doesn't exist
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Pyrogram Client
app = Client(
    "ytdl_downloader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# Regex patterns for links
URL_REGEX = r'(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|instagram\.com/reel/|instagram\.com/p/|instagram\.com/tv/|tiktok\.com/)'

# --- Helper Functions ---

# Pyrogram upload progress callback (for showing progress bar)
async def progress_for_pyrogram(current, total, ud_message, start_time):
    """
    Shows progress for Pyrogram file uploads/downloads.
    """
    now = int(time.time())
    if (now - start_time) < 1:
        return
    
    # Avoid frequent updates
    if current % (total // 100) != 0 and current != total:
        return

    percentage = current * 100 / total
    elapsed = now - start_time
    
    status = f"**Uploading:** `[{round(percentage)}%]`\n"
    status += f"**Progress:** `[{'▓' * int(percentage/10)}{'░' * (10 - int(percentage/10))}]`\n"
    status += f"**Size:** `{humanbytes(current)} / {humanbytes(total)}`\n"
    status += f"**Speed:** `{humanbytes(current / elapsed)}/s`"

    try:
        await ud_message.edit_text(status)
    except MessageNotModified:
        pass
    except FloodWait as e:
        await asyncio.sleep(e.value)

# Convert bytes to human-readable format
def humanbytes(size):
    """
    Convert bytes into human readable format.
    """
    if not size:
        return ""
    power = 2**10
    n = 0
    Power_list = {0: ' ', 1: 'Ki', 2: 'Mi', 3: 'Gi', 4: 'Ti'}
    while size > power:
        size /= power
        n += 1
    return f"{round(size, 2)} {Power_list[n]}B"

import time # Used by progress_for_pyrogram

# --- Core YTDL Download Logic ---

async def download_and_send(client: Client, message: Message, url: str, is_audio=False):
    """
    Handles the main download, upload, and cleanup process.
    """
    status_msg = await message.reply_text("⏳ **Processing request...**")
    
    # YTDL Options
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]' if not is_audio else 'bestaudio/best',
        'outtmpl': os.path.join(DOWNLOAD_DIR, '%(title)s-%(id)s.%(ext)s'),
        'updatetime': True,
        'nocheckcertificate': True,
        'max_filesize': 2097152000,  # Max 2GB
        'no_warnings': True,
        'ignoreerrors': True,
        'prefer_ffmpeg': True,
        'quiet': True,
        'extract_audio': is_audio,
        'audioformat': 'mp3' if is_audio else None,
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}] if is_audio else None,
        'sleep_interval_requests': 1,
        'sleep_interval_wait': 5,
        'socket_timeout': YTDL_TIMEOUT,
    }
    
    temp_file = None
    try:
        await status_msg.edit_text("📥 **Downloading content... Please wait.**")
        
        with YoutubeDL(ydl_opts) as ydl:
            # Run ytdl in a separate thread to prevent blocking Pyrogram's event loop
            info_dict = await asyncio.to_thread(ydl.extract_info, url, download=True)
            
            # Get the actual file path after download/conversion
            # yt-dlp returns the path of the downloaded file (which could be the converted file)
            temp_file = ydl.prepare_filename(info_dict)
            
            # If audio was extracted, the file extension changes to mp3
            if is_audio:
                temp_file = temp_file.rsplit('.', 1)[0] + '.mp3'
            
            if not os.path.exists(temp_file):
                 # Handle cases where postprocessor changes filename (e.g., .webm to .mp3)
                if is_audio:
                    # Try finding the mp3 file
                    base_name = temp_file.rsplit('.', 1)[0]
                    mp3_path = base_name + '.mp3'
                    if os.path.exists(mp3_path):
                        temp_file = mp3_path
                    else:
                        raise FileNotFoundError(f"Final audio file not found: {mp3_path}")
                else:
                    raise FileNotFoundError(f"Downloaded file not found at: {temp_file}")
            
            file_size = os.path.getsize(temp_file)
            
            await status_msg.edit_text(f"⬆️ **Uploading file:** `{info_dict.get('title')}`")
            
            start_time = time.time()
            
            # Send the file using Pyrogram's built-in methods
            if is_audio:
                await client.send_audio(
                    chat_id=message.chat.id,
                    audio=temp_file,
                    caption=f"🎵 Downloaded by @{client.me.username}",
                    file_name=os.path.basename(temp_file),
                    progress=progress_for_pyrogram,
                    progress_args=(status_msg, start_time)
                )
            else:
                await client.send_video(
                    chat_id=message.chat.id,
                    video=temp_file,
                    caption=f"🎬 Downloaded by @{client.me.username}",
                    file_name=os.path.basename(temp_file),
                    progress=progress_for_pyrogram,
                    progress_args=(status_msg, start_time)
                )

        await status_msg.delete()
        await message.reply_text("✅ **Download and upload successful!**")

    except Exception as e:
        logger.error(f"Error in download_and_send: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ **An error occurred!**\n\n`{e}`")
    
    finally:
        # --- File Cleanup (MANDATORY) ---
        if temp_file and os.path.exists(temp_file):
            logger.info(f"Deleting file: {temp_file}")
            os.remove(temp_file)
            logger.info("File deleted successfully.")

# --- Pyrogram Handlers ---

@app.on_message(filters.command("start") & filters.private)
async def start_command(client: Client, message: Message):
    """Handles the /start command."""
    await message.reply_text(
        "👋 **Welcome to the Media Downloader Bot!**\n\n"
        "1. **Video/Reel:** Simply send a YouTube or Instagram link to download the video.\n"
        "2. **MP3 Audio:** Send the name of any song/video (e.g., `Shershaah Raatan Lambiyan`) to get the MP3 file."
    )

@app.on_message(filters.text & filters.private)
async def handle_user_input(client: Client, message: Message):
    """Main handler for text input (Links or Song Names)."""
    text = message.text.strip()
    
    # 1. Check for URL
    if re.search(URL_REGEX, text):
        logger.info(f"Link received: {text}")
        await download_and_send(client, message, text, is_audio=False)
        
    # 2. Assume Song Name and search on YouTube
    else:
        logger.info(f"Song request received: {text}")
        status_msg = await message.reply_text(f"🔎 **Searching YouTube for:** `{text}`")
        
        try:
            # Search for the video
            search = await asyncio.to_thread(VideosSearch, text, limit = 1)
            result = search.result()
            
            if result and result.get('result'):
                first_result = result['result'][0]
                video_url = f"https://www.youtube.com/watch?v={first_result['id']}"
                
                await status_msg.edit_text(f"✅ **Found:** `{first_result['title']}`\n\n"
                                           "Starting MP3 download...")
                
                # Download and send as MP3
                await download_and_send(client, message, video_url, is_audio=True)
            
            else:
                await status_msg.edit_text("❌ **Sorry, no results found** for your search.")
        
        except Exception as e:
            logger.error(f"Error in song search: {e}")
            await status_msg.edit_text(f"❌ **An error occurred during search!**\n\n`{e}`")
            
# --- Bot Start ---
if __name__ == "__main__":
    logger.info("Bot starting...")
    app.run()
    logger.info("Bot stopped.")
