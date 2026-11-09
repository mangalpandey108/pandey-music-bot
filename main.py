import os
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

load_dotenv("/home/container/.env")

API_ID_TEXT = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()

DENO_DIR = Path("/home/container/.deno-runtime").resolve()
DENO_BIN = DENO_DIR / "deno"

if not API_ID_TEXT.isdigit() or not API_HASH or not BOT_TOKEN:
    raise ValueError("API_ID, API_HASH और BOT_TOKEN .env में जाँचें।")

API_ID = int(API_ID_TEXT)


# ================= DENO AUTO INSTALLER =================

def setup_deno():
    """
    Deno की official GitHub release डाउनलोड करके
    server के writable folder में install करने की कोशिश करता है।
    """

    existing = shutil.which("deno")

    if existing:
        print(f"✅ System Deno Found: {existing}")
        return existing

    if DENO_BIN.exists():
        try:
            os.chmod(DENO_BIN, 0o755)
            result = subprocess.run(
                [str(DENO_BIN), "--version"],
                capture_output=True,
                text=True,
                timeout=15
            )

            if result.returncode == 0:
                print("✅ पहले से डाउनलोड किया हुआ Deno मिला।")
                return str(DENO_BIN)

        except Exception as error:
            print(f"⚠️ Existing Deno इस्तेमाल नहीं हो सका: {error}")

    machine = platform.machine().lower()

    if machine in ("x86_64", "amd64"):
        asset_name = "deno-x86_64-unknown-linux-gnu.zip"
    elif machine in ("aarch64", "arm64"):
        asset_name = "deno-aarch64-unknown-linux-gnu.zip"
    else:
        print(f"❌ Unsupported server architecture: {machine}")
        return None

    if platform.system().lower() != "linux":
        print("❌ यह installer Linux server के लिए बनाया गया है।")
        return None

    zip_path = DENO_DIR / "deno-download.zip"

    try:
        DENO_DIR.mkdir(parents=True, exist_ok=True)

        download_url = (
            "https://github.com/denoland/deno/releases/latest/download/"
            + asset_name
        )

        print("⬇️ Official Deno download शुरू हो रहा है...")

        response = requests.get(
            download_url,
            stream=True,
            timeout=(15, 120)
        )
        response.raise_for_status()

        with open(zip_path, "wb") as file:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    file.write(chunk)

        print("📦 Deno ZIP डाउनलोड पूरा हुआ।")

        with zipfile.ZipFile(zip_path, "r") as archive:
            if "deno" not in archive.namelist():
                raise RuntimeError("ZIP में Deno executable नहीं मिला।")

            archive.extract("deno", path=DENO_DIR)

        os.chmod(
            DENO_BIN,
            stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR |
            stat.S_IRGRP | stat.S_IXGRP |
            stat.S_IROTH | stat.S_IXOTH
        )

        result = subprocess.run(
            [str(DENO_BIN), "--version"],
            capture_output=True,
            text=True,
            timeout=20
        )

        if result.returncode != 0:
            raise RuntimeError(
                result.stderr or "Deno executable नहीं चल पाया।"
            )

        os.environ["PATH"] = (
            str(DENO_DIR) + os.pathsep + os.environ.get("PATH", "")
        )

        print("✅ Deno Successfully Installed!")
        print(result.stdout.strip())

        return str(DENO_BIN)

    except Exception as error:
        print("❌ Deno Auto Installation असफल!")
        print(f"कारण: {type(error).__name__}: {error}")
        print(
            "ℹ️ SillyDev ने download या executable permission "
            "रोक रखी हो सकती है।"
        )
        return None

    finally:
        try:
            if zip_path.exists():
                zip_path.unlink()
        except Exception:
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

# Queue में सबसे अगले गाने का audio URL पहले से तैयार रखने के लिए।
prepared_audio = {}
preload_tasks = {}

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
        "cachedir": False
    }

    deno_path = DENO_PATH or shutil.which("deno")

    if deno_path:
        print(f"✅ yt-dlp Deno Runtime: {deno_path}")

        options["js_runtimes"] = {
            "deno": {"path": deno_path}
        }
    else:
        print("⚠️ Deno उपलब्ध नहीं है। yt-dlp बिना Deno चलेगा।")

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(video_url, download=False)

    except yt_dlp.utils.DownloadError as error:
        error_text = str(error)
        lowered = error_text.casefold()

        if "sign in to confirm you're not a bot" in lowered:
            raise RuntimeError(
                "YouTube ने इस वीडियो के लिए verification माँगा है।"
            ) from error

        raise RuntimeError(
            f"yt-dlp extraction असफल: {error_text[:700]}"
        ) from error

    if not info:
        raise RuntimeError("YouTube audio information नहीं मिली।")

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
        raise RuntimeError("Direct Audio URL उपलब्ध नहीं है।")

    print(
        f"⚡ Audio Extraction Time: "
        f"{time.perf_counter() - started:.2f}s"
    )

    return audio_url


# ================= QUEUE AUDIO PRELOAD =================

async def _preload_song(chat_id, song):
    """Queue के सबसे अगले गाने का direct audio URL background में तैयार करें।"""
    this_task = asyncio.current_task()
    try:
        print(f"⏳ PRELOAD START: {song.get('title', 'Unknown Song')}")
        audio_url = await asyncio.to_thread(extract_audio_url, song["url"])
        prepared_audio[chat_id] = (song, audio_url)
        print(f"✅ PRELOAD READY: {song.get('title', 'Unknown Song')}")
        return audio_url
    except Exception as error:
        print(
            f"⚠️ PRELOAD FAILED: {song.get('title', 'Unknown Song')} | "
            f"{type(error).__name__}: {error}"
        )
        traceback.print_exc()
        return None
    finally:
        current = preload_tasks.get(chat_id)
        if current and current[1] is this_task:
            preload_tasks.pop(chat_id, None)


async def start_preload(chat_id):
    """एक समय में केवल एक queued song preload करें।"""
    if not queues[chat_id]:
        return

    song = queues[chat_id][0]
    cached = prepared_audio.get(chat_id)
    if cached and cached[0] is song:
        return

    running = preload_tasks.get(chat_id)
    if running and running[0] is song and not running[1].done():
        return

    task = asyncio.create_task(_preload_song(chat_id, song))
    preload_tasks[chat_id] = (song, task)


async def get_audio_for_song(chat_id, song):
    """पहले से तैयार URL लें; न मिले तो अभी extraction करें।"""
    cached = prepared_audio.get(chat_id)
    if cached and cached[0] is song:
        prepared_audio.pop(chat_id, None)
        print(f"⚡ USING PRELOADED AUDIO: {song.get('title')}")
        return cached[1]

    running = preload_tasks.get(chat_id)
    if running and running[0] is song:
        try:
            audio_url = await running[1]
            cached = prepared_audio.get(chat_id)
            if cached and cached[0] is song:
                prepared_audio.pop(chat_id, None)
                return cached[1]
            if audio_url:
                return audio_url
        except Exception:
            pass

    print(f"🔄 PRELOAD उपलब्ध नहीं; audio तैयार कर रहा हूँ: {song.get('title')}")
    return await asyncio.to_thread(extract_audio_url, song["url"])


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

    # मौजूदा गाना शुरू होने के बाद केवल अगला गाना preload करें।
    await start_preload(chat_id)


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

        audio_input = await get_audio_for_song(chat_id, song)
        vc_started = time.perf_counter()

        # पहली बार connect करेगा; active VC में stream replace करेगा।
        await start_song_in_call(chat_id, song, audio_input)

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
        prepared_audio.pop(chat_id, None)

        running = preload_tasks.pop(chat_id, None)
        if running and not running[1].done():
            running[1].cancel()

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
        # सबसे पहले अगला गाना queue से लें और उसका audio तैयार/प्राप्त करें।
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

        audio_url = await get_audio_for_song(chat_id, next_song)

        # महत्वपूर्ण: यहाँ leave_call() नहीं है। calls.play active VC की
        # stream को बदलता है, इसलिए Assistant call से बाहर नहीं जाता।
        await start_song_in_call(chat_id, next_song, audio_url)
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

        if chat_id in current_songs and chat_id not in skip_in_progress:
            await start_preload(chat_id)

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


# ================= MAIN =================

async def main():
    global bot, user, calls, DENO_PATH

    print("🤖 Pandey Music Bot शुरू हो रहा है...")

    # Bot start होने से पहले Deno install/check करें
    DENO_PATH = await asyncio.to_thread(setup_deno)

    if DENO_PATH:
        print(f"✅ Deno Runtime Ready: {DENO_PATH}")
    else:
        print("⚠️ Deno उपलब्ध नहीं है; yt-dlp fallback mode में चलेगा।")

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
                    callback,
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
    asyncio.run(main())
