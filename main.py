import os
import asyncio
import traceback
import time
import requests
import shutil
import html

from pathlib import Path
from collections import defaultdict, deque
import resource
import gc
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
MAX_QUEUE_SIZE = max(1, int(os.getenv("MAX_QUEUE_SIZE", "10")))


if not API_ID_TEXT.isdigit() or not API_HASH or not BOT_TOKEN:
    raise ValueError("API_ID, API_HASH और BOT_TOKEN .env में जाँचें।")

API_ID = int(API_ID_TEXT)

# Disable core dumps (a previous crash produced a huge dump file).
try:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
except Exception:
    pass

# Small, single-process host: avoid unnecessarily frequent GC cycles.
gc.set_threshold(700, 10, 10)


# ================= LIGHTWEIGHT STREAM RESOLVER =================

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

# 512 MB hosting पर एक समय में केवल एक audio extraction।
# इससे अलग-अलग groups की simultaneous extraction से RAM pressure घटता है।
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


async def delete_command_message(message):
    """Delete a used command when the bot has Telegram delete permissions."""
    try:
        await message.delete()
    except Exception:
        pass


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

    if not items:
        return None

    item = items[0]
    video_id = item["id"]["videoId"]

    return {
        "title": item["snippet"]["title"],
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "source": "youtube"
    }


# ================= AUDIO EXTRACTION: PIPED + LOW-RAM YT-DLP FALLBACK =================

# Public Piped servers are best-effort only. Set PIPED_API_INSTANCES in .env
# to override this short list (comma-separated URLs).
DEFAULT_PIPED_APIS = (
    "https://pipedapi.adminforge.de",
    "https://pipedapi.leptons.xyz",
    "https://pipedapi.orangenet.cc",
    "https://pipedapi.reallyaweso.me",
)

def _youtube_video_id(video_url):
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(video_url)
    host = (parsed.hostname or "").lower()
    if host in ("youtu.be", "www.youtu.be"):
        return parsed.path.strip("/").split("/")[0]
    if parsed.path == "/watch":
        return parse_qs(parsed.query).get("v", [""])[0]
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live"):
        return parts[1]
    return ""

def _resolve_with_piped(video_id):
    configured = os.getenv("PIPED_API_INSTANCES", "").strip()
    api_instances = (
        [x.strip().rstrip("/") for x in configured.split(",") if x.strip()]
        if configured else list(DEFAULT_PIPED_APIS)
    )

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept": "application/json",
    }
    errors = []

    for api_base in api_instances:
        try:
            response = http.get(
                f"{api_base}/streams/{video_id}",
                headers=headers,
                timeout=(2, 4),
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid JSON response")

            streams = data.get("audioStreams") or []
            usable = []
            for item in streams:
                if not isinstance(item, dict):
                    continue
                stream_url = item.get("url")
                mime = (item.get("mimeType") or item.get("contentType") or "").lower()
                if not stream_url or item.get("videoOnly") is True:
                    continue
                if mime and "audio" not in mime:
                    continue
                try:
                    bitrate = int(item.get("bitrate") or item.get("audioBitrate") or 0)
                except (TypeError, ValueError):
                    bitrate = 0
                usable.append((bitrate, stream_url))

            if usable:
                usable.sort(key=lambda item: item[0], reverse=True)
                print(f"✅ Audio URL मिला: Piped ({api_base})")
                return usable[0][1], errors

            errors.append(f"{api_base}: no audio stream")
        except Exception as error:
            errors.append(f"{api_base}: {type(error).__name__}")
            print(f"⚠️ Piped failed ({api_base}): {type(error).__name__}")

    return None, errors

def _youtube_cookie_path():
    """Return configured cookie path without exposing cookie contents."""
    cookie_env = os.getenv("YOUTUBE_COOKIE_FILE", "").strip()
    if cookie_env:
        path = Path(cookie_env).expanduser()
        if not path.is_absolute():
            path = Path(__file__).resolve().parent / path
        return path
    return Path(__file__).resolve().parent / "cookies.txt"


def diagnose_youtube_cookies():
    """Check Netscape cookie-file structure and whether YouTube sees a login.

    This never prints cookie values. A definitive VALID result requires the
    YouTube homepage to report LOGGED_IN=true; a blocked/changed response is
    reported as inconclusive rather than incorrectly calling cookies invalid.
    """
    import http.cookiejar
    import re

    path = _youtube_cookie_path()
    print("🔎 YouTube cookies diagnostic शुरू...")
    if not path.is_file():
        print(f"❌ COOKIE FILE MISSING: {path.name} नहीं मिली।")
        return
    if path.stat().st_size == 0:
        print("❌ COOKIE FILE EMPTY: cookies.txt खाली है।")
        return

    jar = http.cookiejar.MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as error:
        print(f"❌ COOKIE FILE INVALID: Netscape format पढ़ नहीं पाया ({type(error).__name__})।")
        print("ℹ️ Browser से cookies को Netscape HTTP Cookie File format में दोबारा export करें।")
        return

    youtube_cookies = [c for c in jar if "youtube.com" in (c.domain or "").lower()
                       or "google.com" in (c.domain or "").lower()]
    auth_names = {"SID", "HSID", "SSID", "APISID", "SAPISID", "LOGIN_INFO",
                  "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PAPISID",
                  "__Secure-3PAPISID"}
    auth_cookies = [c for c in youtube_cookies if c.name in auth_names]
    now = time.time()
    live_auth = [c for c in auth_cookies if not c.is_expired(now)]
    print(f"📄 COOKIE FILE READABLE: {len(youtube_cookies)} YouTube/Google cookies मिले; values छिपाई गई हैं।")
    print(f"🔐 AUTH COOKIE CHECK: {len(auth_cookies)} पहचान वाले auth cookies, {len(live_auth)} unexpired।")

    if not youtube_cookies:
        print("❌ COOKIE CONTENT INVALID/WRONG: YouTube/Google domain cookies नहीं मिले।")
        return
    if not live_auth:
        print("⚠️ AUTH COOKIES MISSING/EXPIRED: login cookies नहीं मिले या expire हो चुके हैं।")
        print("ℹ️ YouTube से logged-in browser session की नई Netscape cookies export करें।")
        return

    try:
        session = requests.Session()
        session.cookies.update(jar)
        response = session.get(
            "https://www.youtube.com/", timeout=8,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"},
            allow_redirects=True,
        )
        if response.status_code == 200:
            match = re.search(r'"LOGGED_IN"\s*:\s*(true|false)', response.text, re.I)
            if match and match.group(1).lower() == "true":
                print("✅ COOKIES VALID: YouTube ne cookies accept ki aur logged-in session confirm hua.")
                return
            if match and match.group(1).lower() == "false":
                print("❌ COOKIES NOT AUTHENTICATED: YouTube ne request ko logged-out dikhaya; cookies expired/invalid ho sakti hain.")
                return
        print(f"⚠️ COOKIE STATUS INCONCLUSIVE: YouTube HTTP {response.status_code}; response में login status confirm नहीं हुआ।")
        print("ℹ️ Cookies file पढ़ने योग्य है, लेकिन YouTube/IP restriction के कारण server-side validity confirm नहीं हुई।")
    except Exception as error:
        print(f"⚠️ COOKIE STATUS INCONCLUSIVE: YouTube validation request failed ({type(error).__name__})।")
        print("ℹ️ File structure पढ़ने योग्य है; network/IP block की वजह से acceptance जाँची नहीं जा सकी।")


def _resolve_with_ytdlp(video_url):
    """Try YouTube clients that may expose formats without a JS runtime.

    The host has a tight memory limit, so Deno/Node stay disabled.  The TV
    client and Safari HLS client are tried before cookie-authenticated web
    clients; Android is retained only as a no-cookie fallback.
    """
    try:
        import yt_dlp
    except ImportError as error:
        raise RuntimeError(
            "yt-dlp installed नहीं है। requirements.txt में yt-dlp जोड़ें।"
        ) from error

    cookie_path = _youtube_cookie_path()
    has_cookies = cookie_path.is_file() and cookie_path.stat().st_size > 0
    if has_cookies:
        print("🍪 YouTube cookies enabled for supported yt-dlp profiles.")
    else:
        print("⚠️ cookies.txt नहीं मिली; yt-dlp बिना cookies कोशिश करेगा।")

    base_options = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "cachedir": False,
        # Keep JS challenge runtimes disabled for the 294 MB container.
        "js_runtimes": {},
        "socket_timeout": 8,
        "retries": 1,
        "extractor_retries": 1,
        "fragment_retries": 1,
        "concurrent_fragment_downloads": 1,
        "overwrites": False,
    }

    profiles = []

    # TV client does not require a PO token for its formats in current yt-dlp.
    tv_options = dict(base_options)
    tv_options["extractor_args"] = {"youtube": {"player_client": ["tv"]}}
    profiles.append(("tv", tv_options))

    # Safari HLS can expose streams when the regular web client is SABR-only.
    safari_options = dict(base_options)
    safari_options["extractor_args"] = {"youtube": {"player_client": ["web_safari"]}}
    if has_cookies:
        safari_options["cookiefile"] = str(cookie_path)
    profiles.append(("web_safari", safari_options))

    # Embedded client can work for videos that permit embedding; cookies are
    # passed when available, but this profile is not guaranteed for all videos.
    embedded_options = dict(base_options)
    embedded_options["extractor_args"] = {"youtube": {"player_client": ["web_embedded"]}}
    if has_cookies:
        embedded_options["cookiefile"] = str(cookie_path)
    profiles.append(("web_embedded", embedded_options))

    # Android does not support account-cookie authentication in yt-dlp.
    android_options = dict(base_options)
    android_options["extractor_args"] = {"youtube": {"player_client": ["android"]}}
    profiles.append(("android", android_options))

    # Last attempt: yt-dlp's own current client selection with the cookie file.
    default_options = dict(base_options)
    if has_cookies:
        default_options["cookiefile"] = str(cookie_path)
    profiles.append(("default", default_options))

    info = None
    profile_errors = []
    for profile_name, profile_options in profiles:
        try:
            with yt_dlp.YoutubeDL(profile_options) as ydl:
                candidate = ydl.extract_info(video_url, download=False)
            if isinstance(candidate, dict) and (candidate.get("url") or candidate.get("formats")):
                info = candidate
                print(f"✅ yt-dlp profile successful: {profile_name}")
                break
            profile_errors.append(f"{profile_name}: no video formats returned")
            print(f"⚠️ yt-dlp profile failed: {profile_name} (no formats)")
        except Exception as error:
            # Exception text is useful for distinguishing LOGIN_REQUIRED,
            # PO-token and no-format failures. Never print cookie values.
            detail = str(error).replace("\n", " ")[:180]
            profile_errors.append(f"{profile_name}: {type(error).__name__}: {detail}")
            print(f"⚠️ yt-dlp profile failed: {profile_name}: {type(error).__name__}: {detail}")

    if info is None:
        raise RuntimeError(
            "सभी YouTube client profiles असफल: " + " | ".join(profile_errors)
        )

    formats = info.get("formats") or []
    audio_formats = []
    for item in formats:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        if item.get("vcodec") not in (None, "none"):
            continue
        if item.get("acodec") in (None, "none"):
            continue
        audio_formats.append(item)

    if audio_formats:
        audio_formats.sort(
            key=lambda item: (
                item.get("abr") or 0,
                item.get("asr") or 0,
                item.get("tbr") or 0,
            ),
            reverse=True,
        )
        print("✅ Audio URL मिला: yt-dlp client fallback")
        return audio_formats[0]["url"]

    direct_url = info.get("url")
    if direct_url and info.get("acodec") not in (None, "none"):
        print("✅ Audio URL मिला: yt-dlp selected format")
        return direct_url

    raise RuntimeError("yt-dlp response में playable audio-only format नहीं मिला।")

def extract_audio_url(video_url):
    video_id = _youtube_video_id(video_url)
    if not video_id:
        raise RuntimeError("YouTube video ID समझ नहीं आया।")

    piped_url, piped_errors = _resolve_with_piped(video_id)
    if piped_url:
        return piped_url

    print("↪️ सभी Piped attempts असफल; अब yt-dlp low-memory fallback...")
    try:
        return _resolve_with_ytdlp(video_url)
    except Exception as error:
        summary = " | ".join(piped_errors[:4])
        raise RuntimeError(
            "Piped और yt-dlp दोनों resolver असफल हुए। "
            f"Piped: {summary}. yt-dlp: {type(error).__name__}: {str(error)[:220]}"
        ) from error

# ================= PLAYBACK =================

async def start_song_in_call(chat_id, song, audio_url):
    """मौजूदा VC call को छोड़े बिना stream चलाएँ/बदलें।"""
    await calls.play(
        chat_id,
        MediaStream(audio_url, AudioQuality.HIGH)
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
        print(f"🎧 Trying: {song['title']}")

        # Preload बंद है: audio तभी extract होगा जब गाना चलने की बारी आएगी।
        async with extraction_semaphore:
            audio_input = await asyncio.to_thread(
                extract_audio_url, song["url"]
            )

        # पहली बार connect करेगा; active VC में stream replace करेगा।
        await start_song_in_call(chat_id, song, audio_input)

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
        # Queue खाली हो तो मौजूदा stream pause करें; call को बनाए रखें।
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

        # पुराना stream हटाने से अगले YouTube extraction के समय
        # VC/FFmpeg pipeline की अतिरिक्त RAM इस्तेमाल नहीं होगी।
        try:
            await calls.leave_call(chat_id)
            print(f"🧹 Previous VC stream released: {chat_id}")
        except Exception as leave_error:
            # Call पहले से disconnect हो तो भी extraction जारी रखें।
            print(f"⚠️ Previous VC release warning: {leave_error}")

        current_songs.pop(chat_id, None)
        now_playing_messages.pop(chat_id, None)

        async with extraction_semaphore:
            audio_url = await asyncio.to_thread(
                extract_audio_url, next_song["url"]
            )

        # calls.play नया group call/stream शुरू करेगा।
        await start_song_in_call(chat_id, next_song, audio_url)

        print(
            f"✅ SKIP COMPLETE; VC RESTARTED: {chat_id} | "
            f"Time: {time.perf_counter() - skip_started:.2f}s"
        )
        return True

    except Exception as error:
        print(f"❌ SKIP ERROR: {type(error).__name__}: {error}")
        traceback.print_exc()

        # Extraction असफल हो तो गाना queue में रहने दें ताकि खो न जाए।
        try:
            if 'next_song' in locals():
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
    await delete_command_message(message)
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

    # /play command message को chat से हटाने की कोशिश करें।
    # Telegram में इसके लिए bot को group में message delete permission चाहिए।
    try:
        await message.delete()
    except Exception as delete_error:
        print(f"⚠️ /play command delete नहीं हो पाया: {delete_error}")

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

        if len(queues[chat_id]) >= MAX_QUEUE_SIZE:
            await status.edit_text(
                f"⚠️ Queue भर गई है (अधिकतम {MAX_QUEUE_SIZE} गाने)।"
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
    await delete_command_message(message)
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
    await delete_command_message(message)
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
    await delete_command_message(message)
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
    await delete_command_message(message)
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
    await delete_command_message(message)
    await message.reply_text(make_queue_text(message.chat.id))


# ================= MAIN =================

async def main():
    global bot, user, calls

    print("🤖 Pandey Music Bot शुरू हो रहा है...")

    print("🪶 LOW-RAM MODE: Piped first, yt-dlp fallback; Deno/Node JS runtime disabled.")
    await asyncio.to_thread(diagnose_youtube_cookies)

    bot = Client(
        "PandeyMusicBot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        workers=1
    )

    user = Client(
        "FreshLogin",
        api_id=API_ID,
        api_hash=API_HASH,
        workers=1
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
        print("🧠 Piped resolver + yt-dlp fallback enabled; Deno/Node child process disabled.")
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

        # PyTgCalls background tasks को clients से पहले साफ करें।
        try:
            stop_method = getattr(calls, "stop", None) if calls else None
            if callable(stop_method):
                result = stop_method()
                if asyncio.iscoroutine(result):
                    await result
                print("✅ PyTgCalls Stopped")
            else:
                print(
                    "ℹ️ इस PyTgCalls version में stop() method नहीं है; "
                    "User Client बंद होने पर call transport cleanup होगा।"
                )
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
    """Unhandled app-level error पर cleanup के बाद bot को restart करें।"""
    restart_delay = max(5, int(os.getenv("RESTART_DELAY_SECONDS", "10")))
    while True:
        try:
            await main()
            print("⚠️ Main loop समाप्त हुआ; bot दोबारा शुरू होगा।")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"🔁 BOT CRASH RECOVER: {type(error).__name__}: {error}. {restart_delay}s बाद restart होगा।")
            traceback.print_exc()
        await asyncio.sleep(restart_delay)


if __name__ == "__main__":
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        print("🛑 Manual shutdown requested.")
