# Pandey Music Bot - SillyDev / Python 3.11
# Commands: /play /pause /resume /skip /queue /end /ping

import os
import asyncio
import logging
import inspect
from collections import defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from yt_dlp import YoutubeDL
from pytgcalls import PyTgCalls, filters as call_filters
from pytgcalls.types import AudioQuality, MediaStream, StreamEnded

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("PandeyMusicBot")

try:
    API_ID = int(os.getenv("API_ID", "0"))
except ValueError:
    API_ID = 0
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()
# If SESSION_STRING is empty, Pyrogram will use this existing file, e.g. Assistant.session
USER_SESSION_NAME = os.getenv("USER_SESSION_NAME", "Assistant").strip() or "Assistant"
DURATION_LIMIT = max(60, int(os.getenv("DURATION_LIMIT", "3600")))
QUEUE_LIMIT = max(1, int(os.getenv("QUEUE_LIMIT", "20")))

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError(".env mein API_ID, API_HASH aur BOT_TOKEN sahi se set karein.")
if not SESSION_STRING and not Path(f"{USER_SESSION_NAME}.session").exists():
    raise RuntimeError(
        f"User session nahi mila: {USER_SESSION_NAME}.session. "
        "Valid SESSION_STRING dein ya USER_SESSION_NAME ko maujooda session file ke naam par set karein."
    )

bot = Client(
    "PandeyBotClient",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

user_args = {"api_id": API_ID, "api_hash": API_HASH}
if SESSION_STRING:
    user_args["session_string"] = SESSION_STRING
user = Client(USER_SESSION_NAME, **user_args)
calls = PyTgCalls(user)

queues = defaultdict(deque)
locks = defaultdict(asyncio.Lock)

YDL_BASE = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "skip_download": True,
    "socket_timeout": 20,
    "retries": 2,
    "extractor_retries": 2,
    "cachedir": False,
}
if Path("cookies.txt").is_file():
    YDL_BASE["cookiefile"] = "cookies.txt"


def search_song(query: str) -> dict:
    """Find one YouTube result; do not keep an expiring media URL in the queue."""
    options = {**YDL_BASE, "default_search": "ytsearch1"}
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(query, download=False)
    if info and "entries" in info:
        info = next((entry for entry in info["entries"] if entry), None)
    if not info:
        raise ValueError("YouTube par song nahi mila.")
    duration = int(info.get("duration") or 0)
    if duration and duration > DURATION_LIMIT:
        raise ValueError(f"Song duration limit se lamba hai ({DURATION_LIMIT} seconds).")
    webpage_url = info.get("webpage_url") or info.get("original_url")
    if not webpage_url:
        raise ValueError("YouTube ka playable link nahi mila.")
    return {
        "title": info.get("title") or "Unknown title",
        "webpage_url": webpage_url,
        "duration": duration,
    }


def get_fresh_stream_url(webpage_url: str) -> str:
    """Resolve the direct audio URL immediately before playback (fresh URL)."""
    with YoutubeDL(YDL_BASE) as ydl:
        info = ydl.extract_info(webpage_url, download=False)
    if not info or not info.get("url"):
        raise ValueError("YouTube audio stream resolve nahi hua.")
    return info["url"]


async def delete_command(message):
    # Bot ko group mein Delete Messages permission chahiye.
    try:
        await message.delete()
    except Exception:
        pass


async def send_reply(message, text):
    try:
        return await message.reply_text(text, quote=False)
    except FloodWait as exc:
        await asyncio.sleep(exc.value)
        return await message.reply_text(text, quote=False)


async def start_track(chat_id: int, track: dict):
    stream_url = await asyncio.to_thread(get_fresh_stream_url, track["webpage_url"])
    # AudioQuality.HIGH gives the high-quality audio profile supported by PyTgCalls.
    await calls.play(chat_id, MediaStream(stream_url, AudioQuality.HIGH))
    log.info("Now playing in %s: %s", chat_id, track["title"])


async def play_next(chat_id: int):
    async with locks[chat_id]:
        if not queues[chat_id]:
            return None
        track = queues[chat_id].popleft()
        try:
            await start_track(chat_id, track)
            return track
        except Exception:
            log.exception("Could not start queued track in %s", chat_id)
            # Try the next queued item if this one has become unavailable.
            if queues[chat_id]:
                next_track = queues[chat_id].popleft()
                try:
                    await start_track(chat_id, next_track)
                    return next_track
                except Exception:
                    log.exception("Next queued track also failed in %s", chat_id)
            return None


@calls.on_update(call_filters.stream_end())
async def on_stream_end(_, update: StreamEnded):
    """Automatically play the next queued song when the current stream ends."""
    chat_id = update.chat_id
    log.info("Stream ended in %s", chat_id)
    track = await play_next(chat_id)
    if track:
        try:
            await bot.send_message(chat_id, f"▶️ **Now Playing:** {track['title']}")
        except Exception:
            log.exception("Could not send now-playing message")


@bot.on_message(filters.command("play") & filters.group)
async def play_cmd(_, message):
    if len(message.command) < 2:
        await delete_command(message)
        return await send_reply(message, "Use: `/play song name`")

    query = " ".join(message.command[1:]).strip()
    status = await send_reply(message, "🔎 Song search ho raha hai...")
    await delete_command(message)
    try:
        track = await asyncio.to_thread(search_song, query)
        chat_id = message.chat.id
        async with locks[chat_id]:
            try:
                current_call = await calls.get_call(chat_id)
            except Exception:
                current_call = None
            if current_call is not None:
                if len(queues[chat_id]) >= QUEUE_LIMIT:
                    return await status.edit_text("❌ Queue full hai.")
                queues[chat_id].append(track)
                return await status.edit_text(f"➕ **Queue mein add kiya:** {track['title']}")
            await start_track(chat_id, track)
        await status.edit_text(f"▶️ **Now Playing:** {track['title']}")
    except Exception as exc:
        log.exception("/play failed")
        try:
            await status.edit_text(f"❌ Play nahi ho paya: `{str(exc)[:700]}`")
        except Exception:
            pass


@bot.on_message(filters.command("skip") & filters.group)
async def skip_cmd(_, message):
    await delete_command(message)
    chat_id = message.chat.id
    try:
        await calls.leave_call(chat_id)
    except Exception:
        pass
    track = await play_next(chat_id)
    if track:
        await send_reply(message, f"⏭️ **Skipped. Now Playing:** {track['title']}")
    else:
        await send_reply(message, "⏭️ Skipped. Queue khali hai ya agla song play nahi hua.")


@bot.on_message(filters.command("end") & filters.group)
async def end_cmd(_, message):
    await delete_command(message)
    chat_id = message.chat.id
    async with locks[chat_id]:
        queues[chat_id].clear()
        try:
            await calls.leave_call(chat_id)
        except Exception:
            pass
    await send_reply(message, "⏹️ Playback band aur queue clear kar di.")


@bot.on_message(filters.command("queue") & filters.group)
async def queue_cmd(_, message):
    await delete_command(message)
    items = list(queues[message.chat.id])
    if not items:
        return await send_reply(message, "📭 Queue khali hai.")
    lines = [f"{i}. {item['title']}" for i, item in enumerate(items[:20], 1)]
    await send_reply(message, "**🎶 Upcoming Queue:**\n" + "\n".join(lines))


@bot.on_message(filters.command("pause") & filters.group)
async def pause_cmd(_, message):
    await delete_command(message)
    try:
        await calls.pause(message.chat.id)
        await send_reply(message, "⏸️ Paused.")
    except Exception as exc:
        await send_reply(message, f"Pause error: `{str(exc)[:400]}`")


@bot.on_message(filters.command("resume") & filters.group)
async def resume_cmd(_, message):
    await delete_command(message)
    try:
        await calls.resume(message.chat.id)
        await send_reply(message, "▶️ Resumed.")
    except Exception as exc:
        await send_reply(message, f"Resume error: `{str(exc)[:400]}`")


@bot.on_message(filters.command("ping"))
async def ping_cmd(_, message):
    await delete_command(message)
    await send_reply(message, "🏓 Pandey Music Bot online hai.")


async def maybe_await(value):
    # PyTgCalls start() is synchronous in some 3.0.x builds and awaitable in others.
    if inspect.isawaitable(value):
        return await value
    return value


async def main():
    try:
        await bot.start()
        await user.start()
        await maybe_await(calls.start())
        log.info("Pandey Music Bot started successfully.")
        await asyncio.Event().wait()
    finally:
        try:
            await maybe_await(calls.stop())
        except Exception:
            pass
        try:
            await bot.stop()
        except Exception:
            pass
        try:
            await user.stop()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
