
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
skip_in_progress = set()
stop_in_progress = set()

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

        try:
            await bot.send_message(
                chat_id,
                "❌ गाना नहीं चल पाया। Console Error जाँचें।"
            )
        except Exception:
            traceback.print_exc()

        return False

    finally:
        playback_starting.discard(chat_id)


# ---------------- STREAM END ----------------

async def stream_ended_handler(_, update: StreamEnded):

    chat_id = update.chat_id

    print(f"🏁 STREAM ENDED: {chat_id}")

    if chat_id in skip_in_progress or chat_id in stop_in_progress:
        print(f"⏭️ Manual action in progress: {chat_id}")
        return

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

        if (
            chat_id in current_songs
            or chat_id in playback_starting
            or chat_id in skip_in_progress
        ):

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
            f"⚡ Audio तैयार किया जा रहा है:\n"
            f"{song['title']}"
        )

        success = await play_next(chat_id)

        if not success:
            await status.edit_text(
                "❌ गाना शुरू नहीं हो पाया। Console Error जाँचें।"
            )
        else:
            await status.delete()

    except Exception as error:

        print(f"❌ PLAY COMMAND ERROR: {error}")
        traceback.print_exc()

        await status.edit_text(
            "❌ गाना खोजने या चलाने में समस्या हुई।"
        )


# ---------------- PAUSE COMMAND ----------------

async def pause_command(client, message):

    print("🟡 PAUSE COMMAND RECEIVED")

    chat_id = message.chat.id

    try:

        if chat_id not in current_songs:
            await message.reply_text("❌ अभी कोई गाना नहीं चल रहा है।")
            return

        await calls.pause_stream(chat_id)

        print(f"⏸️ PAUSED: {chat_id}")

        await message.reply_text("⏸️ गाना रोक दिया गया है।")

    except Exception as error:

        print(f"❌ PAUSE ERROR: {error}")
        traceback.print_exc()

        await message.reply_text(
            f"❌ Pause Error: {type(error).__name__}"
        )


# ---------------- RESUME COMMAND ----------------

async def resume_command(client, message):

    print("🟢 RESUME COMMAND RECEIVED")

    chat_id = message.chat.id

    try:

        if chat_id not in current_songs:
            await message.reply_text("❌ अभी कोई गाना नहीं चल रहा है।")
            return

        await calls.resume_stream(chat_id)

        print(f"▶️ RESUMED: {chat_id}")

        await message.reply_text("▶️ गाना फिर से शुरू हो गया है।")

    except Exception as error:

        print(f"❌ RESUME ERROR: {error}")
        traceback.print_exc()

        await message.reply_text(
            f"❌ Resume Error: {type(error).__name__}"
        )


# ---------------- SKIP COMMAND ----------------

async def skip_command(client, message):

    print("⏭️ SKIP COMMAND RECEIVED")

    chat_id = message.chat.id

    if chat_id in skip_in_progress:
        await message.reply_text("⏳ Skip पहले से चल रहा है।")
        return

    if chat_id not in current_songs:
        await message.reply_text("❌ अभी कोई गाना नहीं चल रहा है।")
        return

    skip_in_progress.add(chat_id)

    try:

        await calls.leave_call(chat_id)

        current_songs.pop(chat_id, None)

        if queues[chat_id]:
            await message.reply_text("⏭️ अगला गाना शुरू किया जा रहा है।")
        else:
            await message.reply_text("📭 Queue खाली है।")

    except Exception as error:

        print(f"❌ SKIP ERROR: {error}")
        traceback.print_exc()

        await message.reply_text(
            f"❌ Skip Error: {type(error).__name__}"
        )

    finally:
        skip_in_progress.discard(chat_id)

    if queues[chat_id]:
        await play_next(chat_id)


# ---------------- STOP COMMAND ----------------

async def stop_command(client, message):

    print("🔴 STOP COMMAND RECEIVED")

    chat_id = message.chat.id

    stop_in_progress.add(chat_id)

    try:

        queues[chat_id].clear()
        current_songs.pop(chat_id, None)

        await calls.leave_call(chat_id)

        print(f"⏹️ STOPPED: {chat_id}")

        await message.reply_text(
            "⏹️ Playback बंद कर दिया गया है और Queue साफ कर दी गई है।"
        )

    except Exception as error:

        print(f"❌ STOP ERROR: {error}")
        traceback.print_exc()

        await message.reply_text(
            f"❌ Stop Error: {type(error).__name__}"
        )

    finally:
        stop_in_progress.discard(chat_id)


# ---------------- END COMMAND ----------------

async def end_command(client, message):

    print("🔴 END COMMAND RECEIVED")

    chat_id = message.chat.id

    stop_in_progress.add(chat_id)

    try:

        queues[chat_id].clear()
        current_songs.pop(chat_id, None)

        await calls.leave_call(chat_id)

        print(f"🔚 ENDED: {chat_id}")

        await message.reply_text(
            "🔚 Playback समाप्त कर दिया गया है।"
        )

    except Exception as error:

        print(f"❌ END ERROR: {error}")
        traceback.print_exc()

        await message.reply_text(
            f"❌ End Error: {type(error).__name__}"
        )

    finally:
        stop_in_progress.discard(chat_id)


# ---------------- QUEUE COMMAND ----------------

async def queue_command(client, message):

    print("📋 QUEUE COMMAND RECEIVED")

    chat_id = message.chat.id

    try:

        queue = queues[chat_id]
        current = current_songs.get(chat_id)

        if not current and not queue:
            await message.reply_text("📭 Queue खाली है।")
            return

        text = "🎵 **Music Queue**\n\n"

        if current:
            text += f"▶️ **अभी बज रहा है:**\n{current['title']}\n\n"

        if queue:
            text += "📋 **आने वाले गाने:**\n"

            for number, song in enumerate(list(queue)[:10], start=1):
                text += f"{number}. {song['title']}\n"

            if len(queue) > 10:
                text += f"\nऔर {len(queue) - 10} गाने बाकी हैं।"

        await message.reply_text(text)

    except Exception as error:

        print(f"❌ QUEUE ERROR: {error}")
        traceback.print_exc()

        await message.reply_text("❌ Queue दिखाने में समस्या हुई।")


# ---------------- MAIN ----------------

async def main():

    global bot, user, calls

    print("🤖 Pandey Music Bot शुरू हो रहा है...")
    print(f"Event Loop: {id(asyncio.get_running_loop())}")

    bot = Client(
        "PandeyMusicBot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN
    )

    # नई FreshLogin session इस्तेमाल होगी
    user = Client(
        "FreshLogin",
        api_id=API_ID,
        api_hash=API_HASH
    )

    calls = PyTgCalls(user)

    handlers = [
        ("start", start_command),
        ("play", play_command),
        ("pause", pause_command),
        ("resume", resume_command),
        ("skip", skip_command),
        ("stop", stop_command),
        ("end", end_command),
        ("queue", queue_command)
    ]

    try:

        await bot.start()
        print("✅ Bot Client Started")

        await user.start()
        print("✅ FreshLogin User Session Started")

        me = await user.get_me()

        print(f"👤 Account Name: {me.first_name}")
        print(f"🆔 Account ID: {me.id}")
        print(f"🤖 Is Bot Account?: {me.is_bot}")

        if me.is_bot:
            raise RuntimeError(
                "FreshLogin में Bot Account का session है। "
                "Normal User Account का session इस्तेमाल करें।"
            )

        await calls.start()
        print("✅ PyTgCalls Started")

        calls.on_update(
            call_filters.stream_end()
        )(stream_ended_handler)

        for command_name, callback in handlers:

            bot.add_handler(
                MessageHandler(
                    callback,
                    filters.command(command_name)
                )
            )

            print(f"✅ Handler Registered: /{command_name}")

        print("🎵 Pandey Music Bot Online!")
        print("🌐 Multi-Group Queue Enabled!")

        await asyncio.Event().wait()

    except Exception as error:

        print(f"❌ MAIN ERROR: {error}")
        traceback.print_exc()

        raise

    finally:

        print("🛑 Bot बंद हो रहा है...")

        # इस PyTgCalls version में calls.stop() उपलब्ध नहीं है।
        # पहले User Client और फिर Bot Client बंद करें।

        try:
            if user and user.is_connected:
                await user.stop()
                print("✅ User Client Stopped")
        except Exception:
            traceback.print_exc()

        try:
            if bot and bot.is_connected:
                await bot.stop()
                print("✅ Bot Client Stopped")
        except Exception:
            traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
