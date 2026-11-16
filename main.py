import os
import asyncio
import traceback
import time
import requests
import yt_dlp
import shutil
import html

from pathlib import Path
from collections import defaultdict, deque
from itertools import islice

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

YOUTUBE_JS_RUNTIME = os.getenv(
    "YOUTUBE_JS_RUNTIME",
    "auto"
).strip().lower()

if not API_ID_TEXT.isdigit() or not API_HASH or not BOT_TOKEN:
    raise ValueError("API_ID, API_HASH और BOT_TOKEN .env में जाँचें।")

API_ID = int(API_ID_TEXT)


# ================= QUICKJS =================

def find_quickjs():

    existing = shutil.which("qjs")

    if existing:
        print(f"✅ QuickJS Found: {existing}")
        return existing

    local_qjs = Path("/home/container/.local/bin/qjs")

    if local_qjs.is_file() and os.access(local_qjs, os.X_OK):
        print(f"✅ QuickJS Found: {local_qjs}")
        return str(local_qjs)

    print("⚠️ QuickJS नहीं मिला।")
    return None


# ================= GLOBALS =================

bot = None
user = None
calls = None

QJS_PATH = None

queues = defaultdict(deque)
current_songs = {}
now_playing_messages = {}

playback_starting = set()
skip_in_progress = set()
stop_in_progress = set()
failed_chats = set()

# केवल एक global preload

preloaded_audio = None
preload_task = None
preload_chat_id = None
preload_extracting = False

extraction_semaphore = None

PRELOAD_DELAY_SECONDS = 30

PRELOAD_MAX_MEMORY_RATIO = 0.35
PRELOAD_MAX_MEMORY_MB = 105

MAX_QUEUE_SIZE = 50

http = requests.Session()


# ================= MEMORY MONITOR =================

def read_int_file(path):

    try:
        value = Path(path).read_text().strip()

        if value.isdigit():
            return int(value)

    except Exception:
        pass

    return None


def memory_snapshot():

    current = read_int_file("/sys/fs/cgroup/memory.current")
    limit = read_int_file("/sys/fs/cgroup/memory.max")

    if current is not None and limit is not None and limit > 0:
        return (
            current / (1024 * 1024),
            limit / (1024 * 1024),
            "cgroup-v2"
        )

    current = read_int_file(
        "/sys/fs/cgroup/memory/memory.usage_in_bytes"
    )

    limit = read_int_file(
        "/sys/fs/cgroup/memory/memory.limit_in_bytes"
    )

    if current is not None and limit is not None and 0 < limit < (1 << 60):
        return (
            current / (1024 * 1024),
            limit / (1024 * 1024),
            "cgroup-v1"
        )

    try:
        for line in Path("/proc/self/status").read_text().splitlines():

            if line.startswith("VmRSS:"):
                rss = int(line.split()[1]) / 1024
                return rss, 256.0, "process-rss"

    except Exception:
        pass

    return None, None, "unavailable"


def log_memory(label):

    used, limit, source = memory_snapshot()

    if used is not None:
        print(
            f"🧠 RAM {label}: "
            f"{used:.1f}/{limit:.1f} MB ({source})"
        )


def preload_memory_is_safe():

    used, limit, source = memory_snapshot()

    if used is None or limit is None:
        print("⚠️ RAM information unavailable. Preload skipped.")
        return False

    ratio = used / limit

    print(
        f"🧠 Preload RAM: "
        f"{used:.1f}/{limit:.1f} MB ({ratio:.0%})"
    )

    if used >= PRELOAD_MAX_MEMORY_MB:
        return False

    if ratio >= PRELOAD_MAX_MEMORY_RATIO:
        return False

    return True


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

    now_playing_messages[chat_id] = sent


# ================= YOUTUBE SEARCH =================

def search_youtube(query):

    if not YOUTUBE_API_KEY:
        raise ValueError("YOUTUBE_API_KEY नहीं मिली।")

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
        f"🔎 YouTube Search: "
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

    started = time.perf_counter()

    log_memory("before extraction")

    options = {
        # पहले audio-only, उपलब्ध न हो तो best format
        "format": "bestaudio/best",

        "quiet": True,
        "no_warnings": True,

        "noplaylist": True,
        "skip_download": True,

        "socket_timeout": 10,

        "retries": 0,
        "extractor_retries": 0,
        "fragment_retries": 0,

        "cachedir": False,
        "noprogress": True,

        "concurrent_fragment_downloads": 1
    }

    if YOUTUBE_COOKIES_FILE and Path(YOUTUBE_COOKIES_FILE).is_file():
        options["cookiefile"] = YOUTUBE_COOKIES_FILE
        print("🍪 YouTube cookies enabled.")
    else:
        print("⚠️ cookies.txt नहीं मिली।")

    runtime_path = None
    runtime_name = None

    if YOUTUBE_JS_RUNTIME == "auto":
        runtime_path = QJS_PATH

        if runtime_path:
            runtime_name = "quickjs"

    elif YOUTUBE_JS_RUNTIME in ("quickjs", "qjs"):
        runtime_path = QJS_PATH or shutil.which("qjs")

        if runtime_path:
            runtime_name = "quickjs"

    elif YOUTUBE_JS_RUNTIME == "node":
        print("⚠️ LOW RAM MODE: Node disabled.")

    elif YOUTUBE_JS_RUNTIME:
        candidate = Path(YOUTUBE_JS_RUNTIME)

        if candidate.is_file():
            runtime_path = str(candidate)
            runtime_name = "quickjs" if candidate.name == "qjs" else "custom"

    if runtime_path and runtime_name:
        options["js_runtimes"] = {
            runtime_name: {
                "path": runtime_path
            }
        }

        print(f"✅ yt-dlp JS runtime: {runtime_path}")

    else:
        print("⚠️ JS runtime unavailable.")

    info = None

    try:

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(video_url, download=False)

        if not info:
            raise RuntimeError("YouTube information नहीं मिली।")

        audio_url = info.get("url")

        if not audio_url:

            formats = info.get("formats") or []

            best_format = None
            best_score = (-1, -1)

            for item in formats:

                if not item.get("url"):
                    continue

                if item.get("acodec") in (None, "none"):
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
            raise RuntimeError("Audio URL नहीं मिली।")

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
            f"yt-dlp failed: {error_text[:500]}"
        ) from error

    finally:
        info = None
        log_memory("after extraction")


# ================= SMART PRELOAD =================

async def _preload_next_song(chat_id, song):

    global preload_task
    global preload_chat_id
    global preload_extracting
    global preloaded_audio

    this_task = asyncio.current_task()

    try:

        await asyncio.sleep(PRELOAD_DELAY_SECONDS)

        if not preload_memory_is_safe():
            print("⚠️ RAM कम है। Preload skipped.")
            return

        if (
            chat_id in stop_in_progress
            or chat_id not in current_songs
            or not queues[chat_id]
            or queues[chat_id][0] is not song
            or preloaded_audio is not None
        ):
            return

        async with extraction_semaphore:

            if chat_id in stop_in_progress:
                return

            preload_extracting = True

            audio_url = await asyncio.to_thread(
                extract_audio_url,
                song["url"]
            )

        song_is_next = (
            bool(queues[chat_id])
            and queues[chat_id][0] is song
        )

        song_is_starting = (
            chat_id in playback_starting
            or chat_id in skip_in_progress
        )

        if (
            chat_id not in stop_in_progress
            and chat_id in current_songs
            and (song_is_next or song_is_starting)
            and preloaded_audio is None
        ):

            preloaded_audio = (chat_id, song, audio_url)

            print(f"⚡ PRELOAD READY: {song.get('title')}")

        else:
            print("ℹ️ Old preload discarded.")

    except asyncio.CancelledError:
        raise

    except Exception as error:
        print(
            f"⚠️ PRELOAD ERROR: "
            f"{type(error).__name__}: {error}"
        )

    finally:

        if preload_task is this_task:
            preload_task = None
            preload_chat_id = None
            preload_extracting = False


def schedule_preload(chat_id):

    global preload_task
    global preload_chat_id

    if chat_id in stop_in_progress:
        return

    if chat_id not in current_songs:
        return

    if not queues[chat_id] or preloaded_audio is not None:
        return

    if preload_task is not None and not preload_task.done():
        return

    song = queues[chat_id][0]
    preload_chat_id = chat_id

    preload_task = asyncio.create_task(
        _preload_next_song(chat_id, song)
    )

    print(f"⏳ Preload scheduled: {PRELOAD_DELAY_SECONDS}s")


async def get_audio_url_for_song(chat_id, song):

    global preloaded_audio
    global preload_task

    if (
        preloaded_audio is not None
        and preloaded_audio[0] == chat_id
        and preloaded_audio[1] is song
    ):

        audio_url = preloaded_audio[2]
        preloaded_audio = None

        return audio_url

    task = preload_task

    if (
        task is not None
        and not task.done()
        and preload_chat_id == chat_id
    ):

        if not preload_extracting:

            task.cancel()

            try:
                await task
            except asyncio.CancelledError:
                pass

        else:

            try:
                await task
            except asyncio.CancelledError:
                pass

        if (
            preloaded_audio is not None
            and preloaded_audio[0] == chat_id
            and preloaded_audio[1] is song
        ):

            audio_url = preloaded_audio[2]
            preloaded_audio = None

            return audio_url

    async with extraction_semaphore:
        return await asyncio.to_thread(
            extract_audio_url,
            song["url"]
        )


# ================= PLAYBACK =================

async def start_song_in_call(chat_id, song, audio_url):

    await calls.play(
        chat_id,
        MediaStream(
            audio_url,
            AudioQuality.LOW
        )
    )

    current_songs[chat_id] = song
    failed_chats.discard(chat_id)

    try:
        await send_now_playing(chat_id, song)

    except Exception as error:
        print(f"⚠️ Now Playing error: {error}")


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

        audio_url = await get_audio_url_for_song(chat_id, song)

        vc_started = time.perf_counter()

        await start_song_in_call(chat_id, song, audio_url)

        schedule_preload(chat_id)

        print(
            f"📡 VC Switch: "
            f"{time.perf_counter() - vc_started:.2f}s"
        )

        print(
            f"🎵 Total Start: "
            f"{time.perf_counter() - started:.2f}s"
        )

        return True

    except Exception as error:

        current_songs.pop(chat_id, None)
        failed_chats.add(chat_id)

        print(f"❌ Song Failed: {song.get('title')}")
        print(f"Error: {error}")

        traceback.print_exc()

        try:

            if "verification" in str(error).casefold():
                text = (
                    "❌ YouTube verification माँग रहा है।\n"
                    "यह गाना नहीं चल पाया।"
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


# ================= QUEUE TEXT =================

def make_queue_text(chat_id):

    current = current_songs.get(chat_id)
    queue = queues[chat_id]

    text = "🎵 MUSIC QUEUE\n\n"

    if current:
        text += (
            "▶️ अभी बज रहा है:\n"
            f"{current['title']}\n\n"
        )

    if queue:

        text += "📋 आने वाले गाने:\n"

        for number, song in enumerate(islice(queue, 15), start=1):
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
        return

    if chat_id not in current_songs:
        return

    if chat_id in failed_chats:
        return

    current_songs.pop(chat_id, None)
    now_playing_messages.pop(chat_id, None)

    if queues[chat_id]:
        await play_next(chat_id)

    else:
        await bot.send_message(
            chat_id,
            "🎵 Queue समाप्त हो गई।"
        )


# ================= STOP =================

async def stop_music(chat_id):

    global preloaded_audio
    global preload_task

    stop_in_progress.add(chat_id)

    try:

        queues[chat_id].clear()
        current_songs.pop(chat_id, None)
        failed_chats.discard(chat_id)
        now_playing_messages.pop(chat_id, None)

        if (
            preloaded_audio is not None
            and preloaded_audio[0] == chat_id
        ):
            preloaded_audio = None

        if (
            preload_task is not None
            and not preload_task.done()
            and preload_chat_id == chat_id
            and not preload_extracting
        ):

            preload_task.cancel()

            try:
                await preload_task
            except asyncio.CancelledError:
                pass

        await calls.leave_call(chat_id)

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
            except Exception:
                pass

            return True

        next_song = queues[chat_id].popleft()

        print(f"⏭️ SKIP TO: {next_song['title']}")

        audio_url = await get_audio_url_for_song(
            chat_id,
            next_song
        )

        await start_song_in_call(
            chat_id,
            next_song,
            audio_url
        )

        schedule_preload(chat_id)

        print(f"✅ SKIP COMPLETE: {chat_id}")

        return True

    except Exception as error:

        print(f"❌ SKIP ERROR: {error}")
        traceback.print_exc()

        return False

    finally:
        skip_in_progress.discard(chat_id)


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
                await query.answer(
                    "अभी कोई गाना नहीं चल रहा।",
                    show_alert=True
                )
                return

            await calls.pause_stream(chat_id)
            await query.answer("⏸️ Pause हो गया।")

        except Exception as error:

            print(f"PAUSE ERROR: {error}")

            await query.answer(
                "Pause नहीं हो पाया।",
                show_alert=True
            )

        return

    if action == "resume":

        try:

            if chat_id not in current_songs:
                await query.answer(
                    "अभी कोई गाना नहीं चल रहा।",
                    show_alert=True
                )
                return

            await calls.resume_stream(chat_id)
            await query.answer("▶️ Resume हो गया।")

        except Exception as error:

            print(f"RESUME ERROR: {error}")

            await query.answer(
                "Resume नहीं हो पाया।",
                show_alert=True
            )

        return

    if action == "skip":

        if chat_id not in current_songs:
            await query.answer(
                "अभी कोई गाना नहीं चल रहा।",
                show_alert=True
            )
            return

        await query.answer("⏭️ अगला गाना शुरू हो रहा है।")

        success = await skip_music(chat_id)

        if not success and not queues[chat_id]:
            await bot.send_message(chat_id, "📭 Queue खाली है।")

        return

    if action in ("stop", "end"):

        try:

            await stop_music(chat_id)

            await query.answer("⏹️ Playback बंद हो गया।")

            await bot.send_message(
                chat_id,
                "⏹️ Music बंद और Queue साफ।"
            )

        except Exception as error:

            print(f"STOP ERROR: {error}")

            await query.answer(
                "Stop नहीं हो पाया।",
                show_alert=True
            )

        return

    if action == "queue":

        await query.answer("📋 Queue भेज दी गई।")

        await bot.send_message(
            chat_id,
            make_queue_text(chat_id)
        )

        return

    await query.answer("Unknown button.", show_alert=True)


# ================= START =================

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
        f"🎵 PLAY COMMAND | "
        f"Chat: {chat_id} | Message: {message.id}"
    )

    if len(message.command) < 2:
        await message.reply_text("🎵 लिखें:\n/play गाने का नाम")
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
            f"🔎 Total Search: "
            f"{time.perf_counter() - started:.2f}s"
        )

        if len(queues[chat_id]) >= MAX_QUEUE_SIZE:
            await status.edit_text(
                f"⚠️ Queue भर गई है। अधिकतम "
                f"{MAX_QUEUE_SIZE} गाने रख सकते हैं।"
            )
            return

        queues[chat_id].append(song)

        if (
            chat_id in current_songs
            or chat_id in playback_starting
            or chat_id in skip_in_progress
        ):

            schedule_preload(chat_id)

            await status.edit_text(
                f"✅ Queue में जोड़ा गया:\n"
                f"{song['title']}\n"
                f"स्थान: {len(queues[chat_id])}"
            )

            return

        await status.edit_text(
            f"⚡ Audio तैयार हो रहा है:\n"
            f"{song['title']}"
        )

        success = await play_next(chat_id)

        if success:
            await status.delete()
        else:
            await status.edit_text("❌ गाना शुरू नहीं हो पाया।")

    except requests.HTTPError as error:

        print(f"YOUTUBE API ERROR: {error}")

        await status.edit_text(
            "❌ YouTube API Error। API key और quota जाँचें।"
        )

    except Exception as error:

        print(
            f"PLAY ERROR: "
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
            await message.reply_text("❌ अभी कोई गाना नहीं चल रहा।")
            return

        await calls.pause_stream(chat_id)
        await message.reply_text("⏸️ गाना Pause हो गया।")

    except Exception as error:

        print(f"PAUSE ERROR: {error}")
        await message.reply_text("❌ Pause नहीं हो पाया।")


# ================= RESUME =================

async def resume_command(client, message):

    chat_id = message.chat.id

    try:

        if chat_id not in current_songs:
            await message.reply_text("❌ अभी कोई गाना नहीं चल रहा।")
            return

        await calls.resume_stream(chat_id)
        await message.reply_text("▶️ गाना Resume हो गया।")

    except Exception as error:

        print(f"RESUME ERROR: {error}")
        await message.reply_text("❌ Resume नहीं हो पाया।")


# ================= SKIP COMMAND =================

async def skip_command(client, message):

    chat_id = message.chat.id

    try:

        if chat_id not in current_songs:
            await message.reply_text("❌ अभी कोई गाना नहीं चल रहा।")
            return

        success = await skip_music(chat_id)

        if success:
            await message.reply_text("⏭️ Skip हो गया।")
        else:
            await message.reply_text(
                "📭 अगला गाना उपलब्ध नहीं है या Skip असफल हुआ।"
            )

    except Exception as error:

        print(f"SKIP ERROR: {error}")
        await message.reply_text("❌ Skip नहीं हो पाया।")


# ================= STOP COMMAND =================

async def stop_command(client, message):

    chat_id = message.chat.id

    try:

        await stop_music(chat_id)

        await message.reply_text(
            "⏹️ Playback बंद और Queue साफ कर दी गई।"
        )

    except Exception as error:

        print(f"STOP ERROR: {error}")
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
                f"🗑️ Command deleted: "
                f"{message.text or message.caption}"
            )

        except Exception:
            pass

        return await callback(client, message)

    return wrapped


# ================= MAIN =================

async def main():

    global bot, user, calls
    global QJS_PATH, extraction_semaphore

    extraction_semaphore = asyncio.Semaphore(1)

    print("🤖 Pandey Music Bot Starting...")

    log_memory("startup")

    # केवल पहले से मौजूद QuickJS खोजें; डाउनलोड नहीं होगा
    QJS_PATH = find_quickjs()

    print("⚠️ LOW RAM MODE ENABLED")
    print("⚡ Single Global Preload")
    print("🧠 Single Extraction")
    print("🎧 Audio Quality LOW")

    bot = Client(
        "PandeyMusicBot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        workers=1,
        max_concurrent_transmissions=1
    )

    # PyTgCalls voice chat के लिए normal Telegram user session जरूरी है
    user = Client(
        "FreshLogin",
        api_id=API_ID,
        api_hash=API_HASH,
        workers=1,
        max_concurrent_transmissions=1
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
        print("✅ User Client Started")

        me = await user.get_me()

        print(f"👤 Account: {me.first_name}")
        print(f"🆔 Account ID: {me.id}")

        if me.is_bot:
            raise RuntimeError(
                "FreshLogin में Normal User Account आवश्यक है।"
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

            print(f"✅ /{command_name} Registered")

        bot.add_handler(
            CallbackQueryHandler(
                music_callback,
                filters.regex(r"^music:")
            )
        )

        print("✅ Music Buttons Registered")
        print("🎵 Pandey Music Bot ONLINE")
        print("🍪 YouTube Cookies Enabled")
        print("⚡ Smart Preload Enabled")
        print("🧠 RAM Protection Enabled")

        log_memory("online")

        await asyncio.Event().wait()

    except Exception as error:

        print(
            f"❌ MAIN ERROR: "
            f"{type(error).__name__}: {error}"
        )

        traceback.print_exc()
        raise

    finally:

        print("🛑 Bot Shutting Down...")

        try:
            if calls:
                await calls.stop()
        except Exception:
            pass

        try:
            if user and user.is_connected:
                await user.stop()
        except Exception:
            pass

        try:
            if bot and bot.is_connected:
                await bot.stop()
        except Exception:
            pass

        print("✅ Shutdown Complete")


# ================= AUTO RESTART =================

if __name__ == "__main__":

    RESTART_DELAY_SECONDS = 10

    while True:

        try:

            print("🔄 Starting Bot...")

            asyncio.run(main())

            print("⚠️ Bot stopped normally.")

        except KeyboardInterrupt:

            print("🛑 Manual shutdown.")
            break

        except Exception as error:

            print(
                f"💥 BOT ERROR: "
                f"{type(error).__name__}: {error}"
            )

            traceback.print_exc()

            print(
                f"🔁 Restarting in "
                f"{RESTART_DELAY_SECONDS} seconds..."
            )

        time.sleep(RESTART_DELAY_SECONDS)
