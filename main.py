import os
import asyncio
import traceback
import time
import requests
import yt_dlp
import shutil
import html
import platform
import subprocess
import hashlib

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
    "YOUTUBE_COOKIES_FILE", "/home/container/cookies.txt"
).strip()
# auto = prefer lightweight QuickJS (qjs), then Node.js; Deno stays disabled.
# Set YOUTUBE_JS_RUNTIME=quickjs, node, or an explicit executable path if needed.
YOUTUBE_JS_RUNTIME = os.getenv("YOUTUBE_JS_RUNTIME", "auto").strip().lower()

DENO_DIR = Path("/home/container/.deno-runtime").resolve()
DENO_BIN = DENO_DIR / "deno"

if not API_ID_TEXT.isdigit() or not API_HASH or not BOT_TOKEN:
    raise ValueError("API_ID, API_HASH और BOT_TOKEN .env में जाँचें।")

API_ID = int(API_ID_TEXT)


# ================= LIGHTWEIGHT QUICKJS-NG RUNTIME =================

QJS_DIR = Path("/home/container/.local/bin")
QJS_BIN = QJS_DIR / "qjs"


def _check_qjs(path):
    """सिर्फ वही qjs स्वीकार करें जो वास्तव में executable हो।"""
    try:
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def setup_quickjs():
    """
    QuickJS-NG का छोटा prebuilt executable डाउनलोड करें।
    Node/Deno या कोई भारी Python package install नहीं होता।
    """
    # पहले server पर मौजूद qjs को जाँचें।
    existing = shutil.which("qjs")
    if existing and _check_qjs(existing):
        print(f"✅ QuickJS Runtime Found: {existing}")
        return existing

    if QJS_BIN.exists() and _check_qjs(QJS_BIN):
        print(f"✅ QuickJS-NG पहले से मौजूद है: {QJS_BIN}")
        return str(QJS_BIN)

    if platform.system().lower() != "linux":
        print("⚠️ QuickJS auto-install केवल Linux server के लिए है।")
        return None

    machine = platform.machine().lower()
    asset_names = {
        "x86_64": "qjs-linux-x86_64",
        "amd64": "qjs-linux-x86_64",
        "aarch64": "qjs-linux-aarch64",
        "arm64": "qjs-linux-aarch64",
        "armv7l": "qjs-linux-armv7",
    }
    asset_name = asset_names.get(machine)

    if not asset_name:
        print(f"⚠️ QuickJS के लिए unsupported architecture: {machine}")
        return None

    temp_path = QJS_DIR / "qjs.download"
    try:
        QJS_DIR.mkdir(parents=True, exist_ok=True)

        print("⬇️ हल्का QuickJS-NG runtime डाउनलोड हो रहा है...")

        api_response = requests.get(
            "https://api.github.com/repos/quickjs-ng/quickjs/releases/latest",
            headers={"Accept": "application/vnd.github+json"},
            timeout=(10, 20)
        )
        api_response.raise_for_status()
        release = api_response.json()

        asset = next(
            (item for item in release.get("assets", [])
             if item.get("name") == asset_name),
            None
        )
        if not asset or not asset.get("browser_download_url"):
            raise RuntimeError(f"GitHub release में {asset_name} नहीं मिला।")

        expected_digest = asset.get("digest") or ""
        if expected_digest and not expected_digest.startswith("sha256:"):
            raise RuntimeError("GitHub asset का SHA-256 digest उपलब्ध नहीं है।")

        download = requests.get(
            asset["browser_download_url"],
            stream=True,
            timeout=(10, 60)
        )
        download.raise_for_status()

        total = 0
        digest = hashlib.sha256()
        with open(temp_path, "wb") as file:
            for chunk in download.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > 15 * 1024 * 1024:
                    raise RuntimeError("QuickJS binary का आकार सीमा से अधिक है।")
                digest.update(chunk)
                file.write(chunk)

        if total < 100_000:
            raise RuntimeError("डाउनलोड की गई QuickJS file अधूरी है।")

        if expected_digest:
            actual_digest = "sha256:" + digest.hexdigest()
            if actual_digest != expected_digest:
                raise RuntimeError("QuickJS SHA-256 verification असफल हुई।")

        os.chmod(temp_path, 0o755)
        os.replace(temp_path, QJS_BIN)
        os.chmod(QJS_BIN, 0o755)

        if not _check_qjs(QJS_BIN):
            raise RuntimeError("QuickJS executable start नहीं हुआ।")

        print(f"✅ QuickJS-NG Runtime Ready: {QJS_BIN}")
        return str(QJS_BIN)

    except Exception as error:
        print(f"⚠️ QuickJS auto-install नहीं हो पाया: {type(error).__name__}: {error}")
        print("ℹ️ Bot चलेगा, लेकिन YouTube के कुछ audio formats नहीं मिल सकते।")
        return None

    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass


# ================= GLOBALS =================

bot = None
user = None
calls = None
DENO_PATH = None
QJS_PATH = None

queues = defaultdict(deque)
current_songs = {}
now_playing_messages = {}

playback_starting = set()
skip_in_progress = set()
stop_in_progress = set()
failed_chats = set()

# पूरे bot में केवल एक अगला audio URL cache और एक preload task रखें।
# इससे कई groups में एक साथ preload चलकर RAM बढ़ने का जोखिम घटता है।
preloaded_audio = None  # (chat_id, song, audio_url)
preload_task = None
preload_chat_id = None

# पूरे bot में एक समय पर केवल एक audio extraction.
extraction_semaphore = asyncio.Semaphore(1)

http = requests.Session()


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
    started = time.perf_counter()
    options = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": False,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 12,
        "retries": 0,
        "extractor_retries": 0,
        "fragment_retries": 0,
        "cachedir": False,
    }

    if YOUTUBE_COOKIES_FILE and Path(YOUTUBE_COOKIES_FILE).is_file():
        options["cookiefile"] = YOUTUBE_COOKIES_FILE
        print("🍪 YouTube cookies file enabled.")
    else:
        print("⚠️ cookies.txt नहीं मिली; YouTube verification आ सकता है।")

    runtime_path = None
    runtime_name = None

    if YOUTUBE_JS_RUNTIME == "auto":
        # QuickJS-NG का executable startup पर validate किया जाता है।
        runtime_path = QJS_PATH or shutil.which("qjs")
        runtime_name = "quickjs" if runtime_path and _check_qjs(runtime_path) else None
        if not runtime_name:
            # node नाम की Python launcher file को runtime न मानें।
            node_candidate = shutil.which("node")
            if node_candidate:
                try:
                    node_check = subprocess.run(
                        [node_candidate, "--version"],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    if node_check.returncode == 0 and node_check.stdout.strip().startswith("v"):
                        runtime_path = node_candidate
                        runtime_name = "node"
                except Exception:
                    pass
    elif YOUTUBE_JS_RUNTIME in ("quickjs", "qjs"):
        runtime_path = shutil.which("qjs")
        runtime_name = "quickjs" if runtime_path else None
    elif YOUTUBE_JS_RUNTIME == "node":
        runtime_path = shutil.which("node")
        runtime_name = "node" if runtime_path else None
    elif YOUTUBE_JS_RUNTIME:
        candidate = Path(YOUTUBE_JS_RUNTIME)
        if candidate.is_file():
            runtime_path = str(candidate)
            # Custom path is treated as QuickJS only when its filename is qjs;
            # otherwise retain the previous custom-runtime behavior.
            runtime_name = "quickjs" if candidate.name == "qjs" else "custom"

    if runtime_path and runtime_name:
        options["js_runtimes"] = {runtime_name: {"path": runtime_path}}
        print(f"✅ yt-dlp JS runtime ({runtime_name}): {runtime_path}")
    else:
        print("⚠️ JS runtime नहीं मिला (qjs/Node.js); कुछ YouTube videos extract नहीं होंगे।")

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(video_url, download=False)
    except yt_dlp.utils.DownloadError as error:
        error_text = str(error)
        if "sign in to confirm you're not a bot" in error_text.casefold():
            raise RuntimeError(
                "YouTube verification माँग रहा है; server पर valid cookies.txt लगाएँ।"
            ) from error
        raise RuntimeError(
            f"yt-dlp extraction असफल: {error_text[:700]}"
        ) from error

    if not info:
        raise RuntimeError("YouTube audio information नहीं मिली।")

    audio_url = info.get("url")
    if not audio_url:
        audio_formats = [
            item for item in (info.get("formats") or [])
            if item.get("url") and item.get("acodec") not in (None, "none")
        ]
        if audio_formats:
            audio_formats.sort(
                key=lambda item: (item.get("abr") or 0, item.get("tbr") or 0),
                reverse=True,
            )
            audio_url = audio_formats[0]["url"]

    if not audio_url:
        raise RuntimeError("Direct Audio URL उपलब्ध नहीं है।")

    print(f"⚡ Audio Extraction Time: {time.perf_counter() - started:.2f}s")
    return audio_url


# ================= SMART PRELOAD (ONE GLOBAL SLOT) =================

async def _preload_next_song(chat_id, song):
    global preloaded_audio, preload_task, preload_chat_id
    this_task = asyncio.current_task()
    try:
        async with extraction_semaphore:
            audio_url = await asyncio.to_thread(extract_audio_url, song["url"])

        if (
            chat_id not in stop_in_progress
            and queues[chat_id]
            and queues[chat_id][0] is song
            and preloaded_audio is None
        ):
            preloaded_audio = (chat_id, song, audio_url)
            print(f"⚡ SMART PRELOAD READY: {song.get('title')}")
        else:
            print("ℹ️ पुराना preload result discard किया।")
    except Exception as error:
        print(
            f"⚠️ SMART PRELOAD FAILED: {type(error).__name__}: {error}"
        )
    finally:
        if preload_task is this_task:
            preload_task = None
            preload_chat_id = None


def schedule_preload(chat_id):
    global preload_task, preload_chat_id
    if chat_id in stop_in_progress or chat_id not in current_songs:
        return
    if not queues[chat_id] or preloaded_audio is not None:
        return
    if preload_task is not None and not preload_task.done():
        return

    song = queues[chat_id][0]
    preload_chat_id = chat_id
    preload_task = asyncio.create_task(_preload_next_song(chat_id, song))


async def get_audio_url_for_song(chat_id, song):
    global preloaded_audio
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
        await task
        if (
            preloaded_audio is not None
            and preloaded_audio[0] == chat_id
            and preloaded_audio[1] is song
        ):
            audio_url = preloaded_audio[2]
            preloaded_audio = None
            return audio_url

    async with extraction_semaphore:
        return await asyncio.to_thread(extract_audio_url, song["url"])


# ================= PLAYBACK =================

async def start_song_in_call(chat_id, song, audio_url):
    """मौजूदा VC call को छोड़े बिना stream चलाएँ/बदलें।"""
    await calls.play(
        chat_id,
        MediaStream(audio_url, AudioQuality.MEDIUM)
    )

    current_songs[chat_id] = song
    failed_chats.discard(chat_id)

    try:
        await send_now_playing(chat_id, song)
    except Exception as notify_error:
        print(
            "⚠️ Now Playing message भेजने में समस्या हुई: "
            f"{type(notify_error).__name__}: {notify_error}"
        )
        traceback.print_exc()



# ================= PLAY NEXT =================

async def play_next(chat_id):
    if chat_id in playback_starting or chat_id in stop_in_progress:
        return False

    if not queues[chat_id]:
        return False

    playback_starting.add(chat_id)
    song = queues[chat_id].popleft()

    try:
        started = time.perf_counter()
        print(f"🎧 Trying: {song['title']}")

        # अगर यह गाना पहले से background में तैयार है तो URL तुरंत मिलेगा।
        audio_input = await get_audio_url_for_song(chat_id, song)
        vc_started = time.perf_counter()

        # Active VC में stream replace होगा; call leave नहीं की जाती।
        await start_song_in_call(chat_id, song, audio_input)
        schedule_preload(chat_id)

        print(f"📡 VC Startup/Stream Switch Time: {time.perf_counter() - vc_started:.2f}s")
        print(f"🎵 Total Playback Start Time: {time.perf_counter() - started:.2f}s")
        return True

    except Exception as error:
        current_songs.pop(chat_id, None)
        failed_chats.add(chat_id)
        print(f"❌ Song Failed: {song.get('title')}")
        print(f"Error: {type(error).__name__}: {error}")
        traceback.print_exc()

        try:
            if "verification" in str(error).casefold():
                text = (
                    "❌ YouTube ने इस वीडियो पर verification माँगा है।\n"
                    "यह गाना अभी नहीं चल पाया।"
                )
            else:
                text = (
                    "❌ यह गाना शुरू नहीं हो पाया:\n"
                    f"{song.get('title', 'Unknown')}"
                )
            await bot.send_message(chat_id, text)
        except Exception:
            traceback.print_exc()
        return False

    finally:
        playback_starting.discard(chat_id)

# ================= QUEUE TEXT =================

def make_queue_text(chat_id):
    current = current_songs.get(chat_id)
    queue = queues[chat_id]

    text = "🎵 MUSIC QUEUE\n\n"

    if current:
        text += f"▶️ अभी बज रहा है:\n{current['title']}\n\n"

    if queue:
        text += "📋 आने वाले गाने:\n"

        for number, song in enumerate(list(queue)[:15], start=1):
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
        print(f"ℹ️ StreamEnded ignored during manual transition: {chat_id}")
        return

    # Old/stale StreamEnded event को नया गाना रोकने न दें।
    if chat_id not in current_songs:
        print(f"ℹ️ Stale StreamEnded ignored: {chat_id}")
        return

    if chat_id in failed_chats:
        return

    current_songs.pop(chat_id, None)
    now_playing_messages.pop(chat_id, None)

    if queues[chat_id]:
        await play_next(chat_id)
    else:
        await bot.send_message(chat_id, "🎵 Queue समाप्त हो गई।")


# ================= STOP FUNCTION =================

async def stop_music(chat_id):
    stop_in_progress.add(chat_id)
    try:
        queues[chat_id].clear()
        current_songs.pop(chat_id, None)
        failed_chats.discard(chat_id)
        now_playing_messages.pop(chat_id, None)
        global preloaded_audio
        if preloaded_audio and preloaded_audio[0] == chat_id:
            preloaded_audio = None
        # चल रही to_thread extraction को cancel नहीं करते: thread रुकता नहीं,
        # इसलिए उसे पूरा होने दें; stop_in_progress के कारण URL cache नहीं होगा।

        await calls.leave_call(chat_id)
        return True
    finally:
        stop_in_progress.discard(chat_id)


# ================= SKIP FUNCTION =================

async def skip_music(chat_id):
    if chat_id in skip_in_progress or chat_id in stop_in_progress:
        return False

    if chat_id not in current_songs:
        return False

    skip_in_progress.add(chat_id)
    try:
        # अगला गाना पहले से preload हो तो वही URL इस्तेमाल होगा;
        # अन्यथा extraction semaphore के जरिए सामान्य extraction होगी।
        if not queues[chat_id]:
            current_songs.pop(chat_id, None)
            now_playing_messages.pop(chat_id, None)
            # Queue खाली होने पर भी Assistant VC में बना रहेगा।
            try:
                await calls.pause_stream(chat_id)
            except Exception as pause_error:
                print(f"⚠️ Empty queue pause warning: {pause_error}")
            return True

        next_song = queues[chat_id].popleft()
        print(f"⏭️ SKIP TO: {next_song.get('title')}")

        # पहले से तैयार URL इस्तेमाल करें; जरूरत पड़ने पर ही extraction करें।
        audio_url = await get_audio_url_for_song(chat_id, next_song)

        # महत्वपूर्ण: यहाँ leave_call() नहीं है। calls.play active VC की
        # stream को बदलता है, इसलिए Assistant call से बाहर नहीं जाता।
        await start_song_in_call(chat_id, next_song, audio_url)
        schedule_preload(chat_id)
        print(f"✅ SKIP COMPLETE; VC KEPT: {chat_id}")
        return True

    except Exception as error:
        print(f"❌ SKIP ERROR: {type(error).__name__}: {error}")
        traceback.print_exc()
        return False

    finally:
        skip_in_progress.discard(chat_id)


# ================= BUTTON CALLBACK =================

async def music_callback(client, query):
    if not query.message:
        await query.answer("यह बटन उपलब्ध नहीं है।", show_alert=True)
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
            await bot.send_message(chat_id, "⏹️ Music बंद और Queue साफ।")

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
            "🎵 गाना चलाने के लिए लिखें:\n/play गाने का नाम"
        )
        return

    query = " ".join(message.command[1:]).strip()

    status = await message.reply_text(
        "🔎 YouTube पर गाना खोज रहा हूँ..."
    )

    try:
        started = time.perf_counter()
        song = await asyncio.to_thread(search_youtube, query)

        if not song:
            await status.edit_text("❌ YouTube पर गाना नहीं मिला।")
            return

        requester = message.from_user

        if requester:
            song["requester_id"] = requester.id
            song["requester_name"] = (
                requester.first_name or requester.username or "User"
            )
        else:
            song["requester_id"] = None
            song["requester_name"] = "Group Admin"

        print(
            f"🔎 Total Search Time: "
            f"{time.perf_counter() - started:.2f}s"
        )

        queues[chat_id].append(song)
        schedule_preload(chat_id)

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
            f"⚡ YouTube audio तैयार किया जा रहा है:\n{song['title']}"
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
        print(f"❌ PLAY ERROR: {type(error).__name__}: {error}")
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
            await message.reply_text("❌ अभी कोई गाना नहीं चल रहा है।")
            return

        await calls.pause_stream(chat_id)
        await message.reply_text("⏸️ गाना Pause कर दिया गया है।")

    except Exception as error:
        print(f"PAUSE ERROR: {error}")
        await message.reply_text("❌ Pause नहीं हो पाया।")


# ================= RESUME COMMAND =================

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
    await message.reply_text(make_queue_text(message.chat.id))


# ================= COMMAND AUTO DELETE =================

def command_handler_with_auto_delete(callback):
    """हर registered command का user message पहले delete करने की कोशिश करें।"""
    async def wrapped(client, message):
        try:
            await message.delete()
            print(f"🗑️ Command auto-deleted: {message.text or message.caption}")
        except Exception as delete_error:
            # Bot के पास Delete Messages permission न हो तो command फिर भी चले।
            print(
                "⚠️ Command delete नहीं हो पाया (bot permission जाँचें): "
                f"{type(delete_error).__name__}: {delete_error}"
            )

        return await callback(client, message)

    return wrapped


# ================= MAIN =================

async def main():
    global bot, user, calls, DENO_PATH, QJS_PATH

    print("🤖 Pandey Music Bot शुरू हो रहा है...")

    # 256 MB RAM: Deno/Node नहीं; छोटा QuickJS-NG इस्तेमाल करें।
    DENO_PATH = None
    QJS_PATH = setup_quickjs()
    print("⚠️ LOW-RAM MODE: Deno disabled; one-song global preload only.")

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
        print("🎛 Inline Music Controls Enabled!")
        print("⚡ One-Song Global Smart Preload Enabled!")
        print("🧠 Single Audio Extraction Mode Enabled!")
        print("👤 Clickable Requester Profile Tag Enabled!")

        await asyncio.Event().wait()

    except Exception as error:
        print(f"❌ MAIN ERROR: {type(error).__name__}: {error}")
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
    RESTART_DELAY_SECONDS = 10

    while True:
        try:
            print("🔄 Bot process शुरू हो रहा है...")
            asyncio.run(main())
            print("⚠️ Main loop सामान्य रूप से बंद हुआ; 10 सेकंड में restart होगा।")

        except KeyboardInterrupt:
            print("🛑 Manual shutdown मिला; restart नहीं किया जाएगा।")
            break

        except Exception as error:
            print(
                f"💥 Bot crash/error: {type(error).__name__}: {error}"
            )
            traceback.print_exc()
            print(
                f"🔁 {RESTART_DELAY_SECONDS} सेकंड बाद automatic restart..."
            )

        time.sleep(RESTART_DELAY_SECONDS)
