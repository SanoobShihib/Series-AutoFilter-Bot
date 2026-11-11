import os
import re
import asyncio
from datetime import datetime

from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId

# Fix for newer Python versions
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client, filters, idle
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton



# ================= CONFIG =================

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]

ADMIN_IDS = [
    int(x.strip())
    for x in os.environ.get("ADMIN_IDS", "").split(",")
    if x.strip()
]

PORT = int(os.environ.get("PORT", 10000))


# ================= DATABASE =================

mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo["SeriesBot"]

series_col = db["series"]
episodes_col = db["episodes"]
states_col = db["states"]


# ================= BOT =================

app = Client(
    "SeriesBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)


# ================= HELPERS =================

def normalize(text):
    return re.sub(r"\s+", " ", text.strip()).lower()


def parse_caption(caption):
    fields = {}

    for line in caption.splitlines():
        if ":" not in line:
            continue

        key, value = line.split(":", 1)
        fields[key.strip().lower()] = value.strip()

    required = [
        "series name",
        "language",
        "season",
        "quality",
        "episode"
    ]

    for key in required:
        if not fields.get(key):
            raise ValueError(f"Missing field: {key}")

    season = re.fullmatch(r"\d+", fields["season"])
    episode = re.fullmatch(r"\d+", fields["episode"])

    if not season or not episode:
        raise ValueError("Season and Episode must be numbers.")

    return {
        "series_name": fields["series name"],
        "series_key": normalize(fields["series name"]),
        "language": fields["language"],
        "season": int(fields["season"]),
        "quality": fields["quality"],
        "episode": int(fields["episode"])
    }


def make_buttons(items, prefix):
    buttons = []

    for index, item in enumerate(items):
        buttons.append([
            InlineKeyboardButton(
                str(item),
                callback_data=f"{prefix}:{index}"
            )
        ])

    return InlineKeyboardMarkup(buttons)


async def get_options(series_id, field, extra=None):
    query = {"series_id": series_id}

    if extra:
        query.update(extra)

    values = await episodes_col.distinct(field, query)

    if field in ("season", "episode"):
        values = sorted(values)
    else:
        values = sorted(values, key=lambda x: str(x).lower())

    return values


async def get_state(user_id):
    return await states_col.find_one({"user_id": user_id})


async def update_state(user_id, data):
    await states_col.update_one(
        {"user_id": user_id},
        {"$set": data},
        upsert=True
    )


# ================= START =================

@app.on_message(filters.command("start"))
async def start_command(client, message):
    await message.reply_text(
        "👋 Welcome to Series Bot!\n\n"
        "Send a series name to search episodes."
    )


# ================= ADMIN UPLOAD =================

@app.on_message(
    filters.private
    & filters.user(ADMIN_IDS)
    & (filters.video | filters.document)
)
async def save_episode(client, message):

    media = message.video or message.document

    if not media:
        return

    if message.document:
        mime = message.document.mime_type or ""
        if not mime.startswith("video/"):
            await message.reply_text("❌ Please upload a video file.")
            return

    if not message.caption:
        await message.reply_text(
            "❌ Caption missing!\n\n"
            "Use this format:\n\n"
            "Series Name: Stranger Things\n"
            "Language: English\n"
            "Season: 02\n"
            "Quality: 720p\n"
            "Episode: 05"
        )
        return

    try:
        data = parse_caption(message.caption)
    except ValueError as error:
        await message.reply_text(f"❌ {error}")
        return

    series = await series_col.find_one_and_update(
        {"series_key": data["series_key"]},
        {
            "$set": {
                "title": data["series_name"],
                "series_key": data["series_key"]
            },
            "$setOnInsert": {
                "created_at": datetime.utcnow()
            }
        },
        upsert=True,
        return_document=True
    )

    if not series:
        series = await series_col.find_one(
            {"series_key": data["series_key"]}
        )

    episode_data = {
        "series_id": series["_id"],
        "series_name": data["series_name"],
        "series_key": data["series_key"],
        "language": data["language"],
        "season": data["season"],
        "quality": data["quality"],
        "episode": data["episode"],
        "file_id": media.file_id,
        "file_type": "video" if message.video else "document",
        "created_at": datetime.utcnow()
    }

    await episodes_col.insert_one(episode_data)

    await message.reply_text(
        "✅ Episode saved successfully!\n\n"
        f"📺 Series: {data['series_name']}\n"
        f"🌐 Language: {data['language']}\n"
        f"📂 Season: {data['season']:02d}\n"
        f"🎞 Quality: {data['quality']}\n"
        f"🎬 Episode: {data['episode']:02d}"
    )


# ================= SERIES SEARCH =================

@app.on_message(filters.text & ~filters.command(["start"]))
async def search_series(client, message):

    query = message.text.strip()

    if len(query) < 2:
        return

    results = await series_col.find({
        "title": {"$regex": re.escape(query), "$options": "i"}
    }).limit(10).to_list(length=10)

    if not results:
        await message.reply_text("❌ No series found.")
        return

    buttons = []

    for item in results:
        buttons.append([
            InlineKeyboardButton(
                item["title"],
                callback_data=f"s:{item['_id']}"
            )
        ])

    await message.reply_text(
        "🔎 Select your series:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ================= SERIES CALLBACK =================

@app.on_callback_query(filters.regex(r"^s:"))
async def select_series(client, query):

    series_id = query.data.split(":", 1)[1]

    try:
        series_id = ObjectId(series_id)
    except Exception:
        await query.answer("Invalid series.", show_alert=True)
        return

    series = await series_col.find_one({"_id": series_id})

    if not series:
        await query.answer("Series not found.", show_alert=True)
        return

    languages = await get_options(series_id, "language")

    if not languages:
        await query.answer("No episodes found.", show_alert=True)
        return

    await update_state(
        query.from_user.id,
        {
            "series_id": series_id,
            "series_name": series["title"],
            "language": None,
            "season": None,
            "quality": None
        }
    )

    await query.message.edit_text(
        f"📺 {series['title']}\n\nSelect Language:",
        reply_markup=make_buttons(languages, "l")
    )

    await query.answer()


# ================= LANGUAGE CALLBACK =================

@app.on_callback_query(filters.regex(r"^l:\d+$"))
async def select_language(client, query):

    state = await get_state(query.from_user.id)

    if not state:
        await query.answer("Please search again.", show_alert=True)
        return

    languages = await get_options(state["series_id"], "language")
    index = int(query.data.split(":")[1])

    if index >= len(languages):
        await query.answer("Invalid selection.")
        return

    language = languages[index]

    seasons = await get_options(
        state["series_id"],
        "season",
        {"language": language}
    )

    await update_state(
        query.from_user.id,
        {"language": language}
    )

    await query.message.edit_text(
        f"🌐 Language: {language}\n\nSelect Season:",
        reply_markup=make_buttons(seasons, "sn")
    )

    await query.answer()


# ================= SEASON CALLBACK =================

@app.on_callback_query(filters.regex(r"^sn:\d+$"))
async def select_season(client, query):

    state = await get_state(query.from_user.id)

    if not state:
        await query.answer("Please search again.", show_alert=True)
        return

    seasons = await get_options(
        state["series_id"],
        "season",
        {"language": state["language"]}
    )

    index = int(query.data.split(":")[1])

    if index >= len(seasons):
        await query.answer("Invalid selection.")
        return

    season = seasons[index]

    qualities = await get_options(
        state["series_id"],
        "quality",
        {
            "language": state["language"],
            "season": season
        }
    )

    await update_state(
        query.from_user.id,
        {"season": season}
    )

    await query.message.edit_text(
        f"📂 Season: {season:02d}\n\nSelect Quality:",
        reply_markup=make_buttons(qualities, "q")
    )

    await query.answer()


# ================= QUALITY CALLBACK =================

@app.on_callback_query(filters.regex(r"^q:\d+$"))
async def select_quality(client, query):

    state = await get_state(query.from_user.id)

    if not state:
        await query.answer("Please search again.", show_alert=True)
        return

    qualities = await get_options(
        state["series_id"],
        "quality",
        {
            "language": state["language"],
            "season": state["season"]
        }
    )

    index = int(query.data.split(":")[1])

    if index >= len(qualities):
        await query.answer("Invalid selection.")
        return

    quality = qualities[index]

    episodes = await get_options(
        state["series_id"],
        "episode",
        {
            "language": state["language"],
            "season": state["season"],
            "quality": quality
        }
    )

    await update_state(
        query.from_user.id,
        {"quality": quality}
    )

    await query.message.edit_text(
        f"🎞 Quality: {quality}\n\nSelect Episode:",
        reply_markup=make_buttons(
            [f"Episode {ep:02d}" for ep in episodes],
            "e"
        )
    )

    await query.answer()


# ================= EPISODE CALLBACK =================

@app.on_callback_query(filters.regex(r"^e:\d+$"))
async def select_episode(client, query):

    state = await get_state(query.from_user.id)

    if not state:
        await query.answer("Please search again.", show_alert=True)
        return

    episodes = await get_options(
        state["series_id"],
        "episode",
        {
            "language": state["language"],
            "season": state["season"],
            "quality": state["quality"]
        }
    )

    index = int(query.data.split(":")[1])

    if index >= len(episodes):
        await query.answer("Invalid episode.")
        return

    episode_number = episodes[index]

    episode = await episodes_col.find_one({
        "series_id": state["series_id"],
        "language": state["language"],
        "season": state["season"],
        "quality": state["quality"],
        "episode": episode_number
    })

    if not episode:
        await query.answer("File not found.", show_alert=True)
        return

    await query.answer("Sending episode...")

    caption = (
        f"📺 {state['series_name']}\n"
        f"🌐 Language: {state['language']}\n"
        f"📂 Season: {state['season']:02d}\n"
        f"🎞 Quality: {state['quality']}\n"
        f"🎬 Episode: {episode_number:02d}"
    )

    if episode["file_type"] == "video":
        await client.send_video(
            query.message.chat.id,
            episode["file_id"],
            caption=caption
        )
    else:
        await client.send_document(
            query.message.chat.id,
            episode["file_id"],
            caption=caption
        )


# ================= RENDER WEB SERVER =================

async def health(request):
    return web.Response(text="Series Bot is running!")


async def main():

    await app.start()

    web_app = web.Application()
    web_app.router.add_get("/", health)

    runner = web.AppRunner(web_app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    print("Series Bot Started Successfully!")

    try:
        await idle()
    finally:
        await runner.cleanup()
        await app.stop()
        mongo.close()


if __name__ == "__main__":
    app.run(main())
