import os

# Keep native libraries from creating unnecessary worker threads on small hosts.
os.environ.setdefault("MALLOC_ARENA_MAX", "2")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import asyncio
import html
import importlib.metadata
import shutil
import subprocess
import time
import traceback
import urllib.error
import urllib.request
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from pyrogram import Client, filters, enums
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from pytgcalls import PyTgCalls, filters as call_filters
from pytgcalls.types import MediaStream, AudioQuality, StreamEnded


# ================= CONFIG =================

BASE_DIR = Path("/home/container")
load_dotenv(BASE_DIR / ".env")

API_ID = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

if not API_ID.isdigit() or not API_HASH or not BOT_TOKEN:
    raise ValueError("API_ID, API_HASH और BOT_TOKEN .env में जाँचें।")

API_ID = int(API_ID)

# Queue is unlimited. Keep per-chat queues independent.
bot = None
user = None
calls = None

queues = defaultdict(deque)
current_songs = {}
now_playing_messages = {}

chat_locks = defaultdict(asyncio.Lock)
stop_in_progress = set()

AUDIO_DIR = BASE_DIR / "audio"
SONGS_DIR = BASE_DIR / "songs"
FFMPEG_DIR = BASE_DIR / "ffmpeg"
COOKIES_FILE = BASE_DIR / "cookies.txt"


# ================= DIAGNOSTICS =================

def package_version(package_name):
    try:
        return importlib.metadata.version(package_name)
    except Exception:
        return "NOT INSTALLED"


def read_memory_info():
    """Read cgroup memory values when available; do not assume a fixed RAM limit."""
    candidates = [
        (Path("/sys/fs/cgroup/memory.current"),
         Path("/sys/fs/cgroup/memory.max")),
        (Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
         Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")),
    ]

    for used_file, limit_file in candidates:
        try:
            used_text = used_file.read_text().strip()
            limit_text = limit_file.read_text().strip()
            used = int(used_text)
            if limit_text == "max":
                return f"Used: {used / 1024 / 1024:.1f} MB; limit: unlimited"
            limit = int(limit_text)
            if limit > 0:
                return (
                    f"Used: {used / 1024 / 1024:.1f} MB; "
                    f"limit: {limit / 1024 / 1024:.1f} MB"
                )
        except Exception:
            continue

    return "cgroup RAM information unavailable"


def find_ffmpeg():
    """Find an installed FFmpeg executable without downloading or modifying anything."""
    found = shutil.which("ffmpeg")
    if found:
        return found

    candidates = [
        FFMPEG_DIR / "ffmpeg",
        FFMPEG_DIR / "bin" / "ffmpeg",
        BASE_DIR / "ffmpeg",
        BASE_DIR / "bin" / "ffmpeg",
    ]

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    if FFMPEG_DIR.exists():
        try:
            for candidate in FFMPEG_DIR.rglob("ffmpeg"):
                if candidate.is_file():
                    return str(candidate)
        except Exception:
            pass

    return None


def check_ffmpeg():
    executable = find_ffmpeg()
    if not executable:
        return False, "FFmpeg executable नहीं मिला (PATH और /home/container/ffmpeg जाँचा)।"

    try:
        result = subprocess.run(
            [executable, "-version"],
            capture_output=True,
            text=True,
            timeout=8
        )
        if result.returncode == 0:
            first_line = (result.stdout or "").splitlines()
            version_line = first_line[0] if first_line else "version output unavailable"
            return True, f"{executable}\n{version_line}"
        return False, f"Executable मिला लेकिन चल नहीं पाया: {(result.stderr or '')[:250]}"
    except Exception as error:
        return False, f"FFmpeg test failed: {type(error).__name__}: {error}"


def check_direct_audio_url(url):
    """
    Check only a user-supplied direct HTTP(S) media URL.
    This does not extract audio from YouTube or other video platforms.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False, "URL format गलत है। HTTP/HTTPS direct media URL दें।"

    host = (parsed.hostname or "").lower()
    if host.endswith("youtube.com") or host.endswith("youtu.be"):
        return False, "YouTube video URL direct audio URL नहीं है; इस file में YouTube extraction नहीं है।"

    request = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": "PandeyMusicBot-Diagnostic/1.0"}
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            content_type = response.headers.get("Content-Type", "").split(";")[0].lower()
            status = response.status
            if content_type.startswith("audio/") or content_type in (
                "application/octet-stream",
                "application/ogg"
            ):
                return True, f"HTTP {status}; Content-Type: {content_type or 'unknown'}"
            return False, (
                f"HTTP {status}, लेकिन Content-Type '{content_type or 'missing'}' है। "
                "यह audio file होने की पुष्टि नहीं करता।"
            )
    except urllib.error.HTTPError as error:
        return False, f"HTTP {error.code}: server ने HEAD request स्वीकार नहीं की/अनुमति नहीं दी।"
    except Exception as error:
        return False, f"{type(error).__name__}: {error}"


def run_diagnostics():
    lines = [
        "🧪 PANDEY MUSIC BOT DIAGNOSTICS",
        "",
        f"Python: {os.sys.version.split()[0]}",
        f"Pyrogram: {package_version('Pyrogram')}",
        f"PyTgCalls: {package_version('py-tgcalls')}",
        f"ntgcalls: {package_version('ntgcalls')}",
        f"yt-dlp: {package_version('yt-dlp')}",
        f"python-dotenv: {package_version('python-dotenv')}",
        "",
        "CONFIG:",
        f"API_ID present: {'YES' if API_ID else 'NO'}",
        f"API_HASH present: {'YES' if API_HASH else 'NO'}",
        f"BOT_TOKEN present: {'YES' if BOT_TOKEN else 'NO'}",
        "Secret values are intentionally not displayed.",
        "",
        "FOLDERS:",
    ]

    for folder in (BASE_DIR, AUDIO_DIR, SONGS_DIR, FFMPEG_DIR):
        lines.append(
            f"{folder}: {'EXISTS' if folder.exists() else 'MISSING'}"
        )

    lines.extend([
        f"cookies.txt exists: {'YES' if COOKIES_FILE.is_file() else 'NO'} "
        "(existence only; contents are never read or printed)",
        "",
        "FFMPEG:"
    ])

    ffmpeg_ok, ffmpeg_detail = check_ffmpeg()
    lines.append(("PASS: " if ffmpeg_ok else "FAIL: ") + ffmpeg_detail)

    lines.extend([
        "",
        "SERVER:",
        f"RAM: {read_memory_info()}",
    ])

    try:
        disk = shutil.disk_usage(BASE_DIR)
        lines.append(
            f"Disk free: {disk.free / 1024 / 1024:.1f} MB "
            f"(total {disk.total / 1024 / 1024:.1f} MB)"
        )
    except Exception as error:
        lines.append(f"Disk check unavailable: {error}")

    lines.extend([
        "",
        "YOUTUBE NOTE:",
        "This main.py does not run YouTube search or yt-dlp extraction.",
        "YouTube search results are video pages, not direct audio stream URLs.",
        "No YouTube cookies, signatures, or access controls are bypassed.",
        "",
        "CURRENT PLAYBACK MODE:",
        "Direct HTTP(S) audio URLs only; URL must be authorized for playback."
    ])

    return "\n".join(lines)


# ================= HELPERS =================

def valid_audio_url(url):
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return False

        host = (parsed.hostname or "").lower()
        if host.endswith("youtube.com") or host.endswith("youtu.be"):
            return False

        return True
    except Exception:
        return False


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
            InlineKeyboardButton("📋 Queue", callback_data="music:queue")
        ]
    ])


def queue_text(chat_id):
    current = current_songs.get(chat_id)
    queue = queues[chat_id]
    result = "🎵 **PANDEY MUSIC QUEUE**\n\n"

    if current:
        result += f"▶️ अभी चल रहा है:\n{current['title']}\n\n"

    if queue:
        result += "📋 अगली Queue:\n"
        for number, song in enumerate(list(queue)[:20], 1):
            result += f"{number}. {song['title']}\n"
        if len(queue) > 20:
            result += f"\nऔर {len(queue) - 20} गाने बाकी हैं।"
    elif not current:
        result = "📭 Queue खाली है।"

    return result


async def send_now_playing(chat_id, song):
    old = now_playing_messages.pop(chat_id, None)
    if old:
        try:
            await old.delete()
        except Exception:
            pass

    title = html.escape(song["title"])
    name = html.escape(song.get("requester_name", "User"))

    message = await bot.send_message(
        chat_id,
        f"🎵 <b>NOW PLAYING</b>\n\n"
        f"🎧 <b>Song:</b> {title}\n"
        f"👤 <b>Requested by:</b> {name}",
        parse_mode=enums.ParseMode.HTML,
        reply_markup=music_buttons()
    )
    now_playing_messages[chat_id] = message


# ================= PLAYBACK =================

async def start_song(chat_id, song):
    await calls.play(
        chat_id,
        MediaStream(song["url"], audio_parameters=AudioQuality.LOW)
    )
    current_songs[chat_id] = song

    try:
        await send_now_playing(chat_id, song)
    except Exception as error:
        print("NOW PLAYING ERROR:", repr(error))


async def play_next(chat_id):
    async with chat_locks[chat_id]:
        if chat_id in stop_in_progress:
            return False

        while queues[chat_id]:
            song = queues[chat_id].popleft()
            try:
                print("🎧 Starting:", song["title"])
                await start_song(chat_id, song)
                return True
            except Exception as error:
                print("SONG ERROR:", repr(error))
                traceback.print_exc()
                try:
                    await bot.send_message(
                        chat_id,
                        f"⚠️ यह audio नहीं चल पाया:\n{song['title']}\n"
                        "URL access, audio format और FFmpeg जाँचें।"
                    )
                except Exception:
                    pass

        current_songs.pop(chat_id, None)
        return False


async def skip_music(chat_id):
    async with chat_locks[chat_id]:
        if chat_id not in current_songs:
            return False

        if not queues[chat_id]:
            current_songs.pop(chat_id, None)
            old = now_playing_messages.pop(chat_id, None)
            if old:
                try:
                    await old.delete()
                except Exception:
                    pass
            try:
                await calls.leave_call(chat_id)
            except Exception as error:
                print("SKIP END LEAVE ERROR:", repr(error))
            return False

        while queues[chat_id]:
            song = queues[chat_id].popleft()
            try:
                print("⏭️ Skipping to:", song["title"])
                await start_song(chat_id, song)
                return True
            except Exception as error:
                print("SKIP ERROR:", repr(error))
                traceback.print_exc()

        current_songs.pop(chat_id, None)
        return False


async def stop_music(chat_id):
    stop_in_progress.add(chat_id)
    try:
        queues[chat_id].clear()
        current_songs.pop(chat_id, None)

        old = now_playing_messages.pop(chat_id, None)
        if old:
            try:
                await old.delete()
            except Exception:
                pass

        try:
            await calls.leave_call(chat_id)
        except Exception as error:
            print("LEAVE CALL:", repr(error))
    finally:
        stop_in_progress.discard(chat_id)


# ================= STREAM END =================

async def stream_ended_handler(_, update: StreamEnded):
    chat_id = update.chat_id
    if chat_id in stop_in_progress:
        return

    current_songs.pop(chat_id, None)
    old = now_playing_messages.pop(chat_id, None)
    if old:
        try:
            await old.delete()
        except Exception:
            pass

    if queues[chat_id]:
        await play_next(chat_id)
    else:
        try:
            await bot.send_message(chat_id, "🎵 Queue समाप्त हो गई।")
        except Exception:
            pass


# ================= COMMANDS =================

async def start_command(client, message):
    await message.reply_text(
        "🎵 **Pandey Music Bot Online!**\n\n"
        "इस संस्करण में अनुमति वाले direct audio URL चलते हैं:\n"
        "`/play https://example.com/song.mp3`\n\n"
        "/diagnose - Server और dependencies की जाँच\n"
        "/pause - Pause\n"
        "/resume - Resume\n"
        "/skip - Skip\n"
        "/stop - Stop\n"
        "/queue - Queue"
    )


async def diagnose_command(client, message):
    report = run_diagnostics()
    # Telegram message limit is 4096 characters; split safely.
    chunks = [report[i:i + 3800] for i in range(0, len(report), 3800)]
    for chunk in chunks:
        await message.reply_text(
            f"<pre>{html.escape(chunk)}</pre>",
            parse_mode=enums.ParseMode.HTML
        )


async def play_command(client, message):
    if len(message.command) < 2:
        await message.reply_text(
            "कृपया अनुमति वाला direct MP3/M4A URL दें।\n"
            "उदाहरण: `/play https://example.com/song.mp3`"
        )
        return

    url = message.command[1].strip()
    if not valid_audio_url(url):
        await message.reply_text(
            "❌ यह direct audio URL नहीं है।\n"
            "YouTube video link इस संस्करण में समर्थित नहीं है।"
        )
        return

    status = await message.reply_text("🔎 Audio URL की जाँच हो रही है...")
    ok, detail = await asyncio.to_thread(check_direct_audio_url, url)
    if not ok:
        await status.edit_text(
            "❌ URL audio source के रूप में verify नहीं हुआ।\n"
            f"{detail}"
        )
        return

    requester = message.from_user
    title = urlparse(url).path.split("/")[-1] or "Authorized Audio"
    song = {
        "title": title,
        "url": url,
        "requester_name": requester.first_name if requester else "Group User"
    }

    chat_id = message.chat.id
    queues[chat_id].append(song)

    if chat_id in current_songs:
        await status.edit_text(
            f"✅ Queue में जोड़ा गया:\n{title}\n"
            f"स्थान: {len(queues[chat_id])}\n\n"
            f"URL check: {detail}"
        )
        return

    success = await play_next(chat_id)
    try:
        if success:
            await status.delete()
        else:
            await status.edit_text(
                "❌ Audio शुरू नहीं हो पाया। Console में SONG ERROR देखें।"
            )
    except Exception:
        pass


async def pause_command(client, message):
    chat_id = message.chat.id
    if chat_id not in current_songs:
        await message.reply_text("अभी कोई गाना नहीं चल रहा।")
        return
    try:
        await calls.pause_stream(chat_id)
        await message.reply_text("⏸️ Pause हो गया।")
    except Exception as error:
        print("PAUSE ERROR:", repr(error))
        await message.reply_text("Pause नहीं हो पाया। Console में error देखें।")


async def resume_command(client, message):
    chat_id = message.chat.id
    if chat_id not in current_songs:
        await message.reply_text("अभी कोई गाना नहीं चल रहा।")
        return
    try:
        await calls.resume_stream(chat_id)
        await message.reply_text("▶️ Resume हो गया।")
    except Exception as error:
        print("RESUME ERROR:", repr(error))
        await message.reply_text("Resume नहीं हो पाया। Console में error देखें।")


async def skip_command(client, message):
    success = await skip_music(message.chat.id)
    await message.reply_text(
        "⏭️ अगला गाना शुरू हो गया।"
        if success else "📭 अगला गाना उपलब्ध नहीं है; playback बंद कर दिया गया।"
    )


async def stop_command(client, message):
    await stop_music(message.chat.id)
    await message.reply_text("⏹️ Playback बंद और Queue साफ।")


async def queue_command(client, message):
    await message.reply_text(queue_text(message.chat.id))


# ================= CALLBACKS =================

async def music_callback(client, query):
    if not query.message:
        await query.answer("Message उपलब्ध नहीं है।", show_alert=True)
        return

    chat_id = query.message.chat.id
    action = query.data.split(":")[1]

    try:
        if action == "pause":
            await calls.pause_stream(chat_id)
            await query.answer("⏸️ Pause हो गया।")
        elif action == "resume":
            await calls.resume_stream(chat_id)
            await query.answer("▶️ Resume हो गया।")
        elif action == "skip":
            success = await skip_music(chat_id)
            await query.answer("⏭️ Skip हो गया।" if success else "अगला गाना नहीं है।")
        elif action == "stop":
            await stop_music(chat_id)
            await query.answer("⏹️ Music बंद।")
            await bot.send_message(chat_id, "Queue साफ कर दी गई।")
        elif action == "queue":
            await query.answer("Queue भेज दी गई।")
            await bot.send_message(chat_id, queue_text(chat_id))
    except Exception as error:
        print("BUTTON ERROR:", repr(error))
        await query.answer("Action पूरा नहीं हो पाया।", show_alert=True)


# ================= AUTO DELETE =================

def auto_delete(callback):
    async def wrapped(client, message):
        try:
            await message.delete()
            print("🗑️ Command deleted")
        except Exception as error:
            print("COMMAND DELETE INFO:", repr(error))
        return await callback(client, message)
    return wrapped


# ================= MAIN =================

async def main():
    global bot, user, calls

    print("🤖 Pandey Music Bot Starting...")
    print(run_diagnostics())

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

    handlers = {
        "start": start_command,
        "play": play_command,
        "diagnose": diagnose_command,
        "pause": pause_command,
        "resume": resume_command,
        "skip": skip_command,
        "stop": stop_command,
        "end": stop_command,
        "queue": queue_command
    }

    try:
        await bot.start()
        print("✅ Bot started")

        await user.start()
        print("✅ User session started")

        me = await user.get_me()
        if me.is_bot:
            raise RuntimeError("FreshLogin में normal user account चाहिए।")

        await calls.start()
        print("✅ PyTgCalls started")

        calls.on_update(call_filters.stream_end())(stream_ended_handler)

        for command, callback in handlers.items():
            bot.add_handler(
                MessageHandler(auto_delete(callback), filters.command(command))
            )
            print(f"✅ /{command} registered")

        bot.add_handler(
            CallbackQueryHandler(music_callback, filters.regex(r"^music:"))
        )

        print("🎵 Pandey Music Bot ONLINE")
        await asyncio.Event().wait()

    finally:
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


if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
            break
        except KeyboardInterrupt:
            break
        except Exception as error:
            print("BOT ERROR:", repr(error))
            traceback.print_exc()
            time.sleep(10)
