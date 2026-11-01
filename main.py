import os
import asyncio
import requests
import yt_dlp

from collections import defaultdict, deque
from dotenv import load_dotenv

from pyrogram import Client, filters
from pytgcalls import PyTgCalls, filters as call_filters
from pytgcalls.types import MediaStream, AudioQuality, StreamEnded


# ---------------- CONFIG ----------------

load_dotenv("/home/container/.env")

API_ID_TEXT = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()

if not API_ID_TEXT.isdigit() or not API_HASH or not BOT_TOKEN:
    raise ValueError("API_ID, API_HASH और BOT_TOKEN .env में जाँचें।")

API_ID = int(API_ID_TEXT)


# ---------------- CLIENTS ----------------

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


# ---------------- QUEUES ----------------

queues = defaultdict(deque)
current_songs = {}
chat_locks = defaultdict(asyncio.Lock)


# ---------------- YOUTUBE SEARCH ----------------

def search_youtube(query):

    if not YOUTUBE_API_KEY:
        raise ValueError("YOUTUBE_API_KEY .env में नहीं मिली।")

    response = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": 1,
            "key": YOUTUBE_API_KEY
        },
        timeout=20
    )

    response.raise_for_status()

    items = response.json().get("items", [])

    if not items:
        return None

    item = items[0]
    video_id = item["id"]["videoId"]

    return {
        "title": item["snippet"]["title"],
        "url": f"https://www.youtube.com/watch?v={video_id}"
    }


# ---------------- EXTRACT AUDIO ----------------

def extract_audio_url(video_url):

    options = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True
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

        return audio_url


# ---------------- PLAY NEXT ----------------

async def play_next(chat_id):

    async with chat_locks[chat_id]:

        if not queues[chat_id]:
            current_songs.pop(chat_id, None)
            return False

        song = queues[chat_id].popleft()

        try:

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
                f"🎵 अब बज रहा है:\n{song['title']}"
            )

            return True

        except Exception as error:

            print(
                f"Play Error in Group {chat_id}: "
                f"{type(error).__name__}: {error}"
            )

            current_songs.pop(chat_id, None)

            await bot.send_message(
                chat_id,
                "❌ गाना नहीं चल पाया। Console में Error जाँचें।"
            )

            return False


# ---------------- STREAM END ----------------

@calls.on_update(call_filters.stream_end())
async def stream_ended_handler(_, update: StreamEnded):

    chat_id = update.chat_id

    current_songs.pop(chat_id, None)

    if queues[chat_id]:
        await play_next(chat_id)
    else:
        await bot.send_message(
            chat_id,
            "🎵 Queue समाप्त हो गई।"
        )


# ---------------- START COMMAND ----------------

@bot.on_message(filters.command("start"))
async def start_command(client, message):

    await message.reply_text(
        "🎵 Pandey Music Bot ऑनलाइन है!\n\n"
        "Commands:\n"
        "/play गाने का नाम\n"
        "/pause - गाना रोकें\n"
        "/resume - गाना फिर चलाएँ\n"
        "/skip - अगला गाना\n"
        "/stop - Playback बंद करें\n"
        "/queue - Queue देखें"
    )


# ---------------- PLAY COMMAND ----------------

@bot.on_message(filters.command("play"))
async def play_command(client, message):

    if len(message.command) < 2:
        await message.reply_text("उदाहरण: /play Kesariya")
        return

    query = " ".join(message.command[1:])

    status = await message.reply_text("🔎 गाना खोज रहा हूँ...")

    try:

        song = await asyncio.to_thread(
            search_youtube,
            query
        )

        if not song:
            await status.edit_text("❌ कोई गाना नहीं मिला।")
            return

        chat_id = message.chat.id

        async with chat_locks[chat_id]:

            if chat_id in current_songs:

                queues[chat_id].append(song)

                position = len(queues[chat_id])

                await status.edit_text(
                    f"✅ Queue में जोड़ा गया:\n"
                    f"{song['title']}\n"
                    f"स्थान: {position}"
                )
                return

            queues[chat_id].appendleft(song)

        await status.edit_text(
            f"▶️ गाना चलाने की कोशिश:\n{song['title']}"
        )

        success = await play_next(chat_id)

        if not success:
            await status.edit_text(
                "❌ गाना नहीं चल पाया। Console Error जाँचें।"
            )

    except Exception as error:

        print(
            f"Play Command Error: "
            f"{type(error).__name__}: {error}"
        )

        await status.edit_text(
            "❌ गाना खोजने या चलाने में समस्या आई।"
        )


# ---------------- PAUSE COMMAND ----------------

@bot.on_message(filters.command("pause"))
async def pause_command(client, message):

    try:

        await calls.pause(message.chat.id)

        await message.reply_text("⏸️ गाना रोक दिया गया।")

    except Exception as error:

        print(f"Pause Error: {error}")

        await message.reply_text("❌ गाना Pause नहीं हुआ।")


# ---------------- RESUME COMMAND ----------------

@bot.on_message(filters.command("resume"))
async def resume_command(client, message):

    try:

        await calls.resume(message.chat.id)

        await message.reply_text("▶️ गाना फिर शुरू हो गया।")

    except Exception as error:

        print(f"Resume Error: {error}")

        await message.reply_text("❌ गाना Resume नहीं हुआ।")


# ---------------- SKIP COMMAND ----------------

@bot.on_message(filters.command("skip"))
async def skip_command(client, message):

    chat_id = message.chat.id

    try:

        await calls.leave_call(chat_id)

        current_songs.pop(chat_id, None)

        success = await play_next(chat_id)

        if not success:
            await message.reply_text(
                "📭 Queue में अगला गाना नहीं है।"
            )

    except Exception as error:

        print(f"Skip Error: {error}")

        await message.reply_text(
            "❌ Skip करने में समस्या आई।"
        )


# ---------------- STOP COMMAND ----------------

@bot.on_message(filters.command("stop"))
async def stop_command(client, message):

    chat_id = message.chat.id

    try:
        await calls.leave_call(chat_id)

    except Exception as error:
        print(f"Stop Error: {error}")

    current_songs.pop(chat_id, None)
    queues[chat_id].clear()

    await message.reply_text(
        "⏹️ Playback बंद और Queue खाली कर दी गई।"
    )


# ---------------- QUEUE COMMAND ----------------

@bot.on_message(filters.command("queue"))
async def queue_command(client, message):

    chat_id = message.chat.id

    current = current_songs.get(chat_id)
    upcoming = list(queues[chat_id])

    if not current and not upcoming:
        await message.reply_text("📭 Queue खाली है।")
        return

    queue_text = "🎵 **Music Queue**\n\n"

    if current:
        queue_text += (
            f"▶️ अभी बज रहा है:\n"
            f"{current['title']}\n\n"
        )

    if upcoming:

        queue_text += "आने वाले गाने:\n"

        for index, song in enumerate(upcoming[:20], start=1):
            queue_text += f"{index}. {song['title']}\n"

        if len(upcoming) > 20:
            queue_text += (
                f"\nऔर {len(upcoming) - 20} "
                "गाने Queue में हैं।"
            )

    await message.reply_text(queue_text)


# ---------------- MAIN ----------------

async def main():

    print("🤖 Pandey Music Bot शुरू हो रहा है...")

    # Start Bot Client
    await bot.start()
    print("✅ Bot Client Started")

    # Start User Client
    await user.start()

    me = await user.get_me()

    print("--------------------------------")
    print("SESSION ACCOUNT:", me.first_name)
    print("SESSION USER ID:", me.id)
    print("SESSION IS BOT:", me.is_bot)
    print("--------------------------------")

    # User Session must be a normal user account
    if me.is_bot:
        raise RuntimeError(
            "UserSession में Bot Account का session है। "
            "सामान्य Telegram User Account का session इस्तेमाल करें।"
        )

    # Start Voice Calls
    await calls.start()

    print("✅ PyTgCalls Started")
    print("🎵 Pandey Music Bot Online!")
    print("🌐 Multi-Group Queue Enabled!")

    await asyncio.Event().wait()


# ---------------- RUN ----------------

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot बंद किया गया।")
