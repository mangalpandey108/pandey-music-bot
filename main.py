
import os
import asyncio
import traceback
import yt_dlp

from pyrogram import Client, filters, idle
from pyrogram.handlers import MessageHandler
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]


async def main():
    # Clients ko running event loop ke andar banayein
    bot_app = Client(
        "MusicBot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
    )

    user_app = Client(
        "UserSession",
        api_id=API_ID,
        api_hash=API_HASH,
    )

    call_py = PyTgCalls(user_app)

    async def start_command(client, message):
        await message.reply_text(
            "नमस्ते! 🎵 Music Bot ऑनलाइन है।\n"
            "गाना चलाने के लिए /play Kesariya लिखें।"
        )

    async def play_song(client, message):
        if len(message.command) < 2:
            await message.reply_text("उदाहरण: /play Kesariya")
            return

        query = " ".join(message.command[1:])
        status = await message.reply_text(f"🔎 खोज रहे हैं: {query}")

        try:
            options = {
                "format": "bestaudio/best",
                "noplaylist": True,
                "quiet": True,
            }

            with yt_dlp.YoutubeDL(options) as ydl:
                info = await asyncio.to_thread(
                    ydl.extract_info,
                    f"ytsearch1:{query}",
                    download=False,
                )

            entries = info.get("entries") or []
            if not entries:
                await status.edit_text("गाना नहीं मिला।")
                return

            song = entries[0]
            url = song.get("url")
            title = song.get("title", "Unknown")

            if not url:
                await status.edit_text("ऑडियो URL नहीं मिला।")
                return

            await call_py.play(
                message.chat.id,
                MediaStream(url),
            )
            await status.edit_text(f"🎵 चलाने का अनुरोध भेजा: {title}")

        except Exception as error:
            traceback.print_exc()
            await status.edit_text(
                f"❌ Error: {type(error).__name__}: {str(error)[:400]}"
            )

    bot_app.add_handler(
        MessageHandler(start_command, filters.command("start"))
    )
    bot_app.add_handler(
        MessageHandler(play_song, filters.command("play"))
    )

    bot_started = False
    user_started = False
    calls_started = False

    try:
        await bot_app.start()
        bot_started = True

        await user_app.start()
        user_started = True

        await call_py.start()
        calls_started = True

        print("🤖 Music Bot online hai!")
        await idle()

    finally:
        if calls_started:
            await call_py.stop()
        if user_started:
            await user_app.stop()
        if bot_started:
            await bot_app.stop()


if __name__ == "__main__":
    asyncio.run(main())
