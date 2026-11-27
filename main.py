import os
import asyncio
import traceback
import time
import requests
import yt_dlp
import gc

from pathlib import Path
from collections import defaultdict, deque
from html import escape
from functools import wraps

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait

from pytgcalls import PyTgCalls, filters as call_filters
from pytgcalls.types import MediaStream, AudioQuality, StreamEnded


# ==================================================
# CONFIG
# ==================================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

API_ID_TEXT = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()

# SillyDev API
SILLYDEV_API_KEY = os.getenv("SILLYDEV_API_KEY", "").strip()
OWNER_ID_TEXT = os.getenv("OWNER_ID", "").strip()

SILLYDEV_SERVER_ID = "92eaec77"
SILLYDEV_API_URL = "https://sillydev.co.uk/panel/api/client"

if not API_ID_TEXT.isdigit() or not API_HASH or not BOT_TOKEN:
    raise ValueError("API_ID, API_HASH और BOT_TOKEN .env में जाँचें।")

API_ID = int(API_ID_TEXT)

BOT_TITLE = "🎵 PANDEY MUSIC BOT"
POWERED_BY = "👑 POWERED BY @The_PANDEY_G"

RESTART_DELAY = 10
MAX_RESTART_DELAY = 60

# हर गाने के बाद Python garbage collection
CLEANUP_AFTER_TRACK = True

# Telegram FloodWait पर अधिकतम अतिरिक्त retry
TELEGRAM_RETRIES = 3


# ==================================================
# GLOBALS
# ==================================================

bot = None
user = None
calls = None

queues = defaultdict(deque)
current_songs = {}

playback_starting = set()
skip_in_progress = set()
stop_in_progress = set()

http = requests.Session()


# ==================================================
# MEMORY CLEANUP
# ==================================================

def memory_cleanup(chat_id=None, reason="cleanup"):
    """
    Python के unused objects हटाता है।
    Active stream या दूसरे chats की Queue को नहीं छूता।
    """

    try:
        if chat_id is not None:
            if (
                chat_id in queues
                and not queues[chat_id]
                and chat_id not in current_songs
                and chat_id not in playback_starting
            ):
                queues.pop(chat_id, None)

        collected = gc.collect()

        print(
            f"🧹 MEMORY CLEANUP | "
            f"Reason: {reason} | "
            f"Collected: {collected}"
        )

    except Exception as error:
        print(f"⚠️ MEMORY CLEANUP ERROR: {error}")


def cleanup_all():
    """Shutdown के समय सभी अस्थायी Python objects साफ करना।"""

    try:
        gc.collect()
        print("🧹 Final garbage collection completed")

    except Exception:
        traceback.print_exc()


# ==================================================
# TELEGRAM FLOODWAIT HELPER
# ==================================================

async def telegram_send(chat_id, text, **kwargs):
    """
    Telegram FloodWait आने पर निर्धारित समय तक इंतजार करके retry करता है।
    """

    for attempt in range(TELEGRAM_RETRIES + 1):
        try:
            return await bot.send_message(
                chat_id,
                text,
                **kwargs
            )

        except FloodWait as error:
            wait_time = int(error.value) + 1

            print(
                f"⏳ Telegram FloodWait: "
                f"{wait_time}s | Chat: {chat_id}"
            )

            if attempt >= TELEGRAM_RETRIES:
                raise

            await asyncio.sleep(wait_time)

    return None


# ==================================================
# HELPERS
# ==================================================

def safe_text(value):
    return escape(str(value or "Unknown"))


def requester_name(message):
    person = message.from_user

    if not person:
        return "Unknown"

    name = person.first_name or "Unknown"

    if person.last_name:
        name += " " + person.last_name

    return name


def requester_link(user_id, name):
    if not user_id:
        return safe_text(name)

    return (
        f'<a href="tg://user?id={int(user_id)}">'
        f'{safe_text(name)}</a>'
    )


def bot_header():
    return (
        f"<b>{BOT_TITLE}</b>\n"
        f"<i>{POWERED_BY}</i>\n"
        "━━━━━━━━━━━━━━━━━━\n"
    )


def music_keyboard(chat_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⏸️ Pause",
                callback_data=f"music:pause:{chat_id}"
            ),
            InlineKeyboardButton(
                "▶️ Resume",
                callback_data=f"music:resume:{chat_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "⏭️ Skip",
                callback_data=f"music:skip:{chat_id}"
            ),
            InlineKeyboardButton(
                "⏹️ Stop",
                callback_data=f"music:stop:{chat_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "📋 Queue",
                callback_data=f"music:queue:{chat_id}"
            ),
            InlineKeyboardButton(
                "🔄 Refresh",
                callback_data=f"music:refresh:{chat_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "🔚 End",
                callback_data=f"music:end:{chat_id}"
            )
        ]
    ])


# ==================================================
# SAFE TELEGRAM OPERATIONS
# ==================================================

async def safe_delete(message):
    try:
        await message.delete()
        print("🗑️ Command Message Deleted")

    except FloodWait as error:
        print(f"⏳ Delete FloodWait: {error.value}s")
        await asyncio.sleep(int(error.value) + 1)

        try:
            await message.delete()
        except Exception:
            pass

    except Exception as error:
        print(f"⚠️ Message Delete नहीं हुआ: {error}")


def auto_delete_command(func):
    @wraps(func)
    async def wrapper(client, message, *args, **kwargs):
        await safe_delete(message)

        return await func(
            client,
            message,
            *args,
            **kwargs
        )

    return wrapper


# ==================================================
# NOW PLAYING TEXT
# ==================================================

def now_playing_text(song):
    return (
        bot_header()
        + "🎶 <b>NOW PLAYING</b>\n\n"
        + f"🎧 <b>{safe_text(song['title'])}</b>\n"
        + f"👤 <b>Requested By:</b> "
        + requester_link(
            song.get("requester_id"),
            song["requester"]
        )
        + "\n"
        + "▶️ <b>Status:</b> Playing\n"
        + "━━━━━━━━━━━━━━━━━━\n"
        + "🎛️ नीचे दिए गए बटन से Music Control करें।"
    )


# ==================================================
# QUEUE TEXT
# ==================================================

def queue_text(chat_id):
    current = current_songs.get(chat_id)
    queue = queues.get(chat_id, deque())

    text = bot_header() + "📋 <b>MUSIC QUEUE</b>\n\n"

    if current:
        text += (
            "▶️ <b>NOW PLAYING</b>\n"
            f"🎶 {safe_text(current['title'])}\n"
            f"👤 Requested By: "
            + requester_link(
                current.get("requester_id"),
                current["requester"]
            )
            + "\n\n"
        )

    if queue:
        text += "📌 <b>UP NEXT</b>\n"

        for number, song in enumerate(list(queue)[:20], start=1):
            text += (
                f"\n<b>{number}.</b> "
                f"{safe_text(song['title'])}\n"
                f"👤 "
                + requester_link(
                    song.get("requester_id"),
                    song["requester"]
                )
                + "\n"
            )

        if len(queue) > 20:
            text += f"\nऔर {len(queue) - 20} गाने बाकी हैं।"

    elif not current:
        text += "📭 Queue खाली है।"

    text += (
        "\n━━━━━━━━━━━━━━━━━━\n"
        f"🎧 <b>आने वाले गाने:</b> {len(queue)}"
    )

    return text


# ==================================================
# YOUTUBE SEARCH
# ==================================================

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

    items = response.json().get("items", [])

    print(
        f"🔎 Search Time: "
        f"{time.perf_counter() - start:.2f}s"
    )

    if not items:
        return None

    item = items[0]

    return {
        "title": item["snippet"]["title"],
        "url": (
            "https://www.youtube.com/watch?v="
            + item["id"]["videoId"]
        )
    }


# ==================================================
# AUDIO EXTRACTION
# ==================================================

def extract_audio_url(video_url):
    """
    yt-dlp का temporary object function समाप्त होने पर
    अपने आप release हो जाता है।
    """

    start = time.perf_counter()
    audio_url = None

    options = {
        "format": "bestaudio[acodec!=none]/bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 15,
        "retries": 2,
        "extractor_retries": 2,
        "fragment_retries": 2,
        "cachedir": False,
        "nopart": True,
        "overwrites": True
    }

    try:
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

            # Unneeded metadata reference हटाएँ
            del info

        print(
            f"⚡ Extraction Time: "
            f"{time.perf_counter() - start:.2f}s"
        )

        return audio_url

    finally:
        # Function समाप्त होने पर local references release होंगे
        gc.collect()


# ==================================================
# PLAY NEXT
# ==================================================

async def play_next(chat_id):
    if chat_id in playback_starting:
        return False

    playback_starting.add(chat_id)

    try:
        # एक गाना फेल होने पर Queue के अगले गाने की कोशिश
        while queues.get(chat_id):

            song = queues[chat_id].popleft()
            audio_url = None
            media_stream = None

            try:
                print(
                    f"🔎 Preparing: {song.get('title', 'Unknown')}"
                )

                audio_url = await asyncio.to_thread(
                    extract_audio_url,
                    song["url"]
                )

                media_stream = MediaStream(
                    audio_url,
                    AudioQuality.HIGH
                )

                await calls.play(
                    chat_id,
                    media_stream
                )

                # Playback object को calls संभालता है।
                # Bot में direct URL reference रखने की जरूरत नहीं।
                del media_stream
                media_stream = None

                del audio_url
                audio_url = None

                current_songs[chat_id] = song

                try:
                    await telegram_send(
                        chat_id,
                        now_playing_text(song),
                        reply_markup=music_keyboard(chat_id),
                        disable_web_page_preview=True
                    )

                except Exception as send_error:
                    print(
                        f"⚠️ Now Playing message failed: "
                        f"{send_error}"
                    )

                print(
                    f"🎵 Playing: {song['title']} | "
                    f"Requester: {song['requester']} | "
                    f"ID: {song.get('requester_id')}"
                )

                return True

            except Exception as error:
                print(
                    f"❌ SONG PLAY ERROR {chat_id}: "
                    f"{type(error).__name__}: {error}"
                )

                traceback.print_exc()

                # Failed song metadata release
                song.clear()
                del song

                # Failed extraction के references हटाएँ
                audio_url = None
                media_stream = None

                memory_cleanup(
                    chat_id,
                    reason="song_play_failed"
                )

                try:
                    await telegram_send(
                        chat_id,
                        bot_header()
                        + "❌ यह गाना नहीं चल पाया। "
                        + "अगले गाने की कोशिश कर रहा हूँ।"
                    )
                except Exception as notify_error:
                    print(
                        f"⚠️ Error message failed: {notify_error}"
                    )

                continue

        current_songs.pop(chat_id, None)

        memory_cleanup(
            chat_id,
            reason="queue_finished"
        )

        return False

    finally:
        playback_starting.discard(chat_id)


# ==================================================
# STREAM END
# ==================================================

async def stream_ended_handler(_, update: StreamEnded):
    chat_id = update.chat_id

    print(f"🏁 STREAM ENDED: {chat_id}")

    # Skip / Stop के दौरान आने वाले पुराने event को ignore करें
    if (
        chat_id in skip_in_progress
        or chat_id in stop_in_progress
    ):
        return

    old_song = current_songs.pop(chat_id, None)

    if old_song:
        old_song.clear()
        del old_song

    # पुराने track का cleanup
    memory_cleanup(
        chat_id,
        reason="stream_ended"
    )

    if queues.get(chat_id):
        await play_next(chat_id)

    else:
        try:
            await telegram_send(
                chat_id,
                bot_header() + "✅ Queue समाप्त हो गई।"
            )
        except Exception as error:
            print(f"⚠️ Queue end message failed: {error}")

        memory_cleanup(
            chat_id,
            reason="queue_empty"
        )


# ==================================================
# START
# ==================================================

@auto_delete_command
async def start_command(client, message):
    await message.reply_text(
        bot_header()
        + "ऑनलाइन है! 🎧\n\n"
        + "<b>COMMANDS</b>\n"
        + "/play गाने का नाम\n"
        + "/pause\n"
        + "/resume\n"
        + "/skip\n"
        + "/stop\n"
        + "/end\n"
        + "/queue\n"
        + "/restart",
        disable_web_page_preview=True
    )


# ==================================================
# PLAY
# ==================================================

@auto_delete_command
async def play_command(client, message):
    if len(message.command) < 2:
        await message.reply_text(
            bot_header() + "उदाहरण: /play Kesariya"
        )
        return

    query = " ".join(message.command[1:])

    requester = requester_name(message)

    requester_id = (
        message.from_user.id
        if message.from_user else None
    )

    status = await message.reply_text(
        bot_header() + "🔎 गाना खोज रहा हूँ..."
    )

    try:
        song = await asyncio.to_thread(
            search_youtube,
            query
        )

        if not song:
            await status.edit_text(
                bot_header() + "❌ कोई गाना नहीं मिला।"
            )
            return

        chat_id = message.chat.id

        song["requester"] = requester
        song["requester_id"] = requester_id

        queues[chat_id].append(song)

        position = len(queues[chat_id])

        if (
            chat_id in current_songs
            or chat_id in playback_starting
            or chat_id in skip_in_progress
        ):
            await status.edit_text(
                bot_header()
                + "✅ <b>QUEUE में जोड़ा गया</b>\n\n"
                + f"🎶 {safe_text(song['title'])}\n"
                + f"👤 Requested By: "
                + requester_link(
                    requester_id,
                    requester
                )
                + f"\n🔢 Queue Position: {position}",
                reply_markup=music_keyboard(chat_id)
            )
            return

        await status.edit_text(
            bot_header() + "⚡ Audio तैयार किया जा रहा है..."
        )

        success = await play_next(chat_id)

        if success:
            try:
                await status.delete()
            except Exception:
                pass

        else:
            await status.edit_text(
                bot_header()
                + "❌ गाना शुरू नहीं हो पाया।"
            )

    except Exception as error:
        print(f"❌ PLAY COMMAND ERROR: {error}")
        traceback.print_exc()

        try:
            await status.edit_text(
                bot_header()
                + "❌ गाना खोजने या चलाने में समस्या हुई।"
            )
        except Exception:
            pass


# ==================================================
# PAUSE
# ==================================================

async def pause_action(chat_id):
    if chat_id not in current_songs:
        return False, "अभी कोई गाना नहीं चल रहा है।"

    await calls.pause_stream(chat_id)

    return True, "⏸️ गाना रोक दिया गया है।"


@auto_delete_command
async def pause_command(client, message):
    try:
        ok, result = await pause_action(message.chat.id)

        await message.reply_text(
            bot_header() + result
        )

    except Exception as error:
        print(f"PAUSE ERROR: {error}")

        await message.reply_text(
            "❌ Pause Error"
        )


# ==================================================
# RESUME
# ==================================================

async def resume_action(chat_id):
    if chat_id not in current_songs:
        return False, "अभी कोई गाना नहीं चल रहा है।"

    await calls.resume_stream(chat_id)

    return True, "▶️ गाना फिर शुरू हो गया है।"


@auto_delete_command
async def resume_command(client, message):
    try:
        ok, result = await resume_action(message.chat.id)

        await message.reply_text(
            bot_header() + result
        )

    except Exception as error:
        print(f"RESUME ERROR: {error}")

        await message.reply_text(
            "❌ Resume Error"
        )


# ==================================================
# SKIP
# ==================================================

async def skip_action(chat_id):
    if chat_id in skip_in_progress:
        return False, "Skip पहले से चल रहा है।"

    if chat_id not in current_songs:
        return False, "अभी कोई गाना नहीं चल रहा है।"

    skip_in_progress.add(chat_id)

    try:
        await calls.leave_call(chat_id)

        old_song = current_songs.pop(chat_id, None)

        if old_song:
            old_song.clear()
            del old_song

        memory_cleanup(
            chat_id,
            reason="skip"
        )

        if queues.get(chat_id):
            success = await play_next(chat_id)

            if success:
                return True, "⏭️ अगला गाना शुरू हो गया है।"

            return False, "अगला गाना शुरू नहीं हो पाया।"

        return True, "📭 Queue खाली है।"

    finally:
        skip_in_progress.discard(chat_id)


@auto_delete_command
async def skip_command(client, message):
    try:
        ok, result = await skip_action(message.chat.id)

        await message.reply_text(
            bot_header() + result
        )

    except Exception as error:
        print(f"SKIP ERROR: {error}")
        traceback.print_exc()

        await message.reply_text(
            "❌ Skip Error"
        )


# ==================================================
# STOP
# ==================================================

async def stop_action(chat_id):
    if chat_id in stop_in_progress:
        return False, "Stop पहले से चल रहा है।"

    stop_in_progress.add(chat_id)

    try:
        queues[chat_id].clear()

        old_song = current_songs.pop(chat_id, None)

        if old_song:
            old_song.clear()
            del old_song

        try:
            await calls.leave_call(chat_id)
        except Exception as error:
            print(f"⚠️ Leave Call: {error}")

        memory_cleanup(
            chat_id,
            reason="stop"
        )

        return True, "⏹️ Playback बंद और Queue साफ कर दी गई है।"

    finally:
        stop_in_progress.discard(chat_id)


@auto_delete_command
async def stop_command(client, message):
    try:
        ok, result = await stop_action(message.chat.id)

        await message.reply_text(
            bot_header() + result
        )

    except Exception as error:
        print(f"STOP ERROR: {error}")
        traceback.print_exc()

        await message.reply_text(
            "❌ Stop Error"
        )


# ==================================================
# END
# ==================================================

@auto_delete_command
async def end_command(client, message):
    try:
        ok, result = await stop_action(message.chat.id)

        await message.reply_text(
            bot_header() + "🔚 " + result
        )

    except Exception as error:
        print(f"END ERROR: {error}")
        traceback.print_exc()

        await message.reply_text(
            "❌ End Error"
        )


# ==================================================
# QUEUE
# ==================================================

@auto_delete_command
async def queue_command(client, message):
    chat_id = message.chat.id

    await message.reply_text(
        queue_text(chat_id),
        reply_markup=music_keyboard(chat_id),
        disable_web_page_preview=True
    )


# ==================================================
# HOST RESTART
# ==================================================

@auto_delete_command
async def restart_command(client, message):
    # केवल configured owner को अनुमति
    if (
        not message.from_user
        or not OWNER_ID_TEXT.isdigit()
        or str(message.from_user.id) != OWNER_ID_TEXT
    ):
        await message.reply_text(
            "⛔ यह command सिर्फ bot owner चला सकता है।"
        )
        return

    # API key की मौजूदगी जाँचें; secret कभी log न करें
    if not SILLYDEV_API_KEY:
        print("❌ SillyDev API key loaded: False")

        await message.reply_text(
            "❌ SILLYDEV_API_KEY .env में नहीं मिली। "
            "जाँचें कि .env, main.py वाले folder में है।"
        )
        return

    print("🔑 SillyDev API key loaded: True")
    print(f"🖥️ SillyDev server ID configured: {bool(SILLYDEV_SERVER_ID)}")

    await message.reply_text(
        "🔄 SillyDev server restart request भेज रहा हूँ..."
    )

    def request_restart():
        return requests.post(
            f"{SILLYDEV_API_URL}/servers/{SILLYDEV_SERVER_ID}/power",
            headers={
                "Authorization": f"Bearer {SILLYDEV_API_KEY}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={"signal": "restart"},
            timeout=15,
        )

    try:
        response = await asyncio.to_thread(request_restart)

        # Restart endpoint सफल होने पर 204 लौटाता है
        if response.status_code == 204:
            print("✅ SillyDev restart request accepted.")

            await telegram_send(
                message.chat.id,
                "✅ SillyDev ने restart request स्वीकार कर ली।"
            )
            return

        # Response body में API key कभी print न करें
        response_text = (response.text or "")[:500]

        print(
            f"❌ SillyDev API response: "
            f"{response.status_code} | {response_text}"
        )

        if response.status_code == 401:
            user_message = (
                "❌ SillyDev ने API key स्वीकार नहीं की (401). "
                "Secret key सही, पूरी और active है या नहीं जाँचें।"
            )

        elif response.status_code == 403:
            user_message = (
                "❌ API key के पास restart permission नहीं है (403). "
                "Key में सही server और restart permission जाँचें।"
            )

        elif response.status_code == 404:
            user_message = (
                "❌ Server नहीं मिला या इस key को उस server का access नहीं है (404). "
                "Server ID और key का server access जाँचें।"
            )

        else:
            user_message = (
                f"❌ SillyDev restart request असफल रही "
                f"({response.status_code}). Server logs जाँचें।"
            )

        await telegram_send(
            message.chat.id,
            user_message
        )

    except requests.RequestException as error:
        print(
            f"❌ SillyDev restart API connection error: "
            f"{type(error).__name__}: {error}"
        )

        try:
            await telegram_send(
                message.chat.id,
                "❌ SillyDev API से संपर्क नहीं हो पाया।"
            )
        except Exception:
            pass


# ==================================================
# CALLBACK BUTTONS
# ==================================================

async def music_callback(client, callback_query):
    data = callback_query.data.split(":")

    if len(data) != 3:
        await callback_query.answer(
            "Invalid Button",
            show_alert=True
        )
        return

    _, action, target_chat = data

    try:
        target_chat = int(target_chat)

    except ValueError:
        await callback_query.answer(
            "Invalid Chat",
            show_alert=True
        )
        return

    if callback_query.message.chat.id != target_chat:
        await callback_query.answer(
            "यह बटन दूसरे ग्रुप का है।",
            show_alert=True
        )
        return

    try:
        if action == "pause":
            ok, result = await pause_action(target_chat)

        elif action == "resume":
            ok, result = await resume_action(target_chat)

        elif action == "skip":
            ok, result = await skip_action(target_chat)

        elif action == "stop":
            ok, result = await stop_action(target_chat)

        elif action == "end":
            ok, result = await stop_action(target_chat)

        elif action in ("queue", "refresh"):
            await callback_query.message.edit_text(
                queue_text(target_chat),
                reply_markup=music_keyboard(target_chat),
                disable_web_page_preview=True
            )

            await callback_query.answer(
                "📋 Queue Updated"
            )
            return

        else:
            await callback_query.answer(
                "Unknown Button",
                show_alert=True
            )
            return

        await callback_query.answer(result)

        if action in ("stop", "end"):
            await callback_query.message.edit_text(
                bot_header()
                + "⏹️ <b>PLAYBACK ENDED</b>\n"
                + "📭 Queue साफ कर दी गई है।",
                reply_markup=None
            )

        elif action == "skip":
            current = current_songs.get(target_chat)

            if current:
                await callback_query.message.edit_text(
                    now_playing_text(current),
                    reply_markup=music_keyboard(target_chat)
                )

            else:
                await callback_query.message.edit_text(
                    bot_header() + result,
                    reply_markup=None
                )

        elif action in ("pause", "resume"):
            current = current_songs.get(target_chat)

            if current:
                await callback_query.message.edit_text(
                    now_playing_text(current),
                    reply_markup=music_keyboard(target_chat)
                )

    except Exception as error:
        print(f"❌ CALLBACK ERROR: {error}")
        traceback.print_exc()

        try:
            await callback_query.answer(
                "❌ Action पूरा नहीं हुआ।",
                show_alert=True
            )
        except Exception:
            pass


# ==================================================
# MAIN
# ==================================================

async def main():
    global bot, user, calls

    print("🤖 PANDEY MUSIC BOT Starting...")

    bot = Client(
        "PandeyMusicBot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN
    )

    user = Client(
        "UserSession",
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
        ("queue", queue_command),
        ("restart", restart_command)
    ]

    try:
        await bot.start()
        print("✅ Bot Started")

        await user.start()
        print("✅ User Session Started")

        me = await user.get_me()

        print(f"👤 Account: {me.first_name}")

        if me.is_bot:
            raise RuntimeError(
                "UserSession में Normal User Account का session चाहिए।"
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

            print(f"✅ /{command_name} Registered")

        bot.add_handler(
            CallbackQueryHandler(
                music_callback,
                filters.regex(r"^music:")
            )
        )

        print("✅ Instant Auto Delete Enabled")
        print("✅ Memory Cleanup Enabled")
        print("✅ FloodWait Handling Enabled")
        print("✅ Auto Recovery Enabled")
        print("✅ Music Buttons Registered")
        print("🎵 PANDEY MUSIC BOT ONLINE!")
        print("👑 POWERED BY @The_PANDEY_G")
        print("🌐 Multi-Group Queue Enabled!")

        await asyncio.Event().wait()

    except Exception as error:
        print(f"❌ MAIN ERROR: {error}")
        traceback.print_exc()
        raise

    finally:
        print("🛑 Bot बंद हो रहा है...")

        # Shutdown पर current songs और queues के references हटाएँ
        for song in list(current_songs.values()):
            if isinstance(song, dict):
                song.clear()

        current_songs.clear()

        for queue in list(queues.values()):
            queue.clear()

        queues.clear()

        playback_starting.clear()
        skip_in_progress.clear()
        stop_in_progress.clear()

        try:
            if calls:
                await calls.stop()

        except Exception:
            traceback.print_exc()

        try:
            if user and user.is_connected:
                await user.stop()

        except Exception:
            traceback.print_exc()

        try:
            if bot and bot.is_connected:
                await bot.stop()

        except Exception:
            traceback.print_exc()

        try:
            http.close()
        except Exception:
            pass

        cleanup_all()

        bot = None
        user = None
        calls = None


# ==================================================
# AUTO RESTART SUPERVISOR
# ==================================================

async def run_forever():
    restart_delay = RESTART_DELAY

    while True:
        try:
            await main()

            print("⚠️ Bot unexpectedly stopped.")

        except Exception as error:
            print(
                f"❌ BOT CRASH: "
                f"{type(error).__name__}: {error}"
            )

            traceback.print_exc()

        print(
            f"🔄 Bot {restart_delay} सेकंड बाद Restart होगा..."
        )

        await asyncio.sleep(restart_delay)

        restart_delay = min(
            restart_delay * 2,
            MAX_RESTART_DELAY
        )


# ==================================================
# RUN
# ==================================================

if __name__ == "__main__":
    try:
        asyncio.run(run_forever())

    except KeyboardInterrupt:
        print("🛑 Bot manually बंद किया गया।")
