
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


# ================= CONFIG =================

load_dotenv("/home/container/.env")

API_ID_TEXT = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()

if not API_ID_TEXT.isdigit() or not API_HASH or not BOT_TOKEN:
    raise ValueError("API_ID, API_HASH और BOT_TOKEN .env में जाँचें।")

API_ID = int(API_ID_TEXT)


# ================= GLOBALS =================

bot = None
user = None
calls = None

queues = defaultdict(deque)
current_songs = {}

playback_starting = set()
skip_in_progress = set()
stop_in_progress = set()

http = requests.Session()


# ================= YOUTUBE SEARCH =================

def search_youtube(query):

    if not YOUTUBE_API_KEY:
        raise ValueError("YOUTUBE_API_KEY .env में नहीं मिली।")

    started = time.perf_counter()

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

    items = response.json().get("items", [])

    print(
        f"🔎 YouTube Search Time: "
        f"{time.perf_counter() - started:.2f}s"
    )

    if not items:
        return None

    item = items[0]
    video_id = item["id"]["videoId"]

    return {
        "title": item["snippet"]["title"],
        "url": f"https://www.youtube.com/watch?v={video_id}"
    }


# ================= AUDIO EXTRACTION =================

def extract_audio_url(video_url):

    started = time.perf_counter()

    options = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": False,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 2,
        "fragment_retries": 2,
        "cachedir": False
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(
            video_url,
            download=False
        )

    if not info:
        raise ValueError("Audio information नहीं मिली।")

    audio_url = info.get("url")

    if not audio_url:

        formats = info.get("formats") or []

        audio_formats = [
            item for item in formats
            if item.get("url")
            and item.get("acodec") not in (None, "none")
        ]

        if audio_formats:

            audio_formats.sort(
                key=lambda item: (
                    item.get("abr") or 0,
                    item.get("tbr") or 0
                ),
                reverse=True
            )

            audio_url = audio_formats[0]["url"]

    if not audio_url:
        raise ValueError("Direct Audio URL उपलब्ध नहीं है।")

    print(
        f"⚡ Audio Extraction Time: "
        f"{time.perf_counter() - started:.2f}s"
    )

    return audio_url


# ================= PLAY NEXT =================

async def play_next(chat_id):

    if chat_id in playback_starting:
        print(f"⏳ Playback पहले से शुरू हो रहा है: {chat_id}")
        return False

    playback_starting.add(chat_id)

    try:

        while queues[chat_id]:

            if chat_id in stop_in_progress:
                return False

            song = queues[chat_id].popleft()

            started = time.perf_counter()

            try:

                print(f"🎧 Trying: {song['title']}")

                audio_url = await asyncio.to_thread(
                    extract_audio_url,
                    song["url"]
                )

                vc_started = time.perf_counter()

                await calls.play(
                    chat_id,
                    MediaStream(
                        audio_url,
                        AudioQuality.HIGH
                    )
                )

                print(
                    f"📡 VC Startup Time: "
                    f"{time.perf_counter() - vc_started:.2f}s"
                )

                current_songs[chat_id] = song

                print(
                    f"🎵 Total Playback Start Time: "
                    f"{time.perf_counter() - started:.2f}s"
                )

                await bot.send_message(
                    chat_id,
                    f"🎵 अब बज रहा है:\n{song['title']}"
                )

                return True

            except Exception as error:

                current_songs.pop(chat_id, None)

                print(
                    f"❌ Song Failed: {song.get('title')}"
                )

                print(
                    f"Error: {type(error).__name__}: {error}"
                )

                traceback.print_exc()

                try:
                    await bot.send_message(
                        chat_id,
                        "⚠️ यह गाना उपलब्ध नहीं हो पाया:\n"
                        f"{song.get('title', 'Unknown')}\n"
                        "Queue का अगला गाना Try कर रहा हूँ।"
                    )
                except Exception:
                    traceback.print_exc()

                # अगला गाना while loop में Try होगा।

        current_songs.pop(chat_id, None)

        print(f"📭 Queue Empty: {chat_id}")

        try:
            await bot.send_message(
                chat_id,
                "❌ Queue के गाने शुरू नहीं हो पाए या Queue खाली है।"
            )
        except Exception:
            traceback.print_exc()

        return False

    finally:
        playback_starting.discard(chat_id)


# ================= STREAM END =================

async def stream_ended_handler(_, update: StreamEnded):

    chat_id = update.chat_id

    print(f"🏁 STREAM ENDED: {chat_id}")

    if chat_id in skip_in_progress:
        return

    if chat_id in stop_in_progress:
        return

    current_songs.pop(chat_id, None)

    if queues[chat_id]:
        await play_next(chat_id)
    else:
        await bot.send_message(
            chat_id,
            "🎵 Queue समाप्त हो गई।"
        )


# ================= START COMMAND =================

async def start_command(client, message):

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


# ================= PLAY COMMAND =================

async def play_command(client, message):

    print("🎵 PLAY COMMAND RECEIVED")

    if len(message.command) < 2:
        await message.reply_text(
            "उदाहरण: /play Kesariya"
        )
        return

    query = " ".join(message.command[1:])

    status = await message.reply_text(
        "🔎 गाना खोज रहा हूँ..."
    )

    try:

        started = time.perf_counter()

        song = await asyncio.to_thread(
            search_youtube,
            query
        )

        print(
            f"🔎 Total Search Time: "
            f"{time.perf_counter() - started:.2f}s"
        )

        if not song:
            await status.edit_text(
                "❌ कोई गाना नहीं मिला।"
            )
            return

        chat_id = message.chat.id

        queues[chat_id].append(song)

        if (
            chat_id in current_songs
            or chat_id in playback_starting
            or chat_id in skip_in_progress
        ):

            await status.edit_text(
                f"✅ Queue में जोड़ा गया:\n"
                f"{song['title']}\n"
                f"स्थान: {len(queues[chat_id])}"
            )

            return

        await status.edit_text(
            f"⚡ Audio तैयार किया जा रहा है:\n{song['title']}"
        )

        success = await play_next(chat_id)

        if success:
            await status.delete()
        else:
            await status.edit_text(
                "❌ Queue का कोई गाना शुरू नहीं हो पाया। "
                "Console में Error देखें।"
            )

    except Exception as error:

        print(
            f"❌ PLAY ERROR: "
            f"{type(error).__name__}: {error}"
        )

        traceback.print_exc()

        try:
            await status.edit_text(
                "❌ गाना खोजने या चलाने में समस्या हुई।"
            )
        except Exception:
            traceback.print_exc()


# ================= PAUSE COMMAND =================

async def pause_command(client, message):

    chat_id = message.chat.id

    try:

        if chat_id not in current_songs:
            await message.reply_text(
                "❌ अभी कोई गाना नहीं चल रहा है।"
            )
            return

        await calls.pause_stream(chat_id)

        await message.reply_text(
            "⏸️ गाना रोक दिया गया है।"
        )

    except Exception as error:

        print(f"❌ PAUSE ERROR: {error}")
        traceback.print_exc()

        await message.reply_text(
            f"❌ Pause Error: {type(error).__name__}"
        )


# ================= RESUME COMMAND =================

async def resume_command(client, message):

    chat_id = message.chat.id

    try:

        if chat_id not in current_songs:
            await message.reply_text(
                "❌ अभी कोई गाना नहीं चल रहा है।"
            )
            return

        await calls.resume_stream(chat_id)

        await message.reply_text(
            "▶️ गाना फिर शुरू हो गया है।"
        )

    except Exception as error:

        print(f"❌ RESUME ERROR: {error}")
        traceback.print_exc()

        await message.reply_text(
            f"❌ Resume Error: {type(error).__name__}"
        )


# ================= SKIP COMMAND =================

async def skip_command(client, message):

    chat_id = message.chat.id

    if chat_id in skip_in_progress:
        await message.reply_text(
            "⏳ Skip पहले से चल रहा है।"
        )
        return

    if chat_id not in current_songs:
        await message.reply_text(
            "❌ अभी कोई गाना नहीं चल रहा है।"
        )
        return

    skip_in_progress.add(chat_id)

    try:

        await calls.leave_call(chat_id)

        current_songs.pop(chat_id, None)

        await message.reply_text(
            "⏭️ अगला गाना शुरू किया जा रहा है।"
        )

    except Exception as error:

        print(f"❌ SKIP ERROR: {error}")
        traceback.print_exc()

        await message.reply_text(
            f"❌ Skip Error: {type(error).__name__}"
        )

        return

    finally:
        skip_in_progress.discard(chat_id)

    if queues[chat_id]:
        await play_next(chat_id)
    else:
        await message.reply_text(
            "📭 Queue खाली है।"
        )


# ================= STOP COMMAND =================

async def stop_command(client, message):

    chat_id = message.chat.id

    stop_in_progress.add(chat_id)

    try:

        queues[chat_id].clear()
        current_songs.pop(chat_id, None)

        await calls.leave_call(chat_id)

        await message.reply_text(
            "⏹️ Playback बंद और Queue साफ कर दी गई है।"
        )

    except Exception as error:

        print(f"❌ STOP ERROR: {error}")
        traceback.print_exc()

        await message.reply_text(
            f"❌ Stop Error: {type(error).__name__}"
        )

    finally:
        stop_in_progress.discard(chat_id)


# ================= END COMMAND =================

async def end_command(client, message):

    await stop_command(client, message)


# ================= QUEUE COMMAND =================

async def queue_command(client, message):

    chat_id = message.chat.id

    queue = queues[chat_id]
    current = current_songs.get(chat_id)

    if not current and not queue:
        await message.reply_text(
            "📭 Queue खाली है।"
        )
        return

    text = "🎵 **Music Queue**\n\n"

    if current:

        text += (
            f"▶️ **अभी बज रहा है:**\n"
            f"{current['title']}\n\n"
        )

    if queue:

        text += "📋 **आने वाले गाने:**\n"

        for number, song in enumerate(
            list(queue)[:10],
            start=1
        ):
            text += f"{number}. {song['title']}\n"

        if len(queue) > 10:
            text += (
                f"\nऔर {len(queue) - 10} गाने बाकी हैं।"
            )

    await message.reply_text(text)


# ================= MAIN =================

async def main():

    global bot, user, calls

    print("🤖 Pandey Music Bot शुरू हो रहा है...")

    bot = Client(
        "PandeyMusicBot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN
    )

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
                "Normal User Account का session आवश्यक है।"
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

            print(
                f"✅ Handler Registered: /{command_name}"
            )

        print("🎵 Pandey Music Bot Online!")
        print("🌐 Multi-Group Queue Enabled!")
        print("⏭️ Failed song पर अगला queued song Try होगा।")

        await asyncio.Event().wait()

    except Exception as error:

        print(
            f"❌ MAIN ERROR: "
            f"{type(error).__name__}: {error}"
        )

        traceback.print_exc()
        raise

    finally:

        print("🛑 Bot बंद हो रहा है...")

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
