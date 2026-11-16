import os
import asyncio
import traceback
import time
import requests
import yt_dlp
import shutil
import html
import gc

from pathlib import Path
from collections import defaultdict, deque
from dotenv import load_dotenv

from pyrogram import Client, filters, enums
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from pytgcalls import PyTgCalls, filters as call_filters
from pytgcalls.types import MediaStream, AudioQuality, StreamEnded


# ================= CONFIG =================

load_dotenv("/home/container/.env")

API_ID_TEXT = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()

YOUTUBE_COOKIES_FILE = os.getenv(
    "YOUTUBE_COOKIES_FILE",
    "/home/container/cookies.txt"
).strip()

if not API_ID_TEXT.isdigit() or not API_HASH or not BOT_TOKEN:
    raise ValueError("API_ID, API_HASH और BOT_TOKEN .env में जाँचें।")

API_ID = int(API_ID_TEXT)


# ================= LOW RAM QUICKJS =================

QJS_BIN = Path("/home/container/.local/bin/qjs")
QJS_PATH = None


def find_quickjs():
    """केवल पहले से मौजूद QuickJS इस्तेमाल करें; Startup पर डाउनलोड नहीं।"""
    candidates = [
        shutil.which("qjs"),
        str(QJS_BIN) if QJS_BIN.is_file() else None
    ]

    for candidate in candidates:
        if not candidate:
            continue

        try:
            result = __import__("subprocess").run(
                [candidate, "--version"],
                capture_output=True,
                text=True,
                timeout=3
            )

            if result.returncode == 0:
                print(f"✅ QuickJS मिला: {candidate}")
                return candidate

        except Exception:
            pass

    print("⚠️ QuickJS नहीं मिला; yt-dlp बिना JS runtime के चलेगा।")
    return None


# ================= GLOBALS =================

bot = None
user = None
calls = None

queues = defaultdict(deque)
current_songs = {}
now_playing_messages = {}

playback_starting = set()
skip_in_progress = set()
stop_in_progress = set()
failed_chats = set()

# हर restart पर नया semaphore बनेगा।
extraction_semaphore = None

http = requests.Session()

# Background preload जानबूझकर पूरी तरह बंद है।
PRELOAD_ENABLED = False


# ================= MEMORY LOG =================

def log_memory():
    """Linux पर process की वर्तमान RAM usage दिखाएँ।"""
    try:
        with open("/proc/self/status", "r") as file:
            for line in file:
                if line.startswith("VmRSS:"):
                    print(f"🧠 Bot RAM Usage: {line.strip()}")
                    return
    except Exception:
        pass


def cleanup_memory():
    try:
        gc.collect()
    except Exception:
        pass


# ================= BUTTONS =================

def music_buttons():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶️ Resume", callback_data="music:resume"),
            InlineKeyboardButton("⏸️ Pause", callback_data="music:pause")
        ],
        [
            InlineKeyboardButton("⏭️ Skip", callback_data="music:skip"),
            InlineKeyboardButton("⏹️ Stop", callback_data="music:stop")
        ],
        [
            InlineKeyboardButton("📋 Queue", callback_data="music:queue"),
            InlineKeyboardButton("🎵 Play", callback_data="music:play")
        ],
        [
            InlineKeyboardButton("🛑 End", callback_data="music:end")
        ]
    ])


def requester_tag(song):
    user_id = song.get("requester_id")
    name = song.get("requester_name") or "Unknown User"
    safe_name = html.escape(name)

    if user_id:
        return f'<a href="tg://user?id={user_id}">{safe_name}</a>'

    return safe_name


async def send_now_playing(chat_id, song):
    title = html.escape(song.get("title", "Unknown Song"))
    requested_by = requester_tag(song)

    text = (
        "🎵 <b>NOW PLAYING</b>\n\n"
        f"🎧 <b>Song:</b> {title}\n"
        f"👤 <b>Requested by:</b> {requested_by}\n\n"
        "🎛 <b>Music Controls</b>"
    )

    sent = await bot.send_message(
        chat_id,
        text,
        reply_markup=music_buttons(),
        parse_mode=enums.ParseMode.HTML
    )

    old_message = now_playing_messages.get(chat_id)
    now_playing_messages[chat_id] = sent

    if old_message:
        try:
            await old_message.delete()
        except Exception:
            pass


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
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "source": "youtube"
    }


# ================= AUDIO EXTRACTION =================

def extract_audio_url(video_url):
    cleanup_memory()
    started = time.perf_counter()

    options = {
        "format": "bestaudio[acodec!=none]/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 10,
        "retries": 0,
        "extractor_retries": 0,
        "fragment_retries": 0,
        "cachedir": False,
        "extract_flat": False,
        "concurrent_fragment_downloads": 1,
        "noprogress": True,
    }

    # Cookies
    cookies_path = Path(YOUTUBE_COOKIES_FILE).expanduser()

    try:
        if cookies_path.is_file() and cookies_path.stat().st_size > 0:
            options["cookiefile"] = str(cookies_path)
            print("🍪 YouTube cookies file enabled.")
        else:
            print(f"⚠️ Cookies file नहीं मिली: {cookies_path}")
    except Exception as error:
        print(f"⚠️ Cookies access error: {error}")

    # QuickJS केवल पहले से installed होने पर।
    if QJS_PATH and Path(QJS_PATH).is_file():
        options["js_runtimes"] = {
            "quickjs": {"path": QJS_PATH}
        }
        print("✅ QuickJS runtime enabled.")

    info = None

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                video_url,
                download=False
            )

        if not info:
            raise RuntimeError("YouTube audio information नहीं मिली।")

        audio_url = info.get("url")

        if not audio_url:
            formats = info.get("formats") or []
            best_format = None
            best_score = (-1, -1)

            for item in formats:
                if (
                    not item.get("url")
                    or item.get("acodec") in (None, "none")
                ):
                    continue

                score = (
                    item.get("abr") or 0,
                    item.get("tbr") or 0
                )

                if score > best_score:
                    best_score = score
                    best_format = item

            if best_format:
                audio_url = best_format.get("url")

        if not audio_url:
            raise RuntimeError("Direct Audio URL उपलब्ध नहीं है।")

        print(
            f"⚡ Extraction Time: "
            f"{time.perf_counter() - started:.2f}s"
        )

        return audio_url

    except yt_dlp.utils.DownloadError as error:
        error_text = str(error)

        if "sign in to confirm you're not a bot" in error_text.casefold():
            raise RuntimeError(
                "YouTube verification माँग रहा है। Cookies जाँचें।"
            ) from error

        raise RuntimeError(
            f"yt-dlp extraction असफल: {error_text[:500]}"
        ) from error

    finally:
        if isinstance(info, dict):
            info.clear()

        info = None
        cleanup_memory()


async def get_audio_url(video_url):
    """पूरे Bot में एक समय पर केवल एक extraction।"""
    async with extraction_semaphore:
        return await asyncio.to_thread(
            extract_audio_url,
            video_url
        )


# ================= PLAYBACK =================

async def start_song_in_call(chat_id, song, audio_url):
    await calls.play(
        chat_id,
        MediaStream(
            audio_url,
            AudioQuality.MEDIUM
        )
    )

    current_songs[chat_id] = song
    failed_chats.discard(chat_id)

    try:
        await send_now_playing(chat_id, song)
    except Exception as error:
        print(f"⚠️ Now Playing message error: {error}")

    log_memory()


# ================= PLAY NEXT =================

async def play_next(chat_id):
    if chat_id in playback_starting:
        return False

    if chat_id in stop_in_progress:
        return False

    if not queues[chat_id]:
        return False

    playback_starting.add(chat_id)
    song = queues[chat_id].popleft()

    try:
        started = time.perf_counter()

        print(f"🎧 Trying: {song['title']}")

        audio_url = await get_audio_url(song["url"])

        await start_song_in_call(
            chat_id,
            song,
            audio_url
        )

        print(
            f"🎵 Playback Start Time: "
            f"{time.perf_counter() - started:.2f}s"
        )

        return True

    except Exception as error:
        current_songs.pop(chat_id, None)
        failed_chats.add(chat_id)

        print(
            f"❌ Song Failed: "
            f"{type(error).__name__}: {error}"
        )

        traceback.print_exc()

        try:
            if "verification" in str(error).casefold():
                text = (
                    "❌ YouTube verification माँग रहा है।\n"
                    "यह गाना अभी नहीं चल पाया।"
                )
            else:
                text = (
                    "❌ यह गाना शुरू नहीं हो पाया:\n"
                    f"{song.get('title', 'Unknown')}"
                )

            await bot.send_message(chat_id, text)

        except Exception:
            pass

        return False

    finally:
        playback_starting.discard(chat_id)
        cleanup_memory()


# ================= QUEUE TEXT =================

def make_queue_text(chat_id):
    current = current_songs.get(chat_id)
    queue = queues[chat_id]

    text = "🎵 MUSIC QUEUE\n\n"

    if current:
        text += (
            f"▶️ अभी बज रहा है:\n"
            f"{current['title']}\n\n"
        )

    if queue:
        text += "📋 आने वाले गाने:\n"

        for number, song in enumerate(
            list(queue)[:15],
            start=1
        ):
            text += f"{number}. {song['title']}\n"

        if len(queue) > 15:
            text += f"\nऔर {len(queue) - 15} गाने बाकी हैं।"

    if not current and not queue:
        text = "📭 Queue खाली है।"

    return text


# ================= STREAM END =================

async def stream_ended_handler(_, update: StreamEnded):
    chat_id = update.chat_id

    print(f"🏁 STREAM ENDED: {chat_id}")

    if chat_id in skip_in_progress or chat_id in stop_in_progress:
        print("ℹ️ Manual transition के दौरान StreamEnded ignore किया।")
        return

    if chat_id not in current_songs:
        print("ℹ️ पुराना StreamEnded event ignore किया।")
        return

    if chat_id in failed_chats:
        return

    current_songs.pop(chat_id, None)
    now_playing_messages.pop(chat_id, None)

    if queues[chat_id]:
        await play_next(chat_id)
    else:
        await bot.send_message(chat_id, "🎵 Queue समाप्त हो गई।")

    cleanup_memory()


# ================= STOP =================

async def stop_music(chat_id):
    stop_in_progress.add(chat_id)

    try:
        queues[chat_id].clear()
        current_songs.pop(chat_id, None)
        failed_chats.discard(chat_id)
        now_playing_messages.pop(chat_id, None)

        await calls.leave_call(chat_id)

        cleanup_memory()
        return True

    finally:
        stop_in_progress.discard(chat_id)


# ================= SKIP =================

async def skip_music(chat_id):
    if chat_id in skip_in_progress:
        return False

    if chat_id in stop_in_progress:
        return False

    if chat_id not in current_songs:
        return False

    skip_in_progress.add(chat_id)

    try:
        if not queues[chat_id]:
            current_songs.pop(chat_id, None)
            now_playing_messages.pop(chat_id, None)

            try:
                await calls.pause_stream(chat_id)
            except Exception as error:
                print(f"⚠️ Pause warning: {error}")

            return True

        next_song = queues[chat_id].popleft()

        print(f"⏭️ SKIP TO: {next_song['title']}")

        audio_url = await get_audio_url(next_song["url"])

        await start_song_in_call(
            chat_id,
            next_song,
            audio_url
        )

        print("✅ SKIP COMPLETE")
        return True

    except Exception as error:
        print(f"❌ SKIP ERROR: {error}")
        traceback.print_exc()
        return False

    finally:
        skip_in_progress.discard(chat_id)
        cleanup_memory()


# ================= BUTTON CALLBACK =================

async def music_callback(client, query):
    if not query.message:
        await query.answer(
            "यह बटन उपलब्ध नहीं है।",
            show_alert=True
        )
        return

    chat_id = query.message.chat.id
    action = query.data.split(":", 1)[1]

    if action == "play":
        await query.answer(
            "गाना चलाने के लिए /play गाने का नाम भेजें।",
            show_alert=True
        )
        return

    if action == "pause":
        try:
            if chat_id not in current_songs:
                await query.answer("अभी कोई गाना नहीं चल रहा।", show_alert=True)
                return

            await calls.pause_stream(chat_id)
            await query.answer("⏸️ गाना Pause हो गया।")

        except Exception as error:
            print(f"BUTTON PAUSE ERROR: {error}")
            await query.answer("Pause नहीं हो पाया।", show_alert=True)

        return

    if action == "resume":
        try:
            if chat_id not in current_songs:
                await query.answer("अभी कोई गाना नहीं चल रहा।", show_alert=True)
                return

            await calls.resume_stream(chat_id)
            await query.answer("▶️ गाना Resume हो गया।")

        except Exception as error:
            print(f"BUTTON RESUME ERROR: {error}")
            await query.answer("Resume नहीं हो पाया।", show_alert=True)

        return

    if action == "skip":
        try:
            if chat_id not in current_songs:
                await query.answer("अभी कोई गाना नहीं चल रहा।", show_alert=True)
                return

            await query.answer("⏭️ अगला गाना शुरू हो रहा है।")

            success = await skip_music(chat_id)

            if not success and not queues[chat_id]:
                await bot.send_message(chat_id, "📭 Queue खाली है।")

        except Exception as error:
            print(f"BUTTON SKIP ERROR: {error}")
            traceback.print_exc()

        return

    if action in ("stop", "end"):
        try:
            await stop_music(chat_id)

            await query.answer("⏹️ Playback बंद कर दिया गया।")
            await bot.send_message(
                chat_id,
                "⏹️ Music बंद और Queue साफ।"
            )

        except Exception as error:
            print(f"BUTTON STOP ERROR: {error}")
            await query.answer("Stop नहीं हो पाया।", show_alert=True)

        return

    if action == "queue":
        await query.answer("📋 Queue भेज दी गई।")
        await bot.send_message(chat_id, make_queue_text(chat_id))
        return

    await query.answer("Unknown button.", show_alert=True)


# ================= START COMMAND =================

async def start_command(client, message):
    await message.reply_text(
        "🎵 Pandey Music Bot ऑनलाइन है!\n\n"
        "Commands:\n"
        "/play गाने का नाम\n"
        "/pause - Pause\n"
        "/resume - Resume\n"
        "/skip - अगला गाना\n"
        "/stop - Playback बंद\n"
        "/end - Playback समाप्त\n"
        "/queue - Queue देखें\n\n"
        "🎛 Music Controls के लिए Now Playing Buttons इस्तेमाल करें।"
    )


# ================= PLAY COMMAND =================

async def play_command(client, message):
    chat_id = message.chat.id

    print(
        f"🎵 PLAY COMMAND RECEIVED | "
        f"Chat: {chat_id} | Message: {message.id}"
    )

    if len(message.command) < 2:
        await message.reply_text(
            "🎵 गाना चलाने के लिए लिखें:\n"
            "/play गाने का नाम"
        )
        return

    query = " ".join(message.command[1:]).strip()

    status = await message.reply_text(
        "🔎 YouTube पर गाना खोज रहा हूँ..."
    )

    try:
        started = time.perf_counter()

        song = await asyncio.to_thread(
            search_youtube,
            query
        )

        if not song:
            await status.edit_text("❌ YouTube पर गाना नहीं मिला।")
            return

        requester = message.from_user

        if requester:
            song["requester_id"] = requester.id
            song["requester_name"] = (
                requester.first_name
                or requester.username
                or "User"
            )
        else:
            song["requester_id"] = None
            song["requester_name"] = "Group Admin"

        print(
            f"🔎 Total Search Time: "
            f"{time.perf_counter() - started:.2f}s"
        )

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
            f"⚡ Audio तैयार किया जा रहा है:\n"
            f"{song['title']}"
        )

        success = await play_next(chat_id)

        if success:
            await status.delete()
        else:
            await status.edit_text(
                "❌ गाना शुरू नहीं हो पाया। Console में Error देखें।"
            )

    except requests.HTTPError as error:
        print(f"❌ YOUTUBE API HTTP ERROR: {error}")
        traceback.print_exc()

        await status.edit_text(
            "❌ YouTube Search API में समस्या है। API key और quota जाँचें।"
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
            pass


# ================= PAUSE =================

async def pause_command(client, message):
    chat_id = message.chat.id

    try:
        if chat_id not in current_songs:
            await message.reply_text("❌ अभी कोई गाना नहीं चल रहा है।")
            return

        await calls.pause_stream(chat_id)
        await message.reply_text("⏸️ गाना Pause कर दिया गया है।")

    except Exception as error:
        print(f"PAUSE ERROR: {error}")
        await message.reply_text("❌ Pause नहीं हो पाया।")


# ================= RESUME =================

async def resume_command(client, message):
    chat_id = message.chat.id

    try:
        if chat_id not in current_songs:
            await message.reply_text("❌ अभी कोई गाना नहीं चल रहा है।")
            return

        await calls.resume_stream(chat_id)
        await message.reply_text("▶️ गाना Resume हो गया है।")

    except Exception as error:
        print(f"RESUME ERROR: {error}")
        await message.reply_text("❌ Resume नहीं हो पाया।")


# ================= SKIP COMMAND =================

async def skip_command(client, message):
    chat_id = message.chat.id

    try:
        if chat_id not in current_songs:
            await message.reply_text("❌ अभी कोई गाना नहीं चल रहा है।")
            return

        success = await skip_music(chat_id)

        if success:
            await message.reply_text("⏭️ Skip हो गया।")
        else:
            await message.reply_text("📭 Queue खाली है।")

    except Exception as error:
        print(f"SKIP ERROR: {error}")
        traceback.print_exc()
        await message.reply_text("❌ Skip नहीं हो पाया।")


# ================= STOP COMMAND =================

async def stop_command(client, message):
    chat_id = message.chat.id

    try:
        await stop_music(chat_id)

        await message.reply_text(
            "⏹️ Playback बंद और Queue साफ कर दी गई है।"
        )

    except Exception as error:
        print(f"STOP ERROR: {error}")
        traceback.print_exc()
        await message.reply_text("❌ Stop नहीं हो पाया।")


# ================= END COMMAND =================

async def end_command(client, message):
    await stop_command(client, message)


# ================= QUEUE COMMAND =================

async def queue_command(client, message):
    await message.reply_text(
        make_queue_text(message.chat.id)
    )


# ================= AUTO DELETE =================

def command_handler_with_auto_delete(callback):
    async def wrapped(client, message):
        try:
            await message.delete()

            print(
                f"🗑️ Command auto-deleted: "
                f"{message.text or message.caption}"
            )

        except Exception as error:
            print(f"⚠️ Command delete error: {error}")

        return await callback(client, message)

    return wrapped


# ================= MAIN =================

async def main():
    global bot, user, calls, QJS_PATH, extraction_semaphore

    print("🤖 Pandey Music Bot शुरू हो रहा है...")

    cleanup_memory()

    # हर नए event loop पर नया semaphore।
    extraction_semaphore = asyncio.Semaphore(1)

    QJS_PATH = find_quickjs()

    print("🧠 LOW RAM MODE ENABLED")
    print("🚫 Background Preload: DISABLED")
    print("🔒 Single Extraction: ENABLED")
    print("🍪 YouTube Cookies Support: ENABLED")

    cookies_path = Path(YOUTUBE_COOKIES_FILE).expanduser()

    try:
        if cookies_path.is_file() and cookies_path.stat().st_size > 0:
            print(f"✅ Cookies File Found: {cookies_path}")
        else:
            print(f"⚠️ Cookies File Missing: {cookies_path}")
    except Exception as error:
        print(f"⚠️ Cookies File Check Error: {error}")

    log_memory()

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
                "FreshLogin में Normal User Account का session आवश्यक है।"
            )

        await calls.start()
        print("✅ PyTgCalls Started")

        calls.on_update(
            call_filters.stream_end()
        )(stream_ended_handler)

        for command_name, callback in handlers:
            bot.add_handler(
                MessageHandler(
                    command_handler_with_auto_delete(callback),
                    filters.command(command_name)
                )
            )

            print(f"✅ Handler Registered: /{command_name}")

        bot.add_handler(
            CallbackQueryHandler(
                music_callback,
                filters.regex(r"^music:")
            )
        )

        print("✅ Music Buttons Registered")
        print("🎵 Pandey Music Bot Online!")
        print("🌐 YouTube Search Enabled!")
        print("⚡ No Background Preload")
        print("🧠 Single Extraction Mode")
        print("🍪 YouTube Cookies Enabled")

        log_memory()

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

        cleanup_memory()


# ================= AUTO RESTART =================

if __name__ == "__main__":
    RESTART_DELAY_SECONDS = 10

    while True:
        try:
            print("🔄 Bot process शुरू हो रहा है...")

            asyncio.run(main())

            print(
                "⚠️ Main loop बंद हुआ; "
                "10 सेकंड में restart होगा।"
            )

        except KeyboardInterrupt:
            print("🛑 Manual shutdown मिला।")
            break

        except Exception as error:
            print(
                f"💥 Bot Error: "
                f"{type(error).__name__}: {error}"
            )
            traceback.print_exc()

            print(
                f"🔁 {RESTART_DELAY_SECONDS} सेकंड बाद restart..."
            )

        cleanup_memory()
        time.sleep(RESTART_DELAY_SECONDS)
