import os

# Keep native libraries from creating extra worker threads on small hosts.
os.environ.setdefault('MALLOC_ARENA_MAX', '2')
os.environ.setdefault('OMP_NUM_THREADS', '1')
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
os.environ.setdefault('MKL_NUM_THREADS', '1')

# बॉट शुरू होने से पहले yt-dlp को ऑटोमैटिकली लेटेस्ट वर्जन पर अपडेट करने का प्रयास
try:
    import subprocess
    print("🔄 Checking & Upgrading yt-dlp automatically...")
    subprocess.run(["pip", "install", "--upgrade", "--user", "yt-dlp"], check=False, capture_output=True)
except Exception:
    pass

import gc
import asyncio
import traceback
import time
import requests
import yt_dlp
import shutil
import html
import ctypes

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
    "YOUTUBE_COOKIES_FILE", "/home/container/cookies.txt"
).strip()

YOUTUBE_JS_RUNTIME = os.getenv("YOUTUBE_JS_RUNTIME", "auto").strip().lower()

if not API_ID_TEXT.isdigit() or not API_HASH or not BOT_TOKEN:
    raise ValueError("API_ID, API_HASH और BOT_TOKEN .env में जाँचें।")

API_ID = int(API_ID_TEXT)

# Small-server safety: cap queued songs per chat to avoid unbounded RAM growth.
MAX_QUEUE_SIZE = 20

# एक समय में केवल एक extraction ताकि रैम पर एकसाथ लोड न पड़े
EXTRACTION_SEMAPHORE_LIMIT = 1


# ================= GLOBALS =================

bot = None
user = None
calls = None
QJS_PATH = None
extraction_semaphore = None

queues = defaultdict(deque)
current_songs = {}
now_playing_messages = {}

playback_starting = set()
skip_in_progress = set()
stop_in_progress = set()
failed_chats = set()
chat_generation = defaultdict(int)

http = requests.Session()
# Keep the HTTP connection pool small; only one YouTube API search runs at a time.
http.mount('https://', requests.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=1))

# 256MB RAM सीमा के लिए आक्रामक गार्बेज कलेक्शन थ्रेशोल्ड
gc.set_threshold(50, 2, 2)


# ================= MEMORY =================

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
        return current / 1048576, limit / 1048576, "cgroup-v2"

    current = read_int_file("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    limit = read_int_file("/sys/fs/cgroup/memory/memory.limit_in_bytes")

    if current is not None and limit is not None and 0 < limit < (1 << 60):
        return current / 1048576, limit / 1048576, "cgroup-v1"

    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024, 256.0, "process-rss"
    except Exception:
        pass

    return None, None, "unavailable"


def log_memory(label):
    used, limit, source = memory_snapshot()
    if used is not None:
        print(f"🧠 RAM {label}: {used:.1f}/{limit:.1f} MiB ({source})")


def cleanup_memory(label="cleanup"):
    gc.collect()
    try:
        # पाइथन द्वारा रोकी गई फालतू रैम को सीधे ऑपरेटिंग सिस्टम को वापस लौटाना (256MB RAM के लिए बेहद ज़रूरी)
        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
    except Exception:
        pass
    log_memory(label)


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
        [InlineKeyboardButton("🛑 End", callback_data="music:end")]
    ])


def requester_tag(song):
    user_id = song.get("requester_id")
    name = html.escape(song.get("requester_name") or "Unknown User")

    if user_id:
        return f'<a href="tg://user?id={user_id}">{name}</a>'

    return name


async def send_now_playing(chat_id, song):
    title = html.escape(song.get("title", "Unknown Song"))
    requested_by = requester_tag(song)

    text = (
        "🎵 <b>NOW PLAYING</b>\n\n"
        f"🎧 <b>Song:</b> {title}\n"
        f"👤 <b>Requested by:</b> {requested_by}\n\n"
        "🎛 <b>Music Controls</b>"
    )

    old_message = now_playing_messages.get(chat_id)

    if old_message:
        try:
            await old_message.delete()
        except Exception:
            pass

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

    try:
        response.raise_for_status()
        items = response.json().get("items", [])
    finally:
        response.close()

    print(f"🔎 YouTube Search: {time.perf_counter() - started:.2f}s")

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
        "format": "bestaudio/best",
        "extractor_args": {"youtube": {"player_client": ["android", "web", "mweb"]}},
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,

        "socket_timeout": 15,
        "retries": 3,
        "extractor_retries": 3,
        "fragment_retries": 3,

        "cachedir": False,
        "noprogress": True,
        "ignoreerrors": True,
        "logtostderr": False,
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
        runtime_path = shutil.which("node")
        if runtime_path:
            runtime_name = "node"

    elif YOUTUBE_JS_RUNTIME not in ("", "none", "false", "0"):
        candidate = Path(YOUTUBE_JS_RUNTIME)
        if candidate.is_file():
            runtime_path = str(candidate)
            runtime_name = "quickjs" if candidate.name == "qjs" else "node"

    if runtime_path and runtime_name:
        options["js_runtimes"] = {
            runtime_name: {"path": runtime_path}
        }
        print(f"✅ yt-dlp JS runtime: {runtime_path}")
    else:
        print("⚠️ JS runtime unavailable.")

    ydl = None
    info = None

    try:
        ydl = yt_dlp.YoutubeDL(options)
        info = ydl.extract_info(video_url, download=False)

        if not info:
            raise RuntimeError("YouTube information नहीं मिली।")

        audio_url = None

        if "url" in info and info["url"]:
            audio_url = info["url"]

        if not audio_url and "requested_formats" in info:
            for fmt in info.get("requested_formats", []):
                if fmt.get("url") and fmt.get("acodec") != "none":
                    audio_url = fmt.get("url")
                    break

        if not audio_url and "formats" in info:
            formats = info.get("formats", [])
            for f in formats:
                if f.get("url") and f.get("acodec") != "none" and f.get("vcodec") == "none":
                    audio_url = f.get("url")
                    break
            if not audio_url:
                for f in formats:
                    if f.get("url"):
                        audio_url = f.get("url")
                        break

        if not audio_url:
            raise RuntimeError("YouTube ने कोई भी playable audio stream नहीं दिया।")

        print(
            f"⚡ Extraction Time: "
            f"{time.perf_counter() - started:.2f}s"
        )

        return audio_url

    except Exception as error:
        error_text = str(error)

        if "sign in to confirm you're not a bot" in error_text.casefold():
            raise RuntimeError(
                "YouTube verification माँग रहा है। Cookies जाँचें।"
            ) from error

        raise RuntimeError(
            f"yt-dlp extraction failed: {error_text[:300]}"
        ) from error

    finally:
        info = None
        if ydl is not None:
            try:
                ydl.close()
            except Exception:
                pass
        ydl = None
        cleanup_memory("after extraction")


async def get_audio_url_for_song(chat_id, song):
    async with extraction_semaphore:
        return await asyncio.to_thread(
            extract_audio_url,
            song["url"]
        )


# ================= PLAYBACK =================

async def start_song_in_call(chat_id, song, audio_url):
    # PyTgCalls में सही ऑडियो क्वालिटी और ट्रांसमिशन पैरामीटर सेट करना ताकि वॉइस चैट में आवाज़ लगातार आए
    await calls.play(
        chat_id,
        MediaStream(
            audio_url,
            audio_parameters=AudioQuality.LOW
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
    if chat_id in playback_starting or chat_id in stop_in_progress:
        return False

    if not queues[chat_id]:
        return False

    playback_starting.add(chat_id)

    song = queues[chat_id].popleft()
    generation = chat_generation[chat_id]

    try:
        started = time.perf_counter()

        print(f"🎧 Trying: {song['title']}")

        audio_url = await get_audio_url_for_song(chat_id, song)

        if (
            chat_id in stop_in_progress
            or generation != chat_generation[chat_id]
        ):
            print("ℹ️ Old extraction discarded.")
            return False

        vc_started = time.perf_counter()

        await start_song_in_call(chat_id, song, audio_url)

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
                message_text = (
                    "❌ YouTube verification माँग रहा है।\n"
                    "Cookies और YouTube access जाँचें।"
                )
            else:
                message_text = (
                    "❌ यह गाना शुरू नहीं हो पाया:\n"
                    f"{song.get('title', 'Unknown')}\n\n"
                    f"कारण: {str(error)[:250]}"
                )

            await bot.send_message(chat_id, message_text)

        except Exception:
            pass

        return False

    finally:
        playback_starting.discard(chat_id)
        cleanup_memory("play-next-finished")


# ================= QUEUE =================

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

        for number, song in enumerate(
            islice(queue, 15),
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
        return

    if chat_id not in current_songs or chat_id in failed_chats:
        return

    current_songs.pop(chat_id, None)

    old_message = now_playing_messages.pop(chat_id, None)

    if old_message:
        try:
            await old_message.delete()
        except Exception:
            pass

    if queues[chat_id]:
        await play_next(chat_id)
    else:
        try:
            await bot.send_message(chat_id, "🎵 Queue समाप्त हो गई।")
        except Exception:
            pass


# ================= STOP =================

async def stop_music(chat_id):
    stop_in_progress.add(chat_id)
    chat_generation[chat_id] += 1

    try:
        queues[chat_id].clear()
        current_songs.pop(chat_id, None)
        failed_chats.discard(chat_id)

        old_message = now_playing_messages.pop(chat_id, None)

        if old_message:
            try:
                await old_message.delete()
            except Exception:
                pass

        try:
            await calls.leave_call(chat_id)
        except Exception as error:
            print(f"ℹ️ leave_call: {error}")

        return True

    finally:
        stop_in_progress.discard(chat_id)
        cleanup_memory("after stop")


# ================= SKIP =================

async def skip_music(chat_id):
    if chat_id in skip_in_progress or chat_id in stop_in_progress:
        return False

    if chat_id not in current_songs:
        return False

    skip_in_progress.add(chat_id)
    generation = chat_generation[chat_id]

    try:
        if not queues[chat_id]:
            current_songs.pop(chat_id, None)

            old_message = now_playing_messages.pop(chat_id, None)

            if old_message:
                try:
                    await old_message.delete()
                except Exception:
                    pass

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

        if (
            chat_id in stop_in_progress
            or generation != chat_generation[chat_id]
        ):
            print("ℹ️ Skip extraction discarded.")
            return False

        await start_song_in_call(
            chat_id,
            next_song,
            audio_url
        )

        print(f"✅ SKIP COMPLETE: {chat_id}")

        return True

    except Exception as error:
        print(f"❌ SKIP ERROR: {error}")
        traceback.print_exc()

        try:
            await bot.send_message(
                chat_id,
                f"❌ अगला गाना शुरू नहीं हो पाया: {str(error)[:250]}"
            )
        except Exception:
            pass

        return False

    finally:
        skip_in_progress.discard(chat_id)
        cleanup_memory("after skip")


# ================= CALLBACKS =================

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

    if action in ("pause", "resume"):
        if chat_id not in current_songs:
            await query.answer(
                "अभी कोई गाना नहीं चल रहा।",
                show_alert=True
            )
            return

        try:
            if action == "pause":
                await calls.pause_stream(chat_id)
                await query.answer("⏸️ Pause हो गया।")
            else:
                await calls.resume_stream(chat_id)
                await query.answer("▶️ Resume हो गया।")

        except Exception as error:
            print(f"{action.upper()} ERROR: {error}")
            await query.answer(
                f"{action.title()} नहीं हो पाया।",
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


# ================= COMMANDS =================

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


async def play_command(client, message):
    chat_id = message.chat.id

    print(
        f"🎵 PLAY COMMAND | Chat: {chat_id} | "
        f"Message: {message.id}"
    )

    if len(message.command) < 2:
        await message.reply_text(
            "🎵 लिखें:\n/play गाने का नाम"
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
            await status.edit_text(
                "❌ YouTube पर गाना नहीं मिला।"
            )
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
            song["requester_name"] = "Group User"

        print(
            f"🔎 Total Search: "
            f"{time.perf_counter() - started:.2f}s"
        )

        if (
            MAX_QUEUE_SIZE is not None
            and len(queues[chat_id]) >= MAX_QUEUE_SIZE
        ):
            await status.edit_text(
                f"⚠️ Queue भर गई है। "
                f"अधिकतम {MAX_QUEUE_SIZE} गाने रख सकते हैं।"
            )
            return

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
            f"⚡ Audio तैयार हो रहा है:\n{song['title']}"
        )

        success = await play_next(chat_id)

        if success:
            try:
                await status.delete()
            except Exception:
                pass
        else:
            try:
                await status.edit_text(
                    "❌ गाना शुरू नहीं हो पाया।"
                )
            except Exception:
                pass

    except requests.HTTPError as error:
        print(f"YOUTUBE API ERROR: {error}")

        await status.edit_text(
            "❌ YouTube API Error। API key और quota जाँचें।"
        )

    except Exception as error:
        print(
            f"PLAY ERROR: {type(error).__name__}: {error}"
        )
        traceback.print_exc()

        try:
            await status.edit_text(
                "❌ गाना खोजने या चलाने में समस्या हुई।"
            )
        except Exception:
            pass


async def pause_command(client, message):
    chat_id = message.chat.id

    try:
        if chat_id not in current_songs:
            await message.reply_text(
                "❌ अभी कोई गाना नहीं चल रहा।"
            )
            return

        await calls.pause_stream(chat_id)
        await message.reply_text("⏸️ गाना Pause हो गया।")

    except Exception as error:
        print(f"PAUSE ERROR: {error}")
        await message.reply_text("❌ Pause नहीं हो पाया।")


async def resume_command(client, message):
    chat_id = message.chat.id

    try:
        if chat_id not in current_songs:
            await message.reply_text(
                "❌ अभी कोई गाना नहीं चल रहा।"
            )
            return

        await calls.resume_stream(chat_id)
        await message.reply_text("▶️ गाना Resume हो गया।")

    except Exception as error:
        print(f"RESUME ERROR: {error}")
        await message.reply_text("❌ Resume नहीं हो पाया।")


async def skip_command(client, message):
    chat_id = message.chat.id

    try:
        if chat_id not in current_songs:
            await message.reply_text(
                "❌ अभी कोई गाना नहीं चल रहा।"
            )
            return

        success = await skip_music(chat_id)

        await message.reply_text(
            "⏭️ Skip हो गया।"
            if success
            else "📭 अगला गाना उपलब्ध नहीं है या Skip असफल हुआ।"
        )

    except Exception as error:
        print(f"SKIP ERROR: {error}")
        await message.reply_text("❌ Skip नहीं हो पाया।")


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


async def end_command(client, message):
    await stop_command(client, message)


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

    extraction_semaphore = asyncio.Semaphore(
        EXTRACTION_SEMAPHORE_LIMIT
    )

    print("🤖 Pandey Music Bot Starting...")

    try:
        os.nice(5)
    except (AttributeError, OSError):
        pass

    log_memory("startup")

    QJS_PATH = find_quickjs()

    print("⚡ LOW RAM MODE ENABLED (256MB Optimized)")
    print("🚫 Automatic Preload DISABLED")
    print("🧠 Single Extraction Enabled")
    print("🎧 LOW Audio Quality")
    print(f"📋 Queue: Maximum {MAX_QUEUE_SIZE} songs per chat")
    print("👷 Pyrogram Workers: 1")

    bot = Client(
        "PandeyMusicBot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        workers=1,
        max_concurrent_transmissions=1
    )

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

        if Path(YOUTUBE_COOKIES_FILE).is_file():
            print("🍪 YouTube Cookies Enabled")
        else:
            print("⚠️ YouTube cookies.txt not found")

        log_memory("online")

        await asyncio.Event().wait()

    except Exception as error:
        print(
            f"❌ MAIN ERROR: {type(error).__name__}: {error}"
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

        try:
            http.close()
        except Exception:
            pass

        cleanup_memory("shutdown")

        print("✅ Shutdown Complete")


# ================= AUTO RESTART =================

if __name__ == "__main__":
    RESTART_DELAY_SECONDS = 10

    while True:
        try:
            print("🔄 Starting Bot...")
            asyncio.run(main())

            print("⚠️ Bot stopped normally.")
            break

        except KeyboardInterrupt:
            print("🛑 Manual shutdown.")
            break

        except Exception as error:
            print(
                f"💥 BOT ERROR: {type(error).__name__}: {error}"
            )
            traceback.print_exc()

            print(
                f"🔁 Restarting in "
                f"{RESTART_DELAY_SECONDS} seconds..."
            )

            time.sleep(RESTART_DELAY_SECONDS)
