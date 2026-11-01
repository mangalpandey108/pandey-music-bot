
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

    elapsed = time.perf_counter() - start
    print(f"🔎 YouTube Search Time: {elapsed:.2f}s")

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

    elapsed = time.perf_counter() - start
    print(f"⚡ Audio Extraction Time: {elapsed:.2f}s")

    return audio_url


# ---------------- PLAY NEXT ----------------

async def play_next(chat_id):

    global calls, bot

    if chat_id in playback_starting:
        return False

    playback_starting.add(chat_id)

    try:

        if not queues[chat_id]:
            current_songs.pop(chat_id, None)
            return False

        song = queues[chat_id].popleft()

        start = time.perf_counter()

        audio_url = await asyncio.to_thread(
            extract_audio_url,
            song["url"]
        )

        await calls.play(
            chat_id,
            MediaStream(
                audio_url,
                AudioQuality.HIGH
            )
        )

        current_songs[chat_id] = song

        elapsed = time.perf_counter() - start
        print(f"🎵 Playback Start Time: {elapsed:.2f}s")

        await bot.send_message(
            chat_id,
            f"🎵 अब बज रहा है:\n{song['title']}"
        )

        return True

    except Exception as error:

        print(
            f"Play Error in Group {chat_id}: "
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

    await message.reply_text(
        "🎵 Pandey Music Bot ऑनलाइन है!\n\n"
        "Commands:\n"
        "/play गाने का नाम\n"
        "/pause - गाना रोकें\n"
        "/resume - गाना फिर चलाएँ\n"
        "/skip - अगला गाना\n"
        "/stop - Playback बंद करें\n"
        "/queue - Queue देखें"
    )


# ---------------- PLAY COMMAND ----------------

async def play_command(client, message):

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

        search_elapsed = time.perf_counter() - start

        print(f"🔎 Total Search Time: {search_elapsed:.2f}s")

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
            f"⚡ Audio तैयार किया जा रहा है:\n{song['title']}"
        )

        success = await play_next(chat_id)

        if success:
            await status.edit_text(
                f"🎵 Playing:\n{song['title']}"
            )
        else:
            await status.edit_text(
                "❌ गाना नहीं चल पाया। Console Error जाँचें।"
            )

    except Exception as error:

        print(
            f"Play Command Error: "
            f"{type(error).__name__}: {error}"
        )

        traceback.print_exc()

        await status.edit_text(
            "❌ गाना खोजने या चलाने में समस्या आई।"
        )


# ---------------- PAUSE COMMAND ----------------

async def pause_command(client, message):

    try:
        await calls.pause(message.chat.id)
        await message.reply_text("⏸️ गाना रोक दिया गया।")

    except Exception as error:

        print(f"Pause Error: {error}")
        traceback.print_exc()

        await message.reply_text("❌ गाना Pause नहीं हुआ।")


# ---------------- RESUME COMMAND ----------------

async def resume_command(client, message):

    try:
        await calls.resume(message.chat.id)
        await message.reply_text("▶️ गाना फिर शुरू हो गया।")

    except Exception as error:

        print(f"Resume Error: {error}")
        traceback.print_exc()

        await message.reply_text("❌ गाना Resume नहीं हुआ।")


# ---------------- SKIP COMMAND ----------------

async def skip_command(client, message):

    chat_id = message.chat.id

    try:

        await calls.leave_call(chat_id)

        current_songs.pop(chat_id, None)

        success = await play_next(chat_id)

        if not success:
            await message.reply_text(
                "📭 Queue में अगला गाना नहीं है।"
            )

    except Exception as error:

        print(f"Skip Error: {error}")
        traceback.print_exc()

        await message.reply_text(
            "❌ Skip करने में समस्या आई।"
        )


# ---------------- STOP COMMAND ----------------

async def stop_command(client, message):

    chat_id = message.chat.id

    try:
        await calls.leave_call(chat_id)

    except Exception as error:
        print(f"Stop Error: {error}")

    current_songs.pop(chat_id, None)
    queues[chat_id].clear()

    await message.reply_text(
        "⏹️ Playback बंद और Queue खाली कर दी गई।"
    )


# ---------------- QUEUE COMMAND ----------------

async def queue_command(client, message):

    chat_id = message.chat.id

    current = current_songs.get(chat_id)
    upcoming = list(queues[chat_id])

    if not current and not upcoming:
        await message.reply_text("📭 Queue खाली है।")
        return

    queue_text = "🎵 **Music Queue**\n\n"

    if current:
        queue_text += (
            f"▶️ अभी बज रहा है:\n"
            f"{current['title']}\n\n"
        )

    if upcoming:

        queue_text += "आने वाले गाने:\n"

        for index, song in enumerate(upcoming[:20], start=1):
            queue_text += f"{index}. {song['title']}\n"

        if len(upcoming) > 20:
            queue_text += (
                f"\nऔर {len(upcoming) - 20} "
                "गाने Queue में हैं।"
            )

    await message.reply_text(queue_text)


# ---------------- MAIN ----------------

async def main():

    global bot, user, calls

    print("🤖 Pandey Music Bot शुरू हो रहा है...")
    print("Event Loop:", id(asyncio.get_running_loop()))

    bot = Client(
        "PandeyMusicBot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN
    )

    user = Client(
        "UserSession",
        api_id=API_ID,
        api_hash=API_HASH
    )

    calls = PyTgCalls(user)

    bot.add_handler(MessageHandler(start_command, filters.command("start")))
    bot.add_handler(MessageHandler(play_command, filters.command("play")))
    bot.add_handler(MessageHandler(pause_command, filters.command("pause")))
    bot.add_handler(MessageHandler(resume_command, filters.command("resume")))
    bot.add_handler(MessageHandler(skip_command, filters.command("skip")))
    bot.add_handler(MessageHandler(stop_command, filters.command("stop")))
    bot.add_handler(MessageHandler(queue_command, filters.command("queue")))

    calls.on_update(call_filters.stream_end())(stream_ended_handler)

    try:

        await bot.start()
        print("✅ Bot Client Started")

        await user.start()

        me = await user.get_me()

        print("✅ User Session Started")
        print("👤 Account Name:", me.first_name)
        print("🆔 Account ID:", me.id)
        print("🤖 Is Bot Account?:", me.is_bot)

        if me.is_bot:
            raise RuntimeError(
                "UserSession में सामान्य Telegram User Account का Session लगाएँ।"
            )

        await calls.start()

        print("✅ PyTgCalls Started")
        print("🎵 Pandey Music Bot Online!")
        print("⚡ Optimized Playback Enabled!")

        await asyncio.Event().wait()

    finally:

        print("🛑 Bot बंद हो रहा है...")

        try:
            await calls.stop()
        except Exception:
            pass

        try:
            await user.stop()
        except Exception:
            pass

        try:
            await bot.stop()
        except Exception:
            pass

        http.close()


# ---------------- RUN ----------------

if __name__ == "__main__":
    asyncio.run(main())
