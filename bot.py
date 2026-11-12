import os
import re
import asyncio
import secrets
import aiohttp
from datetime import datetime, timezone

from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from pymongo import ReturnDocument

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

OMDB_API_KEY = os.environ.get("OMDB_API_KEY", "").strip()

# ================= DATABASE =================

mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo["seriesbot"]

series_col = db["series"]
episodes_col = db["episodes"]
states_col = db["states"]
get_files_col = db["get_files_tokens"]
poster_sessions_col = db["poster_sessions"]
users_col = db["users"]
batch_sessions_col = db["batch_sessions"]

# ================= BOT =================

app = Client(
    "SeriesBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)


# ================= WELCOME SETTINGS =================

WELCOME_PHOTO = "https://n.uguu.se/BHoqTojg.jpeg"

GROUP_LINK = "https://t.me/+Ik14BdOewjQzYjI1"
CHANNEL_LINK = "https://t.me/Flim_maxx"


async def send_welcome(client, message):

    me = await client.get_me()

    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "ᴀᴅᴅ ᴍᴇ ᴛᴏ ʏᴏᴜʀ ɢʀᴏᴜᴘ",
                url=f"https://t.me/{me.username}?startgroup=true"
            )
        ],
        [
            InlineKeyboardButton(
                "ᴊᴏɪɴ ᴛᴏ ɢʀᴏᴜᴘ",
                url="https://t.me/+Ik14BdOewjQzYjI1"
            )
        ],
        [
            InlineKeyboardButton(
                "ᴊᴏɪɴ ᴛᴏ ᴄʜᴀɴɴᴇʟ",
                url="https://t.me/Flim_maxx"
            )
        ]
    ])

    caption = (
        "⚡ **ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ sᴇʀɪᴇs ᴢᴏɴᴇ** ⚡\n\n"
        "🤖 **ᴡᴀᴛᴄʜ ʏᴏᴜʀ ғᴀᴠᴏʀɪᴛᴇ sᴇʀɪᴇs ᴀɴʏᴛɪᴍᴇ!**\n\n"
        "🎬 **sᴇᴀʀᴄʜ ғᴏʀ ʏᴏᴜʀ ғᴀᴠᴏʀɪᴛᴇ sᴇʀɪᴇs, "
        "ғɪɴᴅ ᴀᴠᴀɪʟᴀʙʟᴇ ᴇᴘɪsᴏᴅᴇs ᴀɴᴅ ᴅᴏᴡɴʟᴏᴀᴅ ᴛʜᴇᴍ.**\n\n"
        "🔍 **sᴇᴀʀᴄʜ sᴇʀɪᴇs ɴᴀᴍᴇ ɪɴ ᴛʜᴇ ɢʀᴏᴜᴘ!**\n\n"
        "🍿 **sᴇᴀʀᴄʜ • sᴇʟᴇᴄᴛ • ᴇɴᴊᴏʏ**\n\n"
        
        "⟬ [ᴛᴇᴀᴍ ғʟɪᴍ ᴍᴀxx](https://t.me/Flim_maxx) ⟭"
    )

    await message.reply_photo(
        photo=WELCOME_PHOTO,
        caption=caption,
        reply_markup=buttons
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

async def fetch_omdb_metadata(series_name):

    if not OMDB_API_KEY:
        return {}

    url = "https://www.omdbapi.com/"

    try:
        async with aiohttp.ClientSession() as session:

            async with session.get(
                url,
                params={
                    "apikey": OMDB_API_KEY,
                    "t": series_name,
                    "type": "series"
                },
                timeout=aiohttp.ClientTimeout(total=15)
            ) as response:

                data = await response.json()

        if data.get("Response") != "True":
            return {}

        return {
            "year": data.get("Year", "N/A"),
            "genre": data.get("Genre", "N/A"),
            "rating": data.get("imdbRating", "N/A"),
            "poster": (
                data.get("Poster")
                if data.get("Poster") != "N/A"
                else None
            )
        }

    except Exception as e:
        print("OMDb Error:", e)
        return {}


# ================= START =================

@app.on_message(filters.private & filters.command("start"))
async def start_command(client, message):

    now = datetime.now(timezone.utc)

    await users_col.update_one(
        {"user_id": message.from_user.id},
        {
            "$set": {
                "last_start": now
            },
            "$setOnInsert": {
                "first_start": now
            }
        },
        upsert=True
    )

    if len(message.command) > 1:
        token = message.command[1]

        request = await get_files_col.find_one({"_id": token})

        if not request:
            await message.reply_text(
                "❌ This Get Files link is invalid. "
                "Please search again in the group."
            )
            return

        if request["user_id"] != message.from_user.id:
            await message.reply_text(
                "❌ This link belongs to another user. "
                "Please create your own link."
            )
            return

        query_filter = {
            "series_id": request["series_id"],
            "language": request["language"],
            "season": request["season"],
            "quality": request["quality"]
        }

        files = await episodes_col.find(
            query_filter
        ).sort("episode", 1).to_list(length=None)

        if not files:
            await message.reply_text(
                "❌ No files found for this selection."
            )
            return

        sent = 0

        for item in files:
            caption = (
                f"📺 {item['series_name']}\n"
                f"🌐 Language: {item['language']}\n"
                f"📂 Season: {item['season']:02d}\n"
                f"🎞 Quality: {item['quality']}\n"
                f"🎬 Episode: {item['episode']:02d}"
            )

            if item["file_type"] == "video":
                await client.send_video(
                    message.chat.id,
                    item["file_id"],
                    caption=caption
                )
            else:
                await client.send_document(
                    message.chat.id,
                    item["file_id"],
                    caption=caption
                )

            sent += 1
            await asyncio.sleep(0.5)
        return

    await send_welcome(client, message)

# ================= BOT STATISTICS =================

@app.on_message(
    filters.private
    & filters.user(ADMIN_IDS)
    & filters.command("stats")
)
async def bot_stats(client, message):

    total_users = await users_col.count_documents({})
    total_files = await episodes_col.count_documents({})
    total_series = await series_col.count_documents({})

    video_files = await episodes_col.count_documents({
        "file_type": "video"
    })

    document_files = await episodes_col.count_documents({
        "file_type": "document"
    })

    text = (
        "📊 **SERIES BOT STATISTICS**\n\n"
        f"👥 Total Users Started: `{total_users}`\n\n"
        f"📁 Total Files: `{total_files}`\n"
        f"📺 Total Series: `{total_series}`\n\n"
        f"🎬 Video Files: `{video_files}`\n"
        f"📄 Document Files: `{document_files}`"
    )

    await message.reply_text(text)


# ================= ADMIN POSTER SYSTEM =================

@app.on_message(
    filters.private
    & filters.user(ADMIN_IDS)
    & filters.command("addposter")
)
async def add_poster_command(client, message):

    await poster_sessions_col.update_one(
        {"user_id": message.from_user.id},
        {"$set": {"step": "waiting_series"}},
        upsert=True
    )

    await message.reply_text(
        "📸 Send the Series Name for its Poster.\n\n"
        "Example: Stranger Things"
    )


@app.on_message(
    filters.private
    & filters.user(ADMIN_IDS)
    & filters.text
    & ~filters.command([
        "addposter",
        "batch",
        "cancelbatch",
        "stats",
        "start"
    ])
)
async def poster_series_name(client, message):

    session = await poster_sessions_col.find_one(
        {"user_id": message.from_user.id}
    )

    if not session or session.get("step") != "waiting_series":
        return

    series_name = message.text.strip()

    series = await series_col.find_one({
        "series_key": normalize(series_name)
    })

    if not series:
        await message.reply_text(
            "❌ Series not found!\n\n"
            "Please upload at least one episode "
            "for this series first."
        )
        return

    await poster_sessions_col.update_one(
        {"user_id": message.from_user.id},
        {
            "$set": {
                "step": "waiting_photo",
                "series_id": series["_id"],
                "series_name": series["title"]
            }
        }
    )

    await message.reply_text(
        f"✅ Series: {series['title']}\n\n"
        "Now send the Poster Photo."
    )


@app.on_message(
    filters.private
    & filters.user(ADMIN_IDS)
    & filters.photo
)
async def save_series_poster(client, message):

    session = await poster_sessions_col.find_one(
        {"user_id": message.from_user.id}
    )

    if not session or session.get("step") != "waiting_photo":
        return

    photo_id = message.photo.file_id

    await series_col.update_one(
        {"_id": session["series_id"]},
        {
            "$set": {
                "poster_file_id": photo_id
            }
        }
    )

    await poster_sessions_col.delete_one(
        {"user_id": message.from_user.id}
    )

    await message.reply_text(
        f"✅ Poster saved successfully!\n\n"
        f"📺 Series: {session['series_name']}\n"
        "You can replace it anytime using /addposter."
    )


# ================= PRIVATE CHANNEL BATCH IMPORT =================

import re
import asyncio
from datetime import datetime, timezone
from pymongo import ReturnDocument

PRIVATE_LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?t\.me/c/(\d+)/(?:\d+/)?(\d+)(?:\?.*)?$",
    re.IGNORECASE
)


def parse_private_link(link):
    match = PRIVATE_LINK_RE.fullmatch(link.strip().rstrip("/"))

    if not match:
        raise ValueError("Invalid private channel message link")

    channel_id, message_id = match.groups()

    return int("-100" + channel_id), int(message_id)


async def import_channel_batch(client, message, first_link, last_link):

    try:
        first_chat, first_id = parse_private_link(first_link)
        last_chat, last_id = parse_private_link(last_link)

    except ValueError as e:
        await message.reply_text(f"❌ {e}")
        return

    if first_chat != last_chat:
        await message.reply_text(
            "❌ Both links must belong to the same channel."
        )
        return

    if first_id > last_id:
        await message.reply_text(
            "❌ First link must be before last link."
        )
        return

    total = last_id - first_id + 1

    if total > 20000:
        await message.reply_text(
            "❌ Maximum 20,000 message IDs per batch."
        )
        return

    status = await message.reply_text(
        f"⏳ Batch processing started!\n"
        f"Total messages: {total}"
    )

    saved = 0
    skipped = 0
    duplicates = 0
    checked = 0

    try:

        for start in range(first_id, last_id + 1, 100):

            ids = list(
                range(start, min(start + 100, last_id + 1))
            )

            messages = await client.get_messages(
                first_chat,
                message_ids=ids
            )

            if not isinstance(messages, list):
                messages = [messages]

            for msg in messages:

                checked += 1

                if not msg:
                    skipped += 1
                    continue

                media = msg.video or msg.document

                if not media:
                    skipped += 1
                    continue

                if msg.document:
                    mime = msg.document.mime_type or ""

                    if not mime.startswith("video/"):
                        skipped += 1
                        continue

                if not msg.caption:
                    skipped += 1
                    continue

                try:
                    data = parse_caption(msg.caption)
                except ValueError:
                    skipped += 1
                    continue

                series = await series_col.find_one_and_update(
                    {"series_key": data["series_key"]},
                    {
                        "$set": {
                            "title": data["series_name"],
                            "series_key": data["series_key"]
                        },
                        "$setOnInsert": {
                            "created_at": datetime.now(timezone.utc)
                        }
                    },
                    upsert=True,
                    return_document=ReturnDocument.AFTER
                )

                episode_filter = {
                    "series_key": data["series_key"],
                    "language": data["language"],
                    "season": data["season"],
                    "quality": data["quality"],
                    "episode": data["episode"]
                }

                result = await episodes_col.update_one(
                    episode_filter,
                    {
                        "$setOnInsert": {
                            "series_id": series["_id"],
                            "series_name": data["series_name"],
                            "series_key": data["series_key"],
                            "language": data["language"],
                            "season": data["season"],
                            "quality": data["quality"],
                            "episode": data["episode"],
                            "file_id": media.file_id,
                            "file_type": (
                                "video" if msg.video else "document"
                            ),
                            "source_chat_id": first_chat,
                            "source_message_id": msg.id,
                            "created_at": datetime.now(timezone.utc)
                        }
                    },
                    upsert=True
                )

                if result.upserted_id:
                    saved += 1
                else:
                    duplicates += 1

            await asyncio.sleep(0.15)

    except Exception as e:

        await status.edit_text(
            f"❌ Batch failed!\n"
            f"Error: {str(e)[:300]}\n"
            f"Saved: {saved}"
        )
        return

    await status.edit_text(
        "✅ BATCH IMPORT COMPLETED\n\n"
        f"🔎 Checked: {checked}\n"
        f"✅ Saved: {saved}\n"
        f"♻️ Duplicates: {duplicates}\n"
        f"⏭ Skipped: {skipped}"
    )


# ================= BATCH COMMAND =================

@app.on_message(
    filters.private
    & filters.user(ADMIN_IDS)
    & filters.command("batch")
)
async def batch_command(client, message):

    links = [
        x.strip()
        for x in (message.text or "").splitlines()[1:]
        if x.strip()
    ]

    if len(links) >= 2:
        await import_channel_batch(
            client, message, links[0], links[1]
        )
        return

    await batch_sessions_col.update_one(
        {"user_id": message.from_user.id},
        {"$set": {"step": "first_link"}},
        upsert=True
    )

    await message.reply_text(
        "🔗 Send FIRST message link."
    )


@app.on_message(
    filters.private
    & filters.user(ADMIN_IDS)
    & filters.command("cancelbatch")
)
async def cancel_batch(client, message):

    await batch_sessions_col.delete_one(
        {"user_id": message.from_user.id}
    )

    await message.reply_text("✅ Batch cancelled.")


@app.on_message(
    filters.private
    & filters.user(ADMIN_IDS)
    & filters.text
    & ~filters.command(["batch", "cancelbatch", "addposter"]),
    group=1
)
async def batch_link_input(client, message):

    session = await batch_sessions_col.find_one(
        {"user_id": message.from_user.id}
    )

    if not session:
        return

    link = message.text.strip()

    try:
        parse_private_link(link)
    except ValueError:
        await message.reply_text(
            "❌ Invalid message link. Try again."
        )
        return

    if session["step"] == "first_link":

        await batch_sessions_col.update_one(
            {"user_id": message.from_user.id},
            {
                "$set": {
                    "step": "last_link",
                    "first_link": link
                }
            }
        )

        await message.reply_text(
            "✅ First link received!\n"
            "Now send LAST message link."
        )
        return

    first_link = session["first_link"]

    await batch_sessions_col.delete_one(
        {"user_id": message.from_user.id}
    )

    await import_channel_batch(
        client, message, first_link, link
    )


# ================= SERIES SEARCH =================

@app.on_message(
    filters.text
    & filters.group
    & ~filters.command(["start"])
)
async def search_series(client, message):

    query = message.text.strip()

    await update_state(
        message.from_user.id,
        {"search_query": query}
    )

    if len(query) < 2:
        return

    results = await series_col.find({
        "title": {
            "$regex": re.escape(query),
            "$options": "i"
        }
    }).limit(10).to_list(length=10)

    if not results:
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
            "search_query": (
                query.message.reply_to_message.text
                if query.message.reply_to_message
                else series["title"]
            ),
            "language": None,
            "season": None,
            "quality": None
        }
    )

    info = await fetch_omdb_metadata(series["title"])

    genre = info.get("genre", "N/A")
    rating = info.get("rating", "N/A")
    year = info.get("year", "N/A")

    caption = (
        f"🎬 **Title:** `{series['title']}`\n"
        f"📅 **Released On:** `{year}`\n"
        f"🎭 **Genre:** `{genre}`\n"
        f"⭐ **Rating:** `{rating}`\n\n"
        f"🌐 **Available Languages:**"
    )

    keyboard = make_buttons(languages, "l")
    
    poster = (
        series.get("poster_file_id")
        or info.get("poster")
    )

    reply_id = (
        query.message.reply_to_message_id
        or query.message.id
    )

    if poster:
        await client.send_photo(
            chat_id=query.message.chat.id,
            photo=poster,
            caption=caption,
            reply_markup=keyboard,
            reply_to_message_id=reply_id
        )

    else:
        await client.send_message(
            chat_id=query.message.chat.id,
            text=caption,
            reply_markup=keyboard,
            reply_to_message_id=reply_id
        )

    await query.message.delete()
    await query.answer()
    
# ================= LANGUAGE CALLBACK =================

@app.on_callback_query(
    filters.regex(r"^l:\d+$")
)
async def select_language(client, query):

    state = await get_state(query.from_user.id)

    if not state:
        await query.answer(
            "Please search again.",
            show_alert=True
        )
        return

    languages = await get_options(
        state["series_id"],
        "language"
    )

    index = int(query.data.split(":")[1])

    if index >= len(languages):
        await query.answer("Invalid selection.")
        return

    language = languages[index]

    series = await series_col.find_one(
        {"_id": state["series_id"]}
    )

    if not series:
        await query.answer("Series not found.")
        return

    info = await fetch_omdb_metadata(series["title"])

    year = info.get("year", "N/A")
    genre = info.get("genre", "N/A")
    rating = info.get("rating", "N/A")

    seasons = await get_options(
        state["series_id"],
        "season",
        {"language": language}
    )

    await update_state(
        query.from_user.id,
        {"language": language}
    )

    caption = (
        f"○ **Title:** {series['title']}\n"
        f"○ **Released On:** {year}\n"
        f"○ **Genre:** {genre}\n"
        f"○ **Rating:** {rating}\n\n"
        f"<blockquote>▪️Language: {language}</blockquote>\n"
        f"**Available Seasons:**"
    )

    keyboard = make_buttons(seasons, "sn")

    keyboard.inline_keyboard.append([
        InlineKeyboardButton(
            "ʙᴀᴄᴋ",
            callback_data="back:language"
        )
    ])

    if query.message.photo:
        await query.message.edit_caption(
            caption,
            reply_markup=keyboard
        )
    else:
        await query.message.edit_text(
            caption,
            reply_markup=keyboard
        )

    await query.answer()



# ================= SEASON CALLBACK =================

@app.on_callback_query(
    filters.regex(r"^sn:\d+$")
)
async def select_season(client, query):

    state = await get_state(query.from_user.id)

    if not state:
        await query.answer(
            "Please search again.",
            show_alert=True
        )
        return

    seasons = await get_options(
        state["series_id"],
        "season",
        {"language": state["language"]}
    )

    index = int(query.data.split(":")[1])

    if index >= len(seasons):
        await query.answer(
            "Invalid selection.",
            show_alert=True
        )
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

    series = await series_col.find_one(
        {"_id": state["series_id"]}
    )

    if not series:
        await query.answer(
            "Series not found.",
            show_alert=True
        )
        return

    info = await fetch_omdb_metadata(
        series["title"]
    )

    year = info.get("year", "N/A")
    genre = info.get("genre", "N/A")
    rating = info.get("rating", "N/A")

    caption = (
        f"○ **Title:** {series['title']}\n"
        f"○ **Released On:** {year}\n"
        f"○ **Genre:** {genre}\n"
        f"○ **Rating:** {rating}\n"
        f"▪️Language: {state['language']}\n"
        f"▪️Season: Season {season}\n\n"
        "**Select the quality you need...!**"
    )

    keyboard = make_buttons(
        qualities,
        "q"
    )

    keyboard.inline_keyboard.append([
        InlineKeyboardButton(
            "ʙᴀᴄᴋ",
            callback_data="back:season"
        )
    ])

    if query.message.photo:
        await query.message.edit_caption(
            caption=caption,
            reply_markup=keyboard
        )
    else:
        await query.message.edit_text(
            caption,
            reply_markup=keyboard
        )

    await query.answer()


# ================= BACK NAVIGATION =================

@app.on_callback_query(
    filters.regex(r"^back:(series|language|season)$")
)
async def back_navigation(client, query):

    state = await get_state(query.from_user.id)

    if not state or not state.get("series_id"):
        await query.answer(
            "Please search again!",
            show_alert=True
        )
        return

    back_to = query.data.split(":")[1]
    series_id = state["series_id"]

    # BACK TO SERIES
    if back_to == "series":

        search_query = state.get("search_query")

        if not search_query:
            await query.answer(
                "Please search again!",
                show_alert=True
            )
            return

        results = await series_col.find({
            "title": {
                "$regex": re.escape(search_query),
                "$options": "i"
            }
        }).limit(10).to_list(length=10)

        if not results:
            await query.answer(
                "No series found!",
                show_alert=True
            )
            return

        buttons = []

        for item in results:
            buttons.append([
                InlineKeyboardButton(
                    item["title"],
                    callback_data=f"s:{item['_id']}"
                )
            ])

        await query.message.delete()

        await client.send_message(
            query.message.chat.id,
            "🔎 Select your series:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

        await update_state(
            query.from_user.id,
            {
                "series_id": None,
                "series_name": None,
                "language": None,
                "season": None,
                "quality": None
            }
        )

    # BACK TO LANGUAGE
    elif back_to == "language":

        languages = await get_options(
            series_id,
            "language"
        )

        keyboard = make_buttons(languages, "l")

        await update_state(
            query.from_user.id,
            {
                "language": None,
                "season": None,
                "quality": None
            }
        )

        caption = (
            f"🎬 **Title:** `{state['series_name']}`\n\n"
            "🌐 **Available Languages:**"
        )

        if query.message.photo:
            await query.message.edit_caption(
                caption=caption,
                reply_markup=keyboard
            )
        else:
            await query.message.edit_text(
                caption,
                reply_markup=keyboard
            )

    # BACK TO SEASON
    elif back_to == "season":

        seasons = await get_options(
            series_id,
            "season",
            {"language": state["language"]}
        )

        keyboard = make_buttons(seasons, "sn")

        keyboard.inline_keyboard.append([
            InlineKeyboardButton(
                "ʙᴀᴄᴋ",
                callback_data="back:language"
            )
        ])

        await update_state(
            query.from_user.id,
            {
                "season": None,
                "quality": None
            }
        )

        caption = (
            f"🎬 **Title:** `{state['series_name']}`\n"
            f"🌐 **Language:** `{state['language']}`\n\n"
            "**Available Seasons:**"
        )

        if query.message.photo:
            await query.message.edit_caption(
                caption=caption,
                reply_markup=keyboard
            )
        else:
            await query.message.edit_text(
                caption,
                reply_markup=keyboard
            )

    await query.answer("⬅️ Back!")

# ================= QUALITY CALLBACK =================

@app.on_callback_query(
    filters.regex(r"^q:\d+$")
)
async def select_quality(client, query):

    state = await get_state(query.from_user.id)

    if not state:
        await query.answer(
            "Please search again.",
            show_alert=True
        )
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
        await query.answer(
            "Invalid selection.",
            show_alert=True
        )
        return

    quality = qualities[index]

    await update_state(
        query.from_user.id,
        {"quality": quality}
    )

    token = secrets.token_urlsafe(10)

    await get_files_col.insert_one({
        "_id": token,
        "user_id": query.from_user.id,
        "series_id": state["series_id"],
        "series_name": state["series_name"],
        "language": state["language"],
        "season": state["season"],
        "quality": quality,
        "created_at": datetime.now(timezone.utc)
    })

    me = await client.get_me()

    deep_link = (
        f"https://t.me/{me.username}?start={token}"
    )

    text = (
        f"📺 {state['series_name']}\n"
        f"🌐 Language: {state['language']}\n"
        f"📂 Season: {state['season']:02d}\n"
        f"🎞 Quality: {quality}\n\n"
        "Press below to receive all files in private chat."
    )

    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "ɢᴇᴛ ғɪʟᴇs",
                url=deep_link
            )
        ]
    ])

    if query.message.photo:
        await query.message.edit_caption(
            caption=text,
            reply_markup=buttons
        )
    else:
        await query.message.edit_text(
            text,
            reply_markup=buttons
        )

    await query.answer()



# ================= RENDER WEB SERVER =================

async def health(request):
    return web.Response(
        text="Series Bot is running!"
    )


async def main():

    await app.start()

    web_app = web.Application()
    web_app.router.add_get("/", health)

    runner = web.AppRunner(web_app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

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
