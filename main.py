
import os
os.environ.setdefault("MALLOC_ARENA_MAX", "2")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import asyncio
import html
import traceback
from collections import defaultdict, deque
from urllib.parse import urlparse

from dotenv import load_dotenv
from pyrogram import Client, filters, enums
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from pytgcalls import PyTgCalls, filters as call_filters
from pytgcalls.types import MediaStream, AudioQuality, StreamEnded


# ================ CONFIG ================

load_dotenv("/home/container/.env")

API_ID = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

if not API_ID.isdigit() or not API_HASH or not BOT_TOKEN:
    raise ValueError("API_ID, API_HASH और BOT_TOKEN .env में जाँचें।")

API_ID = int(API_ID)

# Unlimited queue
MAX_QUEUE_SIZE = None

bot = None
user = None
calls = None

queues = defaultdict(deque)
current_songs = {}
now_playing_messages = {}

chat_locks = defaultdict(asyncio.Lock)
stop_in_progress = set()


# ================ HELPERS ================

def valid_audio_url(url):
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False

        if not parsed.netloc:
            return False

        # YouTube links direct audio files नहीं होते।
        if parsed.hostname and (
            parsed.hostname.endswith("youtube.com")
            or parsed.hostname.endswith("youtu.be")
        ):
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

    result = "🎵 **PANDЕY MUSIC QUEUE**\n\n"

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


# ================ PLAYBACK ================

async def start_song(chat_id, song):
    await calls.play(
        chat_id,
        MediaStream(
            song["url"],
            audio_parameters=AudioQuality.LOW
        )
    )

    current_songs[chat_id] = song

    try:
        await send_now_playing(chat_id, song)
    except Exception as error:
        print("NOW PLAYING ERROR:", error)


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
                print("SONG ERROR:", error)
                traceback.print_exc()

                try:
                    await bot.send_message(
                        chat_id,
                        f"⚠️ यह audio नहीं चल पाया:\n{song['title']}"
                    )
                except Exception:
                    pass

        current_songs.pop(chat_id, None)
        return False


async def skip_music(chat_id):
    if chat_id not in current_songs:
        return False

    if not queues[chat_id]:
        try:
            await calls.pause_stream(chat_id)
        except Exception:
            pass

        current_songs.pop(chat_id, None)
        return False

    async with chat_locks[chat_id]:
        while queues[chat_id]:
            song = queues[chat_id].popleft()

            try:
                # Same VC में नया stream पुराने stream को replace करता है।
                await start_song(chat_id, song)
                return True
            except Exception as error:
                print("SKIP ERROR:", error)
                traceback.print_exc()

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
            print("LEAVE CALL:", error)

    finally:
        stop_in_progress.discard(chat_id)


# ================ STREAM END ================

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


# ================ COMMANDS ================

async def start_command(client, message):
    await message.reply_text(
        "🎵 **Pandey Music Bot Online!**\n\n"
        "अनुमति वाले direct audio URL चलाने के लिए:\n"
        "`/play https://example.com/song.mp3`\n\n"
        "/pause - Pause\n"
        "/resume - Resume\n"
        "/skip - Skip\n"
        "/stop - Stop\n"
        "/queue - Queue"
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
            "YouTube वीडियो लिंक इस संस्करण में समर्थित नहीं है।"
        )
        return

    requester = message.from_user
    title = urlparse(url).path.split("/")[-1] or "Authorized Audio"

    song = {
        "title": title,
        "url": url,
        "requester_name": (
            requester.first_name if requester else "Group User"
        )
    }

    chat_id = message.chat.id
    queues[chat_id].append(song)

    if chat_id in current_songs:
        await message.reply_text(
            f"✅ Queue में जोड़ा गया:\n{title}\n"
            f"स्थान: {len(queues[chat_id])}"
        )
        return

    status = await message.reply_text("⚡ Audio शुरू हो रहा है...")
    success = await play_next(chat_id)

    try:
        await status.delete() if success else status.edit_text(
            "❌ Audio शुरू नहीं हो पाया। URL और server access जाँचें।"
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
        print("PAUSE ERROR:", error)
        await message.reply_text("Pause नहीं हो पाया।")


async def resume_command(client, message):
    chat_id = message.chat.id
    if chat_id not in current_songs:
        await message.reply_text("अभी कोई गाना नहीं चल रहा।")
        return

    try:
        await calls.resume_stream(chat_id)
        await message.reply_text("▶️ Resume हो गया।")
    except Exception as error:
        print("RESUME ERROR:", error)
        await message.reply_text("Resume नहीं हो पाया।")


async def skip_command(client, message):
    success = await skip_music(message.chat.id)
    await message.reply_text(
        "⏭️ अगला गाना शुरू हो गया।"
        if success else "📭 अगला गाना उपलब्ध नहीं है।"
    )


async def stop_command(client, message):
    await stop_music(message.chat.id)
    await message.reply_text("⏹️ Playback बंद और Queue साफ।")


async def queue_command(client, message):
    await message.reply_text(queue_text(message.chat.id))


# ================ CALLBACKS ================

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
            await query.answer(
                "⏭️ Skip हो गया।" if success else "अगला गाना नहीं है।"
            )

        elif action == "stop":
            await stop_music(chat_id)
            await query.answer("⏹️ Music बंद।")
            await bot.send_message(chat_id, "Queue साफ कर दी गई।")

        elif action == "queue":
            await query.answer("Queue भेज दी गई।")
            await bot.send_message(chat_id, queue_text(chat_id))

    except Exception as error:
        print("BUTTON ERROR:", error)
        await query.answer("Action पूरा नहीं हो पाया।", show_alert=True)


# ================ AUTO DELETE ================

def auto_delete(callback):
    async def wrapped(client, message):
        try:
            await message.delete()
            print("🗑️ Command deleted")
        except Exception:
            pass

        return await callback(client, message)

    return wrapped


# ================ MAIN ================

async def main():
    global bot, user, calls

    print("🤖 Pandey Music Bot Starting...")

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

        calls.on_update(call_filters.stream_end())(
            stream_ended_handler
        )

        for command, callback in handlers.items():
            bot.add_handler(
                MessageHandler(
                    auto_delete(callback),
                    filters.command(command)
                )
            )
            print(f"✅ /{command} registered")

        bot.add_handler(
            CallbackQueryHandler(
                music_callback,
                filters.regex(r"^music:")
            )
        )

        print("🎵 Pandey Music Bot ONLINE")
        await asyncio.Event().wait()

    finally:
        try:
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
            print("BOT ERROR:", error)
            traceback.print_exc()
            import time
            time.sleep(10)
