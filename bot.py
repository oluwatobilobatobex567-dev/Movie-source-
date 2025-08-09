#!/usr/bin/env python3
import os
import threading
import asyncio
import time
import logging
from typing import Optional, Tuple, Any, List
from urllib.parse import quote, unquote

import psycopg
from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# --------- Logging ----------
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger("movie_bot")

# ------------------ KEEP-ALIVE WEB SERVER ------------------
flask_app = Flask("keepalive")


@flask_app.route("/")
def index():
    return "Movie Bot is alive!"


def run_keepalive(port: int = 8080):
    logger.info("Starting keep-alive server on port %s", port)
    flask_app.run(host="0.0.0.0", port=port, threaded=True)


def start_keepalive_thread(port: int = 8080):
    t = threading.Thread(target=run_keepalive, args=(port,), daemon=True)
    t.start()
    time.sleep(0.3)


# ------------------ ENVIRONMENT ------------------
def require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        logger.error("Missing required environment variable: %s", name)
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


API_ID = int(require_env("API_ID"))
API_HASH = require_env("API_HASH")
BOT_TOKEN = require_env("BOT_TOKEN")
ADMIN_ID = int(require_env("ADMIN_ID"))
DATABASE_URL = require_env("DATABASE_URL")
SOURCE_CHANNEL = require_env("SOURCE_CHANNEL")  # private/public source channel ID or username

DEFAULT_CHANNELS = ["@ModMasterUnlocked", "@AnimeTheaterLeaks", "@hollywoodleaks711"]

if os.environ.get("CHANNELS"):
    CHANNELS = [c.strip() for c in os.environ.get("CHANNELS").split(",") if c.strip()]
else:
    CHANNELS = DEFAULT_CHANNELS

DELETE_AFTER_SECONDS = int(os.environ.get("DELETE_AFTER_SECONDS", 20 * 60))
PORT = int(os.environ.get("PORT", 8080))

# ------------------ FANCY FONT ------------------
def fancy(text: str) -> str:
    normal = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    fancy_bold = (
        "𝗮𝗯𝗰𝗱𝗲𝗳𝗴𝗵𝗶𝗷𝗸𝗹𝗺𝗻𝗼𝗽𝗾𝗿𝘀𝘁𝘶𝘃𝘄𝘅𝘆𝘇"
        "𝗔𝗕𝗖𝗗𝗘𝗙𝗚𝗛𝗜𝗝𝗞𝗟𝗠𝗡𝗢𝗣𝗤𝗥𝗦𝗧𝗨𝗩𝗪𝗫𝗬𝗭"
        "𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟕𝟴𝟵"
    )
    table = str.maketrans(normal, fancy_bold)
    return text.translate(table)


# ------------------ DATABASE HELPERS ------------------
def db_connect():
    return psycopg.connect(DATABASE_URL)


def db_init():
    logger.info("Initializing database (ensuring movies table exists)")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS movies (
                    code TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    cover_id TEXT NOT NULL,
                    PRIMARY KEY (code, file_id)
                );
            """)
        conn.commit()


def db_add_movie(code: str, file_id: str, cover_id: str):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO movies (code, file_id, cover_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (code, file_id) DO NOTHING;
            """, (code, file_id, cover_id))
        conn.commit()


def db_get_movies(code: str) -> List[Tuple[str, str]]:
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT file_id, cover_id
                FROM movies
                WHERE code = %s
                ORDER BY file_id ASC;
            """, (code,))
            return cur.fetchall()


# Async wrappers
async def async_db_init():
    await asyncio.to_thread(db_init)


async def async_db_add_movie(code: str, file_id: str, cover_id: str):
    await asyncio.to_thread(db_add_movie, code, file_id, cover_id)


async def async_db_get_movies(code: str) -> List[Tuple[str, str]]:
    return await asyncio.to_thread(db_get_movies, code)


# ------------------ Pyrogram client ------------------
bot = Client("movie_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
_pending_adds: dict[int, dict] = {}


# UPGRADE 1: Allow /addmovie directly in source channel
@bot.on_message(filters.command("addmovie") & (filters.user(ADMIN_ID) | filters.chat(SOURCE_CHANNEL)))
async def cmd_addmovie(client: Client, message):
    if not message.reply_to_message or not (
        message.reply_to_message.video or message.reply_to_message.document
    ):
        await message.reply_text(fancy("🎬 Reply to a movie file with /addmovie movie_code"), quote=True)
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply_text(fancy("❌ You must provide a movie code. Example: /addmovie demon_slayer"), quote=True)
        return

    code = parts[1].strip()
    if not code or " " in code:
        await message.reply_text(fancy("❌ Invalid code. Use a single token."), quote=True)
        return

    file_id = message.reply_to_message.video.file_id if message.reply_to_message.video else message.reply_to_message.document.file_id

    _pending_adds[message.from_user.id] = {"code": code, "file_id": file_id, "timestamp": time.time()}
    await message.reply_text(fancy("🖼 Now send the movie cover image (reply with the poster)."), quote=True)


@bot.on_message(filters.photo & filters.user(ADMIN_ID))
async def receive_cover(client: Client, message):
    admin_id = message.from_user.id
    if admin_id not in _pending_adds:
        return

    code = _pending_adds[admin_id]["code"]
    file_id = _pending_adds[admin_id]["file_id"]
    cover_id = message.photo.file_id

    try:
        await async_db_add_movie(code, file_id, cover_id)
        del _pending_adds[admin_id]
        me = await bot.get_me()
        link = f"https://t.me/{me.username}?start={quote(code)}"
        await message.reply_text(f"{fancy('✅ Movie saved!')}\n{fancy('🎯 Share link:')}\n{link}", quote=True)
    except Exception as e:
        logger.exception("Failed to save movie %s: %s", code, e)
        await message.reply_text(fancy("❌ Failed to save movie, check logs."), quote=True)


async def user_in_all_channels(user_id: int) -> bool:
    for ch in CHANNELS:
        try:
            member = await bot.get_chat_member(ch, user_id)
            if member.status not in ("member", "administrator", "creator"):
                return False
        except Exception as e:
            logger.warning("Error checking membership for user %s in channel %s: %s", user_id, ch, e)
            return False
    return True


@bot.on_message(filters.command("start"))
async def cmd_start(client: Client, message):
    if len(message.command) == 1:
        await message.reply_text(fancy("🍿 Welcome! Click a Watch link in the channel to get a movie."))
        return

    code = unquote(message.command[1].strip())
    try:
        rows = await async_db_get_movies(code)
    except Exception as e:
        logger.exception("DB error fetching movie %s: %s", code, e)
        await message.reply_text(fancy("❌ Internal error fetching movie."))
        return

    if not rows:
        await message.reply_text(fancy("❌ Movie not found."))
        return

    if not await user_in_all_channels(message.from_user.id):
        buttons: List[List[InlineKeyboardButton]] = []
        for ch in CHANNELS:
            label = fancy("📢 Join ") + fancy(ch.strip().lstrip("@"))
            buttons.append([InlineKeyboardButton(label, url=f"https://t.me/{ch.strip().lstrip('@')}")])
        me = await bot.get_me()
        deep_link = f"https://t.me/{me.username}?start={quote(code)}"
        buttons.append([InlineKeyboardButton(fancy("✅ I Joined - Get Movie"), url=deep_link)])
        await message.reply_photo(
            rows[0][1],
            caption=fancy("🔒 Join all channels to unlock this movie."),
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # Send all episodes/files in order
    for file_id, cover_id in rows:
        sent_cover = await message.reply_photo(cover_id, caption=fancy("🎬 Your movie is ready!"))
        sent_video = await message.reply_video(file_id, caption=fancy("⏳ This file will be deleted in a while."))

        async def delete_later(chat_id: int, message_id: int, delay: int):
            await asyncio.sleep(delay)
            try:
                await bot.delete_messages(chat_id, message_id)
            except Exception:
                pass

        asyncio.create_task(delete_later(sent_cover.chat.id, sent_cover.message_id, DELETE_AFTER_SECONDS))
        asyncio.create_task(delete_later(sent_video.chat.id, sent_video.message_id, DELETE_AFTER_SECONDS))


if __name__ == "__main__":
    start_keepalive_thread(port=PORT)
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(async_db_init())
    except Exception:
        logger.exception("Database initialization failed at startup.")
    bot.run()
