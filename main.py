
import os
import asyncio
import traceback
import time
import requests
import yt_dlp

from pathlib import Path
from collections import defaultdict, deque
from html import escape
from functools import wraps

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

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

if not API_ID_TEXT.isdigit() or not API_HASH or not BOT_TOKEN:
    raise ValueError("API_ID, API_HASH और BOT_TOKEN .env में जाँचें।")

API_ID = int(API_ID_TEXT)

BOT_TITLE = "🎵 PANDEY MUSIC BOT"
POWERED_BY = "👑 POWERED BY @The_PANDEY_G"

RESTART_DELAY = 10
MAX_RESTART_DELAY = 60


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
# INSTANT AUTO DELETE
# ==================================================

async def safe_delete(message):
    try:
        await message.delete()
        print("🗑️ Command Message Deleted")

    except Exception as error:
        print(f"⚠️ Message Delete नहीं हुआ: {error}")


def auto_delete_command(func):
    @wraps(func)
    async def wrapper(client, message, *args, **kwargs):

        # Command आते ही सबसे पहले Delete
        await safe_delete(message)

        # Delete के बाद Command Processing
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
    queue = queues[chat_id]

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
    start = time.perf_counter()

    options = {
        "format": "bestaudio[acodec!=none]/bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 10,
        "retries": 1,
        "extractor_retries": 1,
        "fragment_retries": 1,
        "cachedir": False
    }

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

    print(
        f"⚡ Extraction Time: "
        f"{time.perf_counter() - start:.2f}s"
    )

    return audio_url


# ==================================================
# PLAY NEXT
# ==================================================

async def play_next(chat_id):
    if chat_id in playback_starting:
        return False

    playback_starting.add(chat_id)

    try:
        if not queues[chat_id]:
            current_songs.pop(chat_id, None)
            return False

        song = queues[chat_id].popleft()

        audio_url = await asyncio.to_thread(
            extract_audio_url,
            song["url"]
        )

        await calls.play(
            chat_id,
            MediaStream(
                audio_url,
                AudioQuality.HIGH
            )
        )

        current_songs[chat_id] = song

        await bot.send_message(
            chat_id,
            now_playing_text(song),
            reply_markup=music_keyboard(chat_id),
            disable_web_page_preview=True
        )

        print(
            f"🎵 Playing: {song['title']} | "
            f"Requester: {song['requester']} | "
            f"ID: {song.get('requester_id')}"
        )

        return True

    except Exception as error:
        print(
            f"❌ PLAY ERROR {chat_id}: "
            f"{type(error).__name__}: {error}"
        )

        traceback.print_exc()

        current_songs.pop(chat_id, None)

        try:
            await bot.send_message(
                chat_id,
                bot_header()
                + "❌ गाना नहीं चल पाया। "
                + "YouTube या Audio Stream में समस्या हो सकती है।"
            )

        except Exception:
            traceback.print_exc()

        return False

    finally:
        playback_starting.discard(chat_id)


# ==================================================
# STREAM END
# ==================================================

async def stream_ended_handler(_, update: StreamEnded):
    chat_id = update.chat_id

    print(f"🏁 STREAM ENDED: {chat_id}")

    if (
        chat_id in skip_in_progress
        or chat_id in stop_in_progress
    ):
        return

    current_songs.pop(chat_id, None)

    if queues[chat_id]:
        await play_next(chat_id)

    else:
        await bot.send_message(
            chat_id,
            bot_header() + "✅ Queue समाप्त हो गई।"
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
        + "/queue",
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
            await status.delete()

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

        current_songs.pop(chat_id, None)

    finally:
        skip_in_progress.discard(chat_id)

    if queues[chat_id]:
        success = await play_next(chat_id)

        if success:
            return True, "⏭️ अगला गाना शुरू हो गया है।"

        return False, "अगला गाना शुरू नहीं हो पाया।"

    return True, "📭 Queue खाली है।"


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
        current_songs.pop(chat_id, None)

        await calls.leave_call(chat_id)

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
        ("queue", queue_command)
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
        print("✅ Auto Recovery Enabled")
        print("✅ Music Buttons Registered")
        print("🎵 PANDEY MUSIC BOT ONLINE!")
        print("👑 POWERED BY @The_PANDEY_G")
        print("🌐 Multi-Group Queue Enabled!")

        # Restart के बाद बची हुई Queue को फिर शुरू करने की कोशिश
        for chat_id in list(queues.keys()):
            if queues[chat_id]:
                asyncio.create_task(play_next(chat_id))

        await asyncio.Event().wait()

    except Exception as error:
        print(f"❌ MAIN ERROR: {error}")
        traceback.print_exc()
        raise

    finally:
        print("🛑 Bot बंद हो रहा है...")

        # Restart के समय current song को Queue में वापस रखें
        for chat_id, song in list(current_songs.items()):
            if song:
                queues[chat_id].appendleft(song)

        current_songs.clear()
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
