import os
import re
import asyncio
import traceback
import time
import requests
import yt_dlp
import unicodedata

from pathlib import Path
from difflib import SequenceMatcher
from collections import defaultdict, deque
from dotenv import load_dotenv

from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler

from pytgcalls import PyTgCalls, filters as call_filters
from pytgcalls.types import MediaStream, AudioQuality, StreamEnded


# ================= CONFIG =================

load_dotenv("/home/container/.env")

API_ID_TEXT = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()

SONGS_DIR = Path("/home/container/songs").resolve()

if not API_ID_TEXT.isdigit() or not API_HASH or not BOT_TOKEN:
    raise ValueError("API_ID, API_HASH और BOT_TOKEN .env में जाँचें।")

API_ID = int(API_ID_TEXT)

SONGS_DIR.mkdir(parents=True, exist_ok=True)


# ================= GLOBALS =================

bot = None
user = None
calls = None

queues = defaultdict(deque)
current_songs = {}

playback_starting = set()
skip_in_progress = set()
stop_in_progress = set()
failed_chats = set()

http = requests.Session()


# ================= LOCAL SONGS =================

def get_local_songs():

    if not SONGS_DIR.exists():
        return []

    return sorted(
        [
            file for file in SONGS_DIR.iterdir()
            if file.is_file() and file.suffix.lower() == ".mp3"
        ],
        key=lambda file: file.name.casefold()
    )


def normalize_song_name(name):

    name = unicodedata.normalize("NFKC", name).casefold()

    name = re.sub(
        r"\.(mp3|m4a|wav|ogg|flac|aac)$",
        "",
        name
    )

    name = re.sub(
        r"[^a-z0-9\u0900-\u097f]+",
        " ",
        name
    )

    return " ".join(name.split())


def find_local_song(query):

    query = normalize_song_name(query)

    if not query:
        return None, []

    songs = get_local_songs()

    if not songs:
        return None, []

    query_words = query.split()
    matches = []

    # Exact filename match सबसे पहले।
    for file in songs:

        filename = normalize_song_name(file.stem)

        if filename == query:
            return file, []

    # Smart matching और spelling correction।
    for file in songs:

        filename = normalize_song_name(file.stem)
        filename_words = filename.split()

        if not filename_words:
            continue

        # एक-एक शब्द का सबसे अच्छा match।
        word_scores = []

        for query_word in query_words:

            best_score = 0.0

            for filename_word in filename_words:

                score = SequenceMatcher(
                    None,
                    query_word,
                    filename_word
                ).ratio()

                if query_word == filename_word:
                    score = 1.0

                best_score = max(best_score, score)

            word_scores.append(best_score)

        matched_words = [
            score for score in word_scores
            if score >= 0.74
        ]

        if not matched_words:
            continue

        # कई शब्द लिखे हों तो अधिकांश का match जरूरी।
        if len(query_words) > 1:

            required_matches = max(
                1,
                len(query_words) - 1
            )

            if len(matched_words) < required_matches:
                continue

        average_score = (
            sum(matched_words) / len(matched_words)
        )

        matches.append((average_score, file))

    if not matches:
        return None, []

    matches.sort(
        key=lambda item: item[0],
        reverse=True
    )

    if len(matches) == 1:
        return matches[0][1], []

    best_score = matches[0][0]
    second_score = matches[1][0]

    # स्पष्ट रूप से बेहतर match हो तो वही चुनें।
    if (
        best_score >= 0.90
        and best_score - second_score >= 0.12
    ):
        return matches[0][1], []

    return None, [
        item[1] for item in matches[:15]
    ]


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
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 2,
        "fragment_retries": 2,
        "cachedir": False
    }

    with yt_dlp.YoutubeDL(options) as ydl:

        info = ydl.extract_info(
            video_url,
            download=False
        )

    if not info:
        raise ValueError("Audio information नहीं मिली।")

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
        raise ValueError("Direct Audio URL उपलब्ध नहीं है।")

    print(
        f"⚡ Audio Extraction Time: "
        f"{time.perf_counter() - started:.2f}s"
    )

    return audio_url


# ================= PLAY NEXT =================

async def play_next(chat_id):

    if chat_id in playback_starting:
        print(f"⏳ Playback पहले से शुरू हो रहा है: {chat_id}")
        return False

    if chat_id in stop_in_progress:
        return False

    if not queues[chat_id]:
        print(f"📭 Queue Empty: {chat_id}")
        return False

    playback_starting.add(chat_id)

    song = queues[chat_id].popleft()

    try:

        started = time.perf_counter()

        print(f"🎧 Trying: {song['title']}")

        if song.get("source") == "local":

            audio_input = song["path"]

            if not os.path.isfile(audio_input):
                raise FileNotFoundError(
                    f"Local MP3 नहीं मिली: {audio_input}"
                )

            print(f"📁 Local MP3: {audio_input}")

        else:

            audio_input = await asyncio.to_thread(
                extract_audio_url,
                song["url"]
            )

        vc_started = time.perf_counter()

        await calls.play(
            chat_id,
            MediaStream(
                audio_input,
                AudioQuality.HIGH
            )
        )

        print(
            f"📡 VC Startup Time: "
            f"{time.perf_counter() - vc_started:.2f}s"
        )

        current_songs[chat_id] = song
        failed_chats.discard(chat_id)

        print(
            f"🎵 Total Playback Start Time: "
            f"{time.perf_counter() - started:.2f}s"
        )

        await bot.send_message(
            chat_id,
            f"🎵 अब बज रहा है:\n{song['title']}"
        )

        return True

    except Exception as error:

        current_songs.pop(chat_id, None)
        failed_chats.add(chat_id)

        print(f"❌ Song Failed: {song.get('title')}")
        print(f"Error: {type(error).__name__}: {error}")

        traceback.print_exc()

        try:

            await bot.send_message(
                chat_id,
                "❌ यह गाना शुरू नहीं हो पाया:\n"
                f"{song.get('title', 'Unknown')}\n\n"
                "अगला गाना अपने-आप नहीं चलाया जाएगा।"
            )

        except Exception:
            traceback.print_exc()

        return False

    finally:
        playback_starting.discard(chat_id)


# ================= STREAM END =================

async def stream_ended_handler(_, update: StreamEnded):

    chat_id = update.chat_id

    print(f"🏁 STREAM ENDED: {chat_id}")

    if chat_id in skip_in_progress:
        return

    if chat_id in stop_in_progress:
        return

    if chat_id in failed_chats:
        return

    current_songs.pop(chat_id, None)

    if queues[chat_id]:
        await play_next(chat_id)

    else:

        await bot.send_message(
            chat_id,
            "🎵 Queue समाप्त हो गई।"
        )


# ================= START COMMAND =================

async def start_command(client, message):

    await message.reply_text(
        "🎵 Pandey Music Bot ऑनलाइन है!\n\n"
        "Commands:\n"
        "/play गाने का नाम\n"
        "/pause - गाना रोकें\n"
        "/resume - गाना फिर चलाएँ\n"
        "/skip - अगला गाना\n"
        "/stop - Playback बंद करें\n"
        "/end - Playback समाप्त करें\n"
        "/queue - Queue देखें\n\n"
        "📁 Local MP3 के लिए /play लिखें।"
    )


# ================= PLAY COMMAND =================

async def play_command(client, message):

    print("🎵 PLAY COMMAND RECEIVED")

    chat_id = message.chat.id

    if len(message.command) < 2:

        songs = get_local_songs()

        if not songs:

            await message.reply_text(
                "📂 songs फ़ोल्डर खाली है।\n"
                "पहले उसमें MP3 फ़ाइल अपलोड करें।"
            )

            return

        text = "📁 **Available Local Songs:**\n\n"

        for number, file in enumerate(
            songs[:20],
            start=1
        ):

            text += f"{number}. {file.name}\n"

        text += "\nगाना चलाने के लिए:\n/play गाने का नाम"

        await message.reply_text(text)
        return

    query = " ".join(message.command[1:]).strip()

    status = await message.reply_text(
        "🔎 Local songs में Smart Search कर रहा हूँ..."
    )

    try:

        started = time.perf_counter()

        local_file, multiple_matches = find_local_song(query)

        if multiple_matches:

            text = "⚠️ कई गाने मिले:\n\n"

            for number, file in enumerate(
                multiple_matches,
                start=1
            ):

                text += f"{number}. {file.name}\n"

            text += (
                "\nकृपया गाने का नाम थोड़ा और स्पष्ट लिखें।"
            )

            await status.edit_text(text)
            return

        if local_file:

            song = {
                "title": local_file.stem,
                "source": "local",
                "path": str(local_file.resolve())
            }

            print(f"📁 Local Song Found: {local_file.name}")

        else:

            await status.edit_text(
                "🔎 Local song नहीं मिली। "
                "YouTube पर खोज रहा हूँ..."
            )

            song = await asyncio.to_thread(
                search_youtube,
                query
            )

            if not song:

                await status.edit_text(
                    "❌ Local या YouTube पर गाना नहीं मिला।"
                )

                return

        print(
            f"🔎 Total Search Time: "
            f"{time.perf_counter() - started:.2f}s"
        )

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
            f"⚡ Audio तैयार किया जा रहा है:\n{song['title']}"
        )

        success = await play_next(chat_id)

        if success:
            await status.delete()

        else:

            await status.edit_text(
                "❌ गाना शुरू नहीं हो पाया। "
                "अगला गाना अपने-आप नहीं चलेगा।"
            )

    except Exception as error:

        print(
            f"❌ PLAY ERROR: "
            f"{type(error).__name__}: {error}"
        )

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

            await message.reply_text(
                "❌ अभी कोई गाना नहीं चल रहा है।"
            )

            return

        await calls.pause_stream(chat_id)

        await message.reply_text(
            "⏸️ गाना रोक दिया गया है।"
        )

    except Exception as error:

        print(f"❌ PAUSE ERROR: {error}")
        traceback.print_exc()

        await message.reply_text(
            f"❌ Pause Error: {type(error).__name__}"
        )


# ================= RESUME COMMAND =================

async def resume_command(client, message):

    chat_id = message.chat.id

    try:

        if chat_id not in current_songs:

            await message.reply_text(
                "❌ अभी कोई गाना नहीं चल रहा है।"
            )

            return

        await calls.resume_stream(chat_id)

        await message.reply_text(
            "▶️ गाना फिर शुरू हो गया है।"
        )

    except Exception as error:

        print(f"❌ RESUME ERROR: {error}")
        traceback.print_exc()

        await message.reply_text(
            f"❌ Resume Error: {type(error).__name__}"
        )


# ================= SKIP COMMAND =================

async def skip_command(client, message):

    chat_id = message.chat.id

    if chat_id in skip_in_progress:

        await message.reply_text(
            "⏳ Skip पहले से चल रहा है।"
        )

        return

    if chat_id not in current_songs:

        await message.reply_text(
            "❌ अभी कोई गाना नहीं चल रहा है।"
        )

        return

    skip_in_progress.add(chat_id)

    try:

        await calls.leave_call(chat_id)

        current_songs.pop(chat_id, None)

        await message.reply_text(
            "⏭️ अगला गाना शुरू किया जा रहा है।"
        )

    except Exception as error:

        print(f"❌ SKIP ERROR: {error}")
        traceback.print_exc()

        await message.reply_text(
            f"❌ Skip Error: {type(error).__name__}"
        )

        return

    finally:
        skip_in_progress.discard(chat_id)

    if queues[chat_id]:
        await play_next(chat_id)

    else:

        await message.reply_text(
            "📭 Queue खाली है।"
        )


# ================= STOP COMMAND =================

async def stop_command(client, message):

    chat_id = message.chat.id

    stop_in_progress.add(chat_id)

    try:

        queues[chat_id].clear()
        current_songs.pop(chat_id, None)
        failed_chats.discard(chat_id)

        await calls.leave_call(chat_id)

        await message.reply_text(
            "⏹️ Playback बंद और Queue साफ कर दी गई है।"
        )

    except Exception as error:

        print(f"❌ STOP ERROR: {error}")
        traceback.print_exc()

        await message.reply_text(
            f"❌ Stop Error: {type(error).__name__}"
        )

    finally:
        stop_in_progress.discard(chat_id)


# ================= END COMMAND =================

async def end_command(client, message):

    await stop_command(client, message)


# ================= QUEUE COMMAND =================

async def queue_command(client, message):

    chat_id = message.chat.id

    queue = queues[chat_id]
    current = current_songs.get(chat_id)

    if not current and not queue:

        await message.reply_text(
            "📭 Queue खाली है।"
        )

        return

    text = "🎵 **Music Queue**\n\n"

    if current:

        text += (
            f"▶️ **अभी बज रहा है:**\n"
            f"{current['title']}\n\n"
        )

    if queue:

        text += "📋 **आने वाले गाने:**\n"

        for number, song in enumerate(
            list(queue)[:10],
            start=1
        ):

            text += f"{number}. {song['title']}\n"

        if len(queue) > 10:

            text += (
                f"\nऔर {len(queue) - 10} गाने बाकी हैं।"
            )

    await message.reply_text(text)


# ================= MAIN =================

async def main():

    global bot, user, calls

    print("🤖 Pandey Music Bot शुरू हो रहा है...")

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
                "FreshLogin में Bot Account का session है। "
                "Normal User Account का session आवश्यक है।"
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

            print(
                f"✅ Handler Registered: /{command_name}"
            )

        print("🎵 Pandey Music Bot Online!")
        print("📁 Local MP3 Playback Enabled!")
        print("🔎 Smart Local Song Search Enabled!")
        print("🌐 YouTube Search Enabled!")
        print("⏭️ Failed song पर auto-next बंद है।")

        await asyncio.Event().wait()

    except Exception as error:

        print(
            f"❌ MAIN ERROR: "
            f"{type(error).__name__}: {error}"
        )

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
