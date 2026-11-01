
import os
import requests
from dotenv import load_dotenv
from pyrogram import Client, filters

# Load variables from .env file
load_dotenv("/home/container/.env")

API_ID_TEXT = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()

print("========== CONFIG CHECK ==========")
print("API_ID मौजूद:", API_ID_TEXT.isdigit())
print("API_HASH मौजूद:", bool(API_HASH))
print("BOT_TOKEN मौजूद:", bool(BOT_TOKEN))
print("YOUTUBE_API_KEY मौजूद:", bool(YOUTUBE_API_KEY))
print("==================================")

if not API_ID_TEXT.isdigit() or not API_HASH or not BOT_TOKEN:
    raise ValueError("अपनी .env file में Telegram credentials जाँचें।")

API_ID = int(API_ID_TEXT)

bot = Client(
    "PandeyMusicBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)


@bot.on_message(filters.command("start"))
async def start_command(client, message):
    await message.reply_text(
        "🎵 Pandey Music Bot ऑनलाइन है!\n\n"
        "गाना खोजने के लिए:\n"
        "/play Kesariya"
    )


@bot.on_message(filters.command("play"))
async def play_command(client, message):
    if not YOUTUBE_API_KEY:
        await message.reply_text("YouTube API Key सेट नहीं है।")
        return

    if len(message.command) < 2:
        await message.reply_text("उदाहरण: /play Kesariya")
        return

    query = " ".join(message.command[1:])

    try:
        response = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "snippet",
                "q": query,
                "type": "video",
                "maxResults": 5,
                "key": YOUTUBE_API_KEY
            },
            timeout=15
        )
        response.raise_for_status()
        data = response.json()

        results = []
        for item in data.get("items", []):
            video_id = item["id"]["videoId"]
            title = item["snippet"]["title"]
            results.append(
                f"🎵 {title}\n"
                f"https://www.youtube.com/watch?v={video_id}"
            )

        await message.reply_text(
            "\n\n".join(results) if results else "कोई वीडियो नहीं मिला।"
        )

    except requests.RequestException as error:
        print("YouTube API Error:", error)
        await message.reply_text("YouTube Search में समस्या आई।")


if __name__ == "__main__":
    print("🤖 Pandey Music Bot शुरू हो रहा है...")
    bot.run()
