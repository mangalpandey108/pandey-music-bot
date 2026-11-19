import os
import re
import asyncio
import logging
from collections import defaultdict, deque

import yt_dlp
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, AudioQuality

load_dotenv()

# ---------------- CONFIG ----------------

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")

DURATION_LIMIT = int(os.getenv("DURATION_LIMIT", "3600"))
QUEUE_LIMIT = int(os.getenv("QUEUE_LIMIT", "20"))

if not all([API_ID, API_HASH, BOT_TOKEN, SESSION_STRING]):
    raise RuntimeError(
        "Missing API_ID, API_HASH, BOT_TOKEN or SESSION_STRING"
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("MusicBot")

# ---------------- CLIENTS ----------------

bot = Client(
    "music_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

user = Client(
    "music_user",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

calls = PyTgCalls(user)

# ---------------- STORAGE ----------------

queues = defaultdict(deque)
playing = set()
locks = defaultdict(asyncio.Lock)


# ---------------- YOUTUBE ----------------

YDL_OPTIONS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "skip_download": True,
    "socket_timeout": 20,
    "retries": 2,
    "extractor_retries": 2,
    "cachedir": False,
    "default_search": "ytsearch1",
}

def extract_song(query):
    with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
        info = ydl.extract_info(query, download=False)

        if "entries" in info:
            info = next(
                (entry for entry in info["entries"] if entry),
                None
            )

        if not info:
            raise ValueError("Song not found")

        duration = int(info.get("duration") or 0)

        if duration > DURATION_LIMIT:
            raise ValueError("Song duration limit exceeded")

        return {
            "title": info.get("title", "Unknown"),
            "url": info["url"],
            "webpage": info.get("webpage_url", query),
            "duration": duration,
        }


async def get_song(query):
    return await asyncio.to_thread(extract_song, query)


# ---------------- PLAYBACK ----------------

async def play_next(chat_id):
    async with locks[chat_id]:
        if not queues[chat_id]:
            playing.discard(chat_id)
            return False

        song = queues[chat_id].popleft()

        try:
            await calls.play(
                chat_id,
                MediaStream(
                    song["url"],
                    AudioQuality.HIGH
                )
            )

            playing.add(chat_id)
            return song

        except Exception:
            log.exception("Playback failed in %s", chat_id)
            playing.discard(chat_id)
            return False


# ---------------- COMMANDS ----------------

@bot.on_message(filters.command("start"))
async def start_command(_, message):
    await message.reply_text(
        "🎵 **YouTube Music Bot is Online!**\n\n"
        "/play song name - Play music\n"
        "/pause - Pause music\n"
        "/resume - Resume music\n"
        "/skip - Skip current song\n"
        "/queue - Show queue\n"
        "/end - Stop music\n"
        "/help - Commands"
    )


@bot.on_message(filters.command("help"))
async def help_command(_, message):
    await message.reply_text(
        "**Music Commands**\n\n"
        "`/play song name`\n"
        "`/pause`\n"
        "`/resume`\n"
        "`/skip`\n"
        "`/queue`\n"
        "`/end`"
    )


@bot.on_message(filters.command("play") & filters.group)
async def play_command(_, message):
    if len(message.command) < 2:
        return await message.reply_text(
            "Usage: `/play song name or YouTube URL`"
        )

    query = message.text.split(maxsplit=1)[1].strip()
    status = await message.reply_text("🔎 Searching YouTube...")

    try:
        song = await get_song(query)

        if len(queues[message.chat.id]) >= QUEUE_LIMIT:
            return await status.edit_text("❌ Queue is full.")

        chat_id = message.chat.id

        async with locks[chat_id]:
            if chat_id in playing:
                queues[chat_id].append(song)
                position = len(queues[chat_id])
                await status.edit_text(
                    f"➕ **Added to Queue**\n\n"
                    f"🎵 {song['title']}\n"
                    f"📌 Position: {position}"
                )
                return

            try:
                await calls.play(
                    chat_id,
                    MediaStream(
                        song["url"],
                        AudioQuality.HIGH
                    )
                )
                playing.add(chat_id)

            except Exception:
                log.exception("Could not start playback")
                raise RuntimeError(
                    "Voice Chat start nahi hua. "
                    "User account ko group mein add karein."
                )

        await status.edit_text(
            f"▶️ **Now Playing**\n\n"
            f"🎵 {song['title']}\n"
            f"⏱ Duration: {song['duration']} seconds\n"
            f"🔗 [YouTube]({song['webpage']})"
        )

    except Exception as error:
        log.exception("Play command error")
        await status.edit_text(f"❌ Error: {str(error)[:300]}")


@bot.on_message(filters.command("pause") & filters.group)
async def pause_command(_, message):
    try:
        await calls.pause(message.chat.id)
        await message.reply_text("⏸ Music Paused.")
    except Exception as error:
        await message.reply_text(f"❌ {str(error)[:200]}")


@bot.on_message(filters.command("resume") & filters.group)
async def resume_command(_, message):
    try:
        await calls.resume(message.chat.id)
        await message.reply_text("▶️ Music Resumed.")
    except Exception as error:
        await message.reply_text(f"❌ {str(error)[:200]}")


@bot.on_message(filters.command("skip") & filters.group)
async def skip_command(_, message):
    chat_id = message.chat.id

    async with locks[chat_id]:
        if chat_id not in playing:
            return await message.reply_text("❌ Nothing is playing.")

        try:
            await calls.stop(chat_id)
        except Exception:
            log.exception("Stop failed")

        playing.discard(chat_id)

        if not queues[chat_id]:
            await message.reply_text("⏭ Skipped. Queue is empty.")
            return

        song = queues[chat_id].popleft()

        try:
            await calls.play(
                chat_id,
                MediaStream(song["url"], AudioQuality.HIGH)
            )
            playing.add(chat_id)

            await message.reply_text(
                f"⏭ **Skipped!**\n▶️ Now Playing: {song['title']}"
            )
        except Exception as error:
            log.exception("Next song failed")
            await message.reply_text(f"❌ Playback error: {str(error)[:200]}")


@bot.on_message(filters.command("queue") & filters.group)
async def queue_command(_, message):
    chat_id = message.chat.id
    songs = list(queues[chat_id])

    if not songs:
        return await message.reply_text("📭 Queue is empty.")

    output = "\n".join(
        f"{i}. {song['title']}"
        for i, song in enumerate(songs[:15], 1)
    )

    await message.reply_text(f"📃 **Upcoming Songs**\n\n{output}")


@bot.on_message(filters.command("end") & filters.group)
async def end_command(_, message):
    chat_id = message.chat.id

    async with locks[chat_id]:
        queues[chat_id].clear()

        try:
            await calls.stop(chat_id)
        except Exception:
            log.exception("Stop error")

        playing.discard(chat_id)

    await message.reply_text("⏹ Music stopped and queue cleared.")


# ---------------- ERROR HANDLING ----------------

@bot.on_message(filters.command("ping"))
async def ping_command(_, message):
    await message.reply_text("🏓 Pong! Bot is alive.")


async def main():
    await bot.start()
    log.info("Bot client started")

    await user.start()
    log.info("User client started")

    calls.start()
    log.info("PyTgCalls started")

    log.info("YouTube Music Bot is running")

    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped")
