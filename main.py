import os
import resource

# Disable Linux core dumps before importing/starting child processes.
# Deno inherits this limit, preventing huge `core` files on crashes.
try:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
except (AttributeError, OSError, ValueError):
    pass

import asyncio
import traceback
import time
import requests
import yt_dlp
import shutil
import html
import platform
import zipfile
import subprocess
import stat

from pathlib import Path
from collections import defaultdict, deque
from dotenv import load_dotenv

from pyrogram import Client, filters, enums
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from pytgcalls import PyTgCalls, filters as call_filters
from pytgcalls.types import MediaStream, AudioQuality, StreamEnded


# ================= CONFIG =================

BASE_DIR = Path("/home/container")
load_dotenv(str(BASE_DIR / ".env"))

API_ID_TEXT = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()
AUTO_INSTALL_DENO = os.getenv("AUTO_INSTALL_DENO", "0").strip().lower() in ("1", "true", "yes")
MAX_QUEUE_SIZE = max(1, int(os.getenv("MAX_QUEUE_SIZE", "10")))

# 96 MB is a compromise: 64 MB caused V8 heap OOM in the supplied log.
# On a ~294 MiB cgroup, this is still not a guarantee against host OOM kills.
DENO_V8_HEAP_MB = max(80, min(96, int(os.getenv("DENO_V8_HEAP_MB", "96"))))
DENO_V8_FLAGS = f"--max-old-space-size={DENO_V8_HEAP_MB}"
os.environ["DENO_V8_FLAGS"] = DENO_V8_FLAGS
os.environ.setdefault("DENO_NO_UPDATE_CHECK", "1")

DENO_DIR = (BASE_DIR / ".deno-runtime").resolve()
DENO_BIN = DENO_DIR / "deno"

if not API_ID_TEXT.isdigit() or not API_HASH or not BOT_TOKEN:
    raise ValueError("API_ID, API_HASH और BOT_TOKEN .env में जाँचें।")
API_ID = int(API_ID_TEXT)


def disable_core_dumps():
    """Re-apply zero core limit; child processes inherit it."""
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
        print(f"🛡️ Core dump limit: {soft}/{hard} (0 = disabled)")
    except (AttributeError, OSError, ValueError) as error:
        print(f"⚠️ Core dump limit set नहीं हो सका: {error}")


def remove_old_core_dumps():
    """Remove only known core-dump filenames in the container root."""
    removed = 0
    try:
        for item in BASE_DIR.iterdir():
            if item.is_symlink() or not item.is_file():
                continue
            name = item.name
            if name == "core" or (name.startswith("core.") and name[5:].replace(".", "").isdigit()):
                try:
                    size_mb = item.stat().st_size / (1024 * 1024)
                    item.unlink()
                    removed += 1
                    print(f"🧹 Old core dump deleted: {name} ({size_mb:.1f} MiB)")
                except OSError as error:
                    print(f"⚠️ Core dump delete नहीं हुआ ({name}): {error}")
    except OSError as error:
        print(f"⚠️ Core dump cleanup warning: {error}")
    if not removed:
        print("✅ कोई पुरानी core dump file नहीं मिली।")


# ================= DENO AUTO INSTALLER =================

def setup_deno():
    """Prefer system/local Deno; download only when explicitly enabled."""
    existing = shutil.which("deno")
    if existing:
        try:
            result = subprocess.run(
                [existing, "--version"], capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0:
                print(f"✅ System Deno Ready: {existing}")
                return existing
            print("⚠️ System Deno मिला, लेकिन चल नहीं पाया; local Deno जाँच रहे हैं।")
        except Exception as error:
            print(f"⚠️ System Deno जाँच असफल: {error}")

    if DENO_BIN.is_file():
        try:
            os.chmod(DENO_BIN, 0o755)
            result = subprocess.run(
                [str(DENO_BIN), "--version"], capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0:
                os.environ["PATH"] = str(DENO_DIR) + os.pathsep + os.environ.get("PATH", "")
                print("✅ पहले से डाउनलोड किया हुआ Deno मिला।")
                print(f"✅ Deno Runtime Ready: {DENO_BIN}")
                return str(DENO_BIN)
            print("⚠️ Local Deno मिला, लेकिन चल नहीं पाया।")
        except Exception as error:
            print(f"⚠️ Existing Deno इस्तेमाल नहीं हो सका: {error}")

    if not AUTO_INSTALL_DENO:
        print("⚠️ Deno नहीं मिला और AUTO_INSTALL_DENO बंद है।")
        return None

    if platform.system().lower() != "linux":
        print("❌ यह installer Linux server के लिए बनाया गया है।")
        return None

    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        asset_name = "deno-x86_64-unknown-linux-gnu.zip"
    elif machine in ("aarch64", "arm64"):
        asset_name = "deno-aarch64-unknown-linux-gnu.zip"
    else:
        print(f"❌ Unsupported server architecture: {machine}")
        return None

    zip_path = DENO_DIR / "deno-download.zip"
    try:
        DENO_DIR.mkdir(parents=True, exist_ok=True)
        download_url = "https://github.com/denoland/deno/releases/latest/download/" + asset_name
        print("⬇️ Official Deno download शुरू हो रहा है...")
        response = requests.get(download_url, stream=True, timeout=(15, 120))
        response.raise_for_status()
        with open(zip_path, "wb") as file:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    file.write(chunk)
        with zipfile.ZipFile(zip_path, "r") as archive:
            if "deno" not in archive.namelist():
                raise RuntimeError("ZIP में Deno executable नहीं मिला।")
            archive.extract("deno", path=DENO_DIR)
        os.chmod(DENO_BIN, 0o755)
        result = subprocess.run(
            [str(DENO_BIN), "--version"], capture_output=True, text=True, timeout=20
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr or "Deno executable नहीं चल पाया।")
        os.environ["PATH"] = str(DENO_DIR) + os.pathsep + os.environ.get("PATH", "")
        print("✅ Deno Successfully Installed!")
        print(result.stdout.strip())
        return str(DENO_BIN)
    except Exception as error:
        print(f"❌ Deno Auto Installation असफल: {type(error).__name__}: {error}")
        return None
    finally:
        try:
            if zip_path.exists():
                zip_path.unlink()
        except OSError:
            pass


# ================= GLOBALS =================

bot = None
user = None
calls = None
DENO_PATH = None

queues = defaultdict(deque)
current_songs = {}
now_playing_messages = {}

playback_starting = set()
skip_in_progress = set()
stop_in_progress = set()
failed_chats = set()

# One extraction at a time to reduce concurrent RAM spikes.
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
        [InlineKeyboardButton("🛑 End", callback_data="music:end")]
    ])


def requester_tag(song):
    user_id = song.get("requester_id")
    name = song.get("requester_name") or "Unknown User"
    safe_name = html.escape(name)
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>' if user_id else safe_name


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
        chat_id, text, reply_markup=music_buttons(), parse_mode=enums.ParseMode.HTML
    )
    now_playing_messages[chat_id] = sent


# ================= YOUTUBE SEARCH =================

def search_youtube(query):
    if not YOUTUBE_API_KEY:
        raise ValueError("YOUTUBE_API_KEY .env में नहीं मिली।")
    response = http.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={
            "part": "snippet", "q": query, "type": "video",
            "maxResults": 1, "key": YOUTUBE_API_KEY
        },
        timeout=(5, 10)
    )
    response.raise_for_status()
    items = response.json().get("items", [])
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
    # Use best available audio; fall back to best available media format.
    options = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": False,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 12,
        "retries": 1,
        "extractor_retries": 1,
        "fragment_retries": 0,
        "file_access_retries": 0,
        "cachedir": False,
        "concurrent_fragment_downloads": 1,
        "logger": None,
    }

    cookie_file = BASE_DIR / "cookies.txt"
    if cookie_file.is_file() and cookie_file.stat().st_size > 0:
        options["cookiefile"] = str(cookie_file)
        print("🍪 YouTube cookies file detected; yt-dlp will use it.")
    else:
        print("⚠️ cookies.txt नहीं मिली; YouTube verification आ सकता है।")

    deno_path = DENO_PATH or shutil.which("deno")
    if deno_path:
        options["js_runtimes"] = {"deno": {"path": deno_path}}
        print(f"✅ yt-dlp Deno Runtime: {deno_path}")
        print(f"🧠 Deno V8 heap limit: {DENO_V8_HEAP_MB} MB")
    else:
        print("⚠️ Deno उपलब्ध नहीं है। yt-dlp fallback mode में चलेगा।")

    # DENO_V8_FLAGS is set once at startup and inherited by Deno subprocess.
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(video_url, download=False)
    except yt_dlp.utils.DownloadError as error:
        error_text = str(error)
        lowered = error_text.casefold()
        print(f"❌ YouTube extraction: {error_text[:500]}")
        if "sign in to confirm you're not a bot" in lowered:
            raise RuntimeError("YouTube ने इस वीडियो के लिए verification माँगा है।") from error
        if "requested format is not available" in lowered or "only images are available" in lowered:
            raise RuntimeError(
                "YouTube ने audio format नहीं दिया। Deno/EJS challenge solve नहीं हुआ; "
                "नीचे दिए log में Deno heap या YouTube verification देखें।"
            ) from error
        raise RuntimeError(f"yt-dlp extraction असफल: {error_text[:700]}") from error

    if not info:
        raise RuntimeError("YouTube audio information नहीं मिली।")
    audio_url = info.get("url")
    if not audio_url:
        formats = info.get("formats") or []
        audio_formats = [
            item for item in formats
            if item.get("url") and item.get("acodec") not in (None, "none")
        ]
        if audio_formats:
            audio_formats.sort(
                key=lambda item: (item.get("abr") or 0, item.get("tbr") or 0),
                reverse=True
            )
            audio_url = audio_formats[0]["url"]
    if not audio_url:
        raise RuntimeError("Direct Audio URL उपलब्ध नहीं है।")
    return audio_url


# ================= PLAYBACK =================

async def start_song_in_call(chat_id, song, audio_url):
    await calls.play(chat_id, MediaStream(audio_url, AudioQuality.HIGH))
    current_songs[chat_id] = song
    failed_chats.discard(chat_id)
    try:
        await send_now_playing(chat_id, song)
    except Exception as notify_error:
        print(f"⚠️ Now Playing message समस्या: {type(notify_error).__name__}: {notify_error}")
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
        print(f"🎧 Trying: {song['title']}")
        async with extraction_semaphore:
            audio_input = await asyncio.to_thread(extract_audio_url, song["url"])
        await start_song_in_call(chat_id, song, audio_input)
        return True
    except Exception as error:
        current_songs.pop(chat_id, None)
        failed_chats.add(chat_id)
        print(f"❌ Song Failed: {song.get('title')}")
        print(f"Error: {type(error).__name__}: {error}")
        traceback.print_exc()
        try:
            message = (
                "❌ YouTube ने verification माँगा है; यह गाना अभी नहीं चल पाया।"
                if "verification" in str(error).casefold()
                else f"❌ यह गाना शुरू नहीं हो पाया:\n{song.get('title', 'Unknown')}"
            )
            await bot.send_message(chat_id, message)
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
        if not queues[chat_id]:
            current_songs.pop(chat_id, None)
            now_playing_messages.pop(chat_id, None)
            try:
                await calls.pause_stream(chat_id)
            except Exception as pause_error:
                print(f"⚠️ Empty queue pause warning: {pause_error}")
            return True

        next_song = queues[chat_id].popleft()
        print(f"⏭️ SKIP TO: {next_song.get('title')}")
        skip_started = time.perf_counter()
        try:
            await calls.leave_call(chat_id)
            print(f"🧹 Previous VC stream released: {chat_id}")
        except Exception as leave_error:
            print(f"⚠️ Previous VC release warning: {leave_error}")
        current_songs.pop(chat_id, None)
        now_playing_messages.pop(chat_id, None)
        async with extraction_semaphore:
            audio_url = await asyncio.to_thread(extract_audio_url, next_song["url"])
        await start_song_in_call(chat_id, next_song, audio_url)
        print(
            f"✅ SKIP COMPLETE; VC RESTARTED: {chat_id} | "
            f"Time: {time.perf_counter() - skip_started:.2f}s"
        )
        return True
    except Exception as error:
        print(f"❌ SKIP ERROR: {type(error).__name__}: {error}")
        traceback.print_exc()
        try:
            if "next_song" in locals():
                queues[chat_id].appendleft(next_song)
            await bot.send_message(
                chat_id,
                "❌ अगला गाना शुरू नहीं हो पाया। Queue में रखा है; "
                "कृपया कुछ देर बाद /play से दोबारा कोशिश करें।"
            )
        except Exception:
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
        await query.answer("गाना चलाने के लिए /play गाने का नाम भेजें।", show_alert=True)
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
    try:
        await message.delete()
    except Exception as delete_error:
        print(f"⚠️ /play command delete नहीं हो पाया: {delete_error}")
    print(f"🎵 PLAY COMMAND RECEIVED | Chat: {chat_id} | Message: {message.id}")
    if len(message.command) < 2:
        await message.reply_text("🎵 गाना चलाने के लिए लिखें:\n/play गाने का नाम")
        return
    query = " ".join(message.command[1:]).strip()
    status = await message.reply_text("🔎 YouTube पर गाना खोज रहा हूँ...")
    try:
        song = await asyncio.to_thread(search_youtube, query)
        if not song:
            await status.edit_text("❌ YouTube पर गाना नहीं मिला।")
            return
        requester = message.from_user
        if requester:
            song["requester_id"] = requester.id
            song["requester_name"] = requester.first_name or requester.username or "User"
        else:
            song["requester_id"] = None
            song["requester_name"] = "Group Admin"
        if len(queues[chat_id]) >= MAX_QUEUE_SIZE:
            await status.edit_text(f"⚠️ Queue भर गई है (अधिकतम {MAX_QUEUE_SIZE} गाने)।")
            return
        queues[chat_id].append(song)
        if chat_id in current_songs or chat_id in playback_starting or chat_id in skip_in_progress:
            await status.edit_text(
                f"✅ Queue में जोड़ा गया:\n{song['title']}\nस्थान: {len(queues[chat_id])}"
            )
            return
        await status.edit_text(f"⚡ YouTube audio तैयार किया जा रहा है:\n{song['title']}")
        success = await play_next(chat_id)
        if success:
            await status.delete()
        else:
            await status.edit_text("❌ गाना शुरू नहीं हो पाया। Console में Error देखें।")
    except requests.HTTPError as error:
        print(f"❌ YOUTUBE API HTTP ERROR: {error}")
        traceback.print_exc()
        await status.edit_text("❌ YouTube Search API में समस्या है। API key और quota जाँचें।")
    except Exception as error:
        print(f"❌ PLAY ERROR: {type(error).__name__}: {error}")
        traceback.print_exc()
        try:
            await status.edit_text("❌ गाना खोजने या चलाने में समस्या हुई।")
        except Exception:
            traceback.print_exc()


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


async def skip_command(client, message):
    chat_id = message.chat.id
    try:
        if chat_id not in current_songs:
            await message.reply_text("❌ अभी कोई गाना नहीं चल रहा है।")
            return
        success = await skip_music(chat_id)
        await message.reply_text("⏭️ Skip हो गया।" if success else "📭 Queue खाली है।")
    except Exception as error:
        print(f"SKIP ERROR: {error}")
        traceback.print_exc()
        await message.reply_text("❌ Skip नहीं हो पाया।")


async def stop_command(client, message):
    chat_id = message.chat.id
    try:
        await stop_music(chat_id)
        await message.reply_text("⏹️ Playback बंद और Queue साफ कर दी गई है।")
    except Exception as error:
        print(f"STOP ERROR: {error}")
        traceback.print_exc()
        await message.reply_text("❌ Stop नहीं हो पाया।")


async def end_command(client, message):
    await stop_command(client, message)


async def queue_command(client, message):
    await message.reply_text(make_queue_text(message.chat.id))


# ================= MAIN =================

async def main():
    global bot, user, calls, DENO_PATH
    disable_core_dumps()
    remove_old_core_dumps()
    print("🤖 Pandey Music Bot शुरू हो रहा है...")
    print(f"🧠 Deno V8 flags configured: {DENO_V8_FLAGS}")

    DENO_PATH = await asyncio.to_thread(setup_deno)
    if DENO_PATH:
        print(f"✅ Deno Runtime Ready: {DENO_PATH}")
    else:
        print("⚠️ Deno उपलब्ध नहीं है; yt-dlp fallback mode में चलेगा।")

    bot = Client(
        "PandeyMusicBot", api_id=API_ID, api_hash=API_HASH,
        bot_token=BOT_TOKEN, workers=1
    )
    user = Client(
        "FreshLogin", api_id=API_ID, api_hash=API_HASH, workers=1
    )
    calls = PyTgCalls(user)

    handlers = [
        ("start", start_command), ("play", play_command),
        ("pause", pause_command), ("resume", resume_command),
        ("skip", skip_command), ("stop", stop_command),
        ("end", end_command), ("queue", queue_command)
    ]

    try:
        await bot.start()
        print("✅ Bot Client Started")
        await user.start()
        print("✅ FreshLogin User Session Started")
        me = await user.get_me()
        if me.is_bot:
            raise RuntimeError("FreshLogin में Normal User Account का session आवश्यक है।")
        await calls.start()
        print("✅ PyTgCalls Started")
        calls.on_update(call_filters.stream_end())(stream_ended_handler)

        for command_name, callback in handlers:
            bot.add_handler(MessageHandler(callback, filters.command(command_name)))
            print(f"✅ Handler Registered: /{command_name}")
        bot.add_handler(CallbackQueryHandler(music_callback, filters.regex(r"^music:")))

        print("✅ Music Buttons Registered")
        print("🎵 Pandey Music Bot Online!")
        print("🌐 YouTube Search Enabled!")
        print("🎛 Inline Music Controls Enabled!")
        print("⚙️ HIGH Audio Quality / Low Worker Mode Enabled!")
        print("👤 Clickable Requester Profile Tag Enabled!")
        await asyncio.Event().wait()
    except Exception as error:
        print(f"❌ MAIN ERROR: {type(error).__name__}: {error}")
        traceback.print_exc()
        raise
    finally:
        print("🛑 Bot बंद हो रहा है...")
        try:
            stop_method = getattr(calls, "stop", None) if calls else None
            if callable(stop_method):
                result = stop_method()
                if asyncio.iscoroutine(result):
                    await result
                print("✅ PyTgCalls Stopped")
            else:
                print("ℹ️ इस PyTgCalls version में stop() method नहीं है; User Client cleanup करेगा।")
        except Exception as stop_error:
            print(f"⚠️ PyTgCalls cleanup warning: {stop_error}")
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


async def run_forever():
    restart_delay = max(5, int(os.getenv("RESTART_DELAY_SECONDS", "10")))
    while True:
        try:
            await main()
            print("⚠️ Main loop समाप्त हुआ; bot दोबारा शुरू होगा।")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(
                f"🔁 BOT CRASH RECOVER: {type(error).__name__}: {error}. "
                f"{restart_delay}s बाद restart होगा।"
            )
            traceback.print_exc()
        await asyncio.sleep(restart_delay)


if __name__ == "__main__":
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        print("🛑 Manual shutdown requested.")
