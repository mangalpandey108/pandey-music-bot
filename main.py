import os
import asyncio
from collections import deque
from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.types import Message
from pyrogram.errors import UserNotParticipant, FloodWait
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, AudioQuality
from pytgcalls.types.stream import StreamEnded
import yt_dlp

# .env फ़ाइल से वेरिएबल्स लोड करें
load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")

# Bot और Assistant Clients
bot = Client("MusicBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
assistant = Client("Assistant", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING)
call_py = PyTgCalls(assistant)

# हर ग्रुप का स्वतंत्र Queue और Playback
queues = {}
ASSISTANT_ID = None

# तेज़ YouTube सर्च के लिए yt-dlp विकल्प
ydl_opts = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch1",
    "extract_flat": "in_playlist",
}

def format_duration(seconds) -> str:
    if not seconds:
        return "Live"
    seconds = int(seconds)
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    return f"{hours}:{mins:02d}:{secs:02d}" if hours > 0 else f"{mins}:{secs:02d}"

def search_youtube(query: str):
    """YouTube से गाने का टाइटल, अवधि और असली URL निकालता है।"""
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(query, download=False)
        if not info:
            return None
        if "entries" in info:
            entries = list(info["entries"])
            if not entries:
                return None
            info = entries[0]

        video_url = info.get("webpage_url") or info.get("url")
        if video_url and not video_url.startswith("http"):
            video_url = f"https://youtube.com{video_url}"

        return {
            "title": info.get("title", "Unknown Title"),
            "webpage_url": video_url,
            "duration": format_duration(info.get("duration")),
        }

async def ensure_assistant_in_chat(chat_id: int) -> tuple[bool, str]:
    """Assistant को ग्रुप में चेक करता है और न होने पर इनवाइट लिंक से जोड़ता है।"""
    try:
        await bot.get_chat_member(chat_id, ASSISTANT_ID)
        return True, ""
    except UserNotParticipant:
        try:
            invite_link = await bot.export_chat_invite_link(chat_id)
            await assistant.join_chat(invite_link)
            await asyncio.sleep(1)
            return True, ""
        except FloodWait as e:
            await asyncio.sleep(e.value)
            return True, ""
        except Exception as e:
            return False, f"❌ Assistant ग्रुप जॉइन नहीं कर सका। बॉट को **Invite Users** परमिशन दें या Assistant को खुद ग्रुप में जोड़ें।\n(`{e}`)"
    except Exception as e:
        return False, f"❌ त्रुटि: `{e}`"

async def play_next_song(chat_id: int):
    """Queue का पहला गाना संबंधित ग्रुप के VC में चलाता है।"""
    if chat_id not in queues or not queues[chat_id]:
        try:
            await call_py.leave_call(chat_id)
        except Exception:
            pass
        return

    current_song = queues[chat_id][0]
    try:
        await call_py.play(
            chat_id,
            MediaStream(
                current_song["webpage_url"],
                audio_parameters=AudioQuality.HIGH,
                video_flags=MediaStream.Flags.IGNORE,
            ),
        )
        await bot.send_message(
            chat_id,
            f"▶️ **अब बज रहा है:** {current_song['title']}\n"
            f"⏱ **अवधि:** `{current_song['duration']}`\n"
            f"👤 **अनुरोध:** {current_song['requester']}",
            disable_web_page_preview=True,
        )
    except Exception as e:
        await bot.send_message(
            chat_id,
            f"❌ **गाना चलाने में त्रुटि:** `{e}`\n*(सुनिश्चित करें कि ग्रुप में Voice Chat चालू है)*"
        )
        if chat_id in queues and queues[chat_id]:
            queues[chat_id].popleft()
        await play_next_song(chat_id)

# गाना खत्म होने पर अपने आप अगला गाना चलाने के लिए
@call_py.on_update()
async def on_stream_end(_, update):
    if isinstance(update, StreamEnded):
        chat_id = update.chat_id
        if chat_id in queues and queues[chat_id]:
            queues[chat_id].popleft()
            await play_next_song(chat_id)

# ================= COMMANDS =================

@bot.on_message(filters.command("start"))
async def start_command(_, message: Message):
    await message.reply_text(
        "🎵 **Telegram VC Music Bot चालू है!**\n\n"
        "**उपलब्ध कमांड्स (सिर्फ ग्रुप में):**\n"
        "• `/play <गाने का नाम>` - गाना चलाएं या Queue में जोड़ें\n"
        "• `/skip` - अगला गाना चलाएं\n"
        "• `/pause` - गाना रोकें\n"
        "• `/resume` - गाना फिर से शुरू करें\n"
        "• `/queue` - ग्रुप की प्लेलिस्ट देखें\n"
        "• `/stop` - प्लेबैक बंद करें और VC छोड़ें"
    )

@bot.on_message(filters.command("play") & filters.group)
async def play_command(_, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("❌ कृपया गाने का नाम लिखें!\nउदाहरण: `/play Kesariya`")

    chat_id = message.chat.id
    query = " ".join(message.command[1:])
    requester = message.from_user.mention if message.from_user else "Admin"

    status_msg = await message.reply_text(f"🔍 YouTube पर **{query}** खोजा जा रहा है...")

    joined, err_msg = await ensure_assistant_in_chat(chat_id)
    if not joined:
        return await status_msg.edit_text(err_msg)

    loop = asyncio.get_running_loop()
    song = await loop.run_in_executor(None, search_youtube, query)

    if not song or not song.get("webpage_url"):
        return await status_msg.edit_text("❌ कोई गाना नहीं मिला। कृपया दूसरा नाम या YouTube लिंक ट्राई करें।")

    song["requester"] = requester

    if chat_id not in queues:
        queues[chat_id] = deque()

    queues[chat_id].append(song)

    if len(queues[chat_id]) == 1:
        await status_msg.delete()
        await play_next_song(chat_id)
    else:
        position = len(queues[chat_id]) - 1
        await status_msg.edit_text(
            f"➕ **Queue में जोड़ा गया (#{position}):**\n"
            f"🎵 {song['title']} (`{song['duration']}`)\n"
            f"👤 {requester}",
            disable_web_page_preview=True,
        )

@bot.on_message(filters.command("skip") & filters.group)
async def skip_command(_, message: Message):
    chat_id = message.chat.id
    if chat_id not in queues or not queues[chat_id]:
        return await message.reply_text("❌ अभी कोई गाना नहीं बज रहा है।")

    skipped_song = queues[chat_id].popleft()
    await message.reply_text(f"⏭️ **स्किप किया गया:** {skipped_song['title']}")
    await play_next_song(chat_id)

@bot.on_message(filters.command("pause") & filters.group)
async def pause_command(_, message: Message):
    chat_id = message.chat.id
    try:
        await call_py.pause_stream(chat_id)
        await message.reply_text("⏸️ गाना **Pause** कर दिया गया है।")
    except Exception:
        await message.reply_text("❌ गाना Pause नहीं हो सका।")

@bot.on_message(filters.command("resume") & filters.group)
async def resume_command(_, message: Message):
    chat_id = message.chat.id
    try:
        await call_py.resume_stream(chat_id)
        await message.reply_text("▶️ गाना फिर से **Resume** कर दिया गया है।")
    except Exception:
        await message.reply_text("❌ गाना Resume नहीं हो सका।")

@bot.on_message(filters.command("stop") & filters.group)
async def stop_command(_, message: Message):
    chat_id = message.chat.id
    if chat_id in queues:
        queues[chat_id].clear()
    try:
        await call_py.leave_call(chat_id)
        await message.reply_text("⏹️ प्लेबैक बंद कर दिया गया और इस ग्रुप की Queue खाली कर दी गई।")
    except Exception:
        await message.reply_text("❌ Assistant अभी किसी VC में नहीं है।")

@bot.on_message(filters.command("queue") & filters.group)
async def queue_command(_, message: Message):
    chat_id = message.chat.id
    if chat_id not in queues or not queues[chat_id]:
        return await message.reply_text("📭 इस ग्रुप की Queue खाली है।")

    text = f"📜 **{message.chat.title} की Queue ({len(queues[chat_id])} गाने):**\n\n"
    for idx, song in enumerate(queues[chat_id]):
        if idx == 0:
            text += f"▶️ **अभी बज रहा है:** {song['title']} (`{song['duration']}`) - {song['requester']}\n\n"
        else:
            text += f"**{idx}.** {song['title']} (`{song['duration']}`) - {song['requester']}\n"

    if len(text) > 4000:
        text = text[:3940] + f"\n\n...और कुल {len(queues[chat_id]) - 1} गाने कतार में हैं।"

    await message.reply_text(text, disable_web_page_preview=True)

# ================= STARTUP =================
async def main():
    global ASSISTANT_ID
    await bot.start()
    await assistant.start()
    me = await assistant.get_me()
    ASSISTANT_ID = me.id
    await call_py.start()
    print(f"✅ Music Bot चालू हो गया है! Assistant: {me.first_name} ({ASSISTANT_ID})")
    await idle()
    await bot.stop()
    await assistant.stop()

if __name__ == "__main__":
    asyncio.run(main())
