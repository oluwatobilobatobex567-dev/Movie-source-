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
        "𝗮𝗯𝗰𝗱𝗲𝗳𝗴𝗵𝗶𝗷𝗸𝗹𝗺𝗻𝗼𝗽𝗾𝗿𝘀𝘁𝘂𝘃𝘄𝘅𝘆𝘇"
        "𝗔𝗕𝗖𝗗𝗘𝗙𝗚𝗛𝗜𝗝𝗞𝗟𝗠𝗡𝗢𝗣𝗤𝗥𝗦𝗧𝗨𝗩𝗪𝗫𝗬𝗭"
        "𝟬𝟭𝟮𝟯𝟰𝟵"
    )
    table = str.maketrans(normal, fancy_bold)
    return text.translate(table)


# ------------------ DATABASE HELPERS ------------------
def db_connect():
    return psycopg.connect(DATABASE_URL)


def db_init():
    logger.info("Initializing database (ensuring movies table exists)")
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS movies (
                        code TEXT NOT NULL,
                        file_id TEXT NOT NULL,
                        cover_id TEXT,
                        mode TEXT NOT NULL,
                        PRIMARY KEY (code, file_id)
                    );
                """)
            conn.commit()
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Database initialization failed: {str(e)}")
        raise e


def db_add_movie(code: str, file_id: str, cover_id: Optional[str], mode: str):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO movies (code, file_id, cover_id, mode)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (code, file_id) DO NOTHING;
            """, (code, file_id, cover_id, mode))
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


async def async_db_add_movie(code: str, file_id: str, cover_id: Optional[str], mode: str):
    await asyncio.to_thread(db_add_movie, code, file_id, cover_id, mode)


async def async_db_get_movies(code: str) -> List[Tuple[str, str]]:
    return await asyncio.to_thread(db_get_movies, code)


# ------------------ Pyrogram client ------------------
bot = Client("movie_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
_pending_adds: dict[int, dict] = {}


# UPGRADE 1: Allow /addmovie directly in source channel or private
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

    # Ask if it's a series or single movie
    buttons = [
        [InlineKeyboardButton(fancy("🎥 Single"), callback_data=f"set_mode_single_{code}")],
        [InlineKeyboardButton(fancy("📺 Series"), callback_data=f"set_mode_series_{code}")],
    ]
    await message.reply_text(fancy("🎬 Is this a single movie or a series?"), reply_markup=InlineKeyboardMarkup(buttons))
    _pending_adds[message.from_user.id] = {"code": code, "file_id": file_id, "timestamp": time.time()}


@bot.on_callback_query(filters.regex(r"^set_mode_"))
async def set_mode(client: Client, callback_query):
    mode = callback_query.data.split("_")[2]
    code = callback_query.data.split("_")[3]

    _pending_adds[callback_query.from_user.id]["mode"] = mode
    await callback_query.answer(f"Mode set to {mode}")

    if mode == "series":
        await callback_query.message.reply_text(fancy("📺 Series mode selected. Send the movie cover image now."))
    else:
        await callback_query.message.reply_text(fancy("🎥 Single movie mode selected. Send the movie cover image now."))


@bot.on_message(filters.photo & filters.user(ADMIN_ID))
async def receive_cover(client: Client, message):
    admin_id = message.from_user.id
    if admin_id not in _pending_adds:
        return

    code = _pending_adds[admin_id]["code"]
    file_id = _pending_adds[admin_id]["file_id"]
    cover_id = message.photo.file_id
    mode = _pending_adds[admin_id]["mode"]

    try:
        await async_db_add_movie(code, file_id, cover_id if mode == "single" else None, mode)
        del _pending_adds[admin_id]
        me = await bot.get_me()
        link = f"https://t.me/{me.username}?start={quote(code)}"
        await message.reply_text(f"{fancy('✅ Movie saved!')}\n{fancy('🎯 Share link:')}\n{link}", quote=True)

        if mode == "series":
            await message.reply_text(fancy("📺 Series cover set. Add more episodes or movies!"))
    except Exception as e:
        logger.exception("Failed to save movie %s: %s", code, e)
        await message.reply_text(fancy("❌ Failed to save movie, check logs."), quote=True)


# ------------------ Add a delay for time sync ------------------
import time
time.sleep(20)  # Wait for 20 seconds to synchronize time

# ------------------ Start the bot ------------------
if __name__ == "__main__":
    start_keepalive_thread(port=PORT)
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(async_db_init())
    except Exception:
        logger.exception("Database initialization failed at startup.")
    bot.run()
