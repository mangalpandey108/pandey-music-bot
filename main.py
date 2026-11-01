```python
import os
import asyncio
import traceback
import time
import requests
import yt_dlp

from collections import defaultdict, deque
from dotenv import load_dotenv

from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler

from pytgcalls import PyTgCalls, filters as call_filters
from pytgcalls.types import MediaStream, AudioQuality, StreamEnded


# ---------------- CONFIG ----------------

load_dotenv("/home/container/.env")

API_ID_TEXT = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()

if not API_ID_TEXT.isdigit() or not API_HASH or not BOT_TOKEN:
    raise ValueError("API_ID, API_HASH और BOT_TOKEN .env में जाँचें।")

API_ID = int(API_ID_TEXT)


# ---------------- GLOBALS ----------------

bot = None
user = None
calls = None

queues = defaultdict(deque)
current_songs = {}
playback_starting = set()

http = requests.Session()


# ---------------- YOUTUBE SEARCH ----------------

def search_youtube(query):

    if not YOUTUBE_API_KEY:
        raise ValueError("YOUTUBE_API_KEY .env में नहीं मिली।")

    start = time.perf_counter()

    response = http.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": 1,
            "key": YOUTUBE_API_KEY
        },
        timeout=(5, 10)
    )

    response.raise_for_status()
    data = response.json()

    items = data.get("items", [])

    print(
        f"🔎 YouTube Search Time: "
        f"{time.perf_counter() - start:.2f}s"
    )

    if not items:
        return None

    item = items[0]
    video_id = item["id"]["videoId"]

    return {
        "title": item["snippet"]["title"],
        "url": f"https://www.youtube.com/watch?v={video_id}"
    }


# ---------------- EXTRACT AUDIO ----------------

def extract_audio_url(video_url):

    start = time.perf_counter()

    options = {
        "format": "bestaudio[acodec!=none]/bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 10,
        "retries": 1,
        "extractor_retries": 1,
        "fragment_retries": 1,
        "cachedir": False
    }

    with yt_dlp.YoutubeDL(options) as ydl:

        info = ydl.extract_info(
            video_url,
            download=False
        )

        if not info:
            raise ValueError("Audio जानकारी नहीं मिली।")

        audio_url = info.get("url")

        if not audio_url:
            raise ValueError("Direct Audio URL नहीं मिला।")

    print(
        f"⚡ Audio Extraction Time: "
        f"{time.perf_counter() - start:.2f}s"
    )

    return audio_url


# ---------------- PLAY NEXT ----------------

async def play_next(chat_id):

    global calls, bot

    if chat_id in playback_starting:
        print(f"⏳ Playback already starting: {chat_id}")
        return False

    playback_starting.add(chat_id)

    try:

        if not queues[chat_id]:
            current_songs.pop(chat_id, None)
            print(f"📭 Queue Empty: {chat_id}")
            return False

        song = queues[chat_id].popleft()

        total_start = time.perf_counter()

        audio_url = await asyncio.to_thread(
            extract_audio_url,
            song["url"]
        )

        vc_start = time.perf_counter()

        await calls.play(
            chat_id,
            MediaStream(
                audio_url,
                AudioQuality.HIGH
            )
        )

        print(
            f"📡 VC Startup Time: "
            f"{time.perf_counter() - vc_start:.2f}s"
        )

        current_songs[chat_id] = song

        print(
            f"🎵 Total Playback Start Time: "
            f"{time.perf_counter() - total_start:.2f}s"
        )

        await bot.send_message(
            chat_id,
            f"🎵 अब बज रहा है:\n{song['title']}"
        )

        return True

    except Exception as error:

        print(
            f"❌ Play Error in Group {chat_id}: "
            f"{type(error).__name__}: {error}"
        )

        traceback.print_exc()

        current_songs.pop(chat_id, None)

        await bot.send_message(
            chat_id,
            "❌ गाना नहीं चल पाया। Console Error जाँचें।"
        )

        return False

    finally:
        playback_starting.discard(chat_id)


# ---------------- STREAM END ----------------

async def stream_ended_handler(_, update: StreamEnded):

    chat_id = update.chat_id

    print(f"🏁 STREAM ENDED: {chat_id}")

    current_songs.pop(chat_id, None)

    if queues[chat_id]:
        await play_next(chat_id)
    else:
        await bot.send_message(
            chat_id,
            "🎵 Queue समाप्त हो गई।"
        )


# ---------------- START COMMAND ----------------

async def start_command(client, message):

    print("🟢 START COMMAND RECEIVED")

    await message.reply_text(
        "🎵 Pandey Music Bot ऑनलाइन है!\n\n"
        "Commands:\n"
        "/play गाने का नाम\n"
        "/pause - गाना रोकें\n"
        "/resume - गाना फिर चलाएँ\n"
        "/skip - अगला गाना\n"
        "/stop - Playback बंद करें\n"
        "/end - Playback समाप्त करें\n"
        "/queue - Queue देखें"
    )


# ---------------- PLAY COMMAND ----------------

async def play_command(client, message):

    print("🎵 PLAY COMMAND RECEIVED")

    if len(message.command) < 2:
        await message.reply_text("उदाहरण: /play Kesariya")
        return

    query = " ".join(message.command[1:])

    status = await message.reply_text("🔎 गाना खोज रहा हूँ...")

    try:

        start = time.perf_counter()

        song = await asyncio.to_thread(
            search_youtube,
            query
        )

        if not song:
            await status.edit_text("❌ कोई गाना नहीं मिला।")
            return

        print(
            f"🔎 Total Search Time: "
            f"{time.perf_counter() - start:.2f}s"
        )

        chat_id = message.chat.id

        if chat_id in current_songs or chat_id in playback_starting:

            queues[chat_id].append(song)

            position = len(queues[chat_id])

            await status.edit_text(
                f"✅ Queue में जोड़ा गया:\n"
                f"{song['title']}\n"
                f"स्थान: {position}"
            )
            return

        queues[chat_id].append(song)

        await status.edit_text(
            f"⚡ Audio तैयार किया जा रहा है:\n{song
