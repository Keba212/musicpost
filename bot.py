from __future__ import annotations

import asyncio
import html
import logging
import os
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp
import asyncpg
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message


LOGGER = logging.getLogger("ukrainian_music_bot")
PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"
RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class Settings:
    bot_token: str
    target_chat_id: str
    target_channel_url: str
    pexels_api_key: str
    database_url: str
    admin_user_id: int | None
    dry_run: bool
    interval_minutes: int
    image_style: str
    timeout_seconds: int
    run_once: bool

    @classmethod
    def from_env(cls) -> "Settings":
        required = ("BOT_TOKEN", "TARGET_CHAT_ID", "TARGET_CHANNEL_URL", "PEXELS_API_KEY", "DATABASE_URL")
        missing = [name for name in required if not os.getenv(name)]
        if missing and os.getenv("DRY_RUN", "true").lower() != "true":
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
        return cls(
            bot_token=os.getenv("BOT_TOKEN", "dry-run-token"),
            target_chat_id=os.getenv("TARGET_CHAT_ID", "dry-run-chat"),
            target_channel_url=os.getenv("TARGET_CHANNEL_URL", "https://t.me/"),
            pexels_api_key=os.getenv("PEXELS_API_KEY", "dry-run-key"),
            database_url=os.getenv("DATABASE_URL", "postgresql://localhost/ukrainian_music"),
            admin_user_id=int(os.environ["ADMIN_USER_ID"]) if os.getenv("ADMIN_USER_ID") else None,
            dry_run=os.getenv("DRY_RUN", "true").lower() == "true",
            interval_minutes=int(os.getenv("POST_INTERVAL_MINUTES", "360")),
            image_style=os.getenv(
                "IMAGE_STYLE",
                "dark cinematic Ukrainian music aesthetic, night city, neon, film grain, blue and yellow accents",
            ),
            timeout_seconds=int(os.getenv("HTTP_TIMEOUT_SECONDS", "45")),
            run_once=os.getenv("RUN_ONCE", "false").lower() == "true",
        )


@dataclass(frozen=True)
class Track:
    queue_id: int
    audio_file_id: str
    artist: str
    name: str


@dataclass(frozen=True)
class ImageResult:
    download_url: str
    photographer: str


async def with_retries(
    operation: Callable[[], Awaitable[Any]],
    label: str,
    attempts: int = 4,
) -> Any:
    for attempt in range(attempts):
        try:
            return await operation()
        except Exception as error:
            if attempt == attempts - 1:
                raise
            delay = min(30, 2**attempt) + random.uniform(0, 0.25)
            LOGGER.warning("%s failed (%s); retrying in %.1fs", label, type(error).__name__, delay)
            await asyncio.sleep(delay)


async def request_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    async with session.get(url, params=params, headers=headers) as response:
        if response.status in RETRYABLE_STATUS_CODES:
            raise RuntimeError(f"HTTP {response.status}")
        response.raise_for_status()
        return await response.json()


async def ingest_updates(bot: Bot, pool: asyncpg.Pool, settings: Settings) -> None:
    last_update_id = await pool.fetchval("SELECT value FROM bot_state WHERE key = 'last_update_id'")
    updates = await with_retries(
        lambda: bot.get_updates(
            offset=(last_update_id or 0) + 1,
            limit=100,
            timeout=0,
            allowed_updates=["message", "callback_query"],
        ),
        "Telegram updates",
    )
    for update in updates:
        if update.callback_query and is_allowed_callback(update.callback_query, settings):
            await handle_callback(update.callback_query, bot, pool)
        message = update.message
        if message and message.chat.type == "private" and is_allowed_sender(message, settings):
            if message.audio:
                await pool.execute(
                    """
                    INSERT INTO queued_tracks
                        (source_chat_id, source_message_id, audio_file_id, artist, name)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (source_chat_id, source_message_id) DO NOTHING
                    """,
                    message.chat.id,
                    message.message_id,
                    message.audio.file_id,
                    message.audio.performer or "Unknown artist",
                    message.audio.title or "Untitled",
                )
                LOGGER.info("Queued: %s - %s", message.audio.performer or "Unknown artist", message.audio.title or "Untitled")
                await bot.send_message(
                    message.chat.id,
                    "✅ Трек додано в чергу",
                    reply_markup=admin_keyboard(),
                )
            elif message.text in {"/start", "/menu", "/queue", "/publish_now"}:
                await bot.send_message(message.chat.id, "🎧 MØOD | UA", reply_markup=admin_keyboard())
        await pool.execute(
            """
            INSERT INTO bot_state (key, value) VALUES ('last_update_id', $1)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            update.update_id,
        )


def is_allowed_sender(message: Message, settings: Settings) -> bool:
    return settings.admin_user_id is None or (
        message.from_user is not None and message.from_user.id == settings.admin_user_id
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Черга", callback_data="queue")],
            [InlineKeyboardButton(text="🚀 Опублікувати зараз", callback_data="publish_now")],
        ]
    )


def is_allowed_callback(callback: CallbackQuery, settings: Settings) -> bool:
    return settings.admin_user_id is None or (
        callback.from_user is not None and callback.from_user.id == settings.admin_user_id
    )


async def handle_callback(callback: CallbackQuery, bot: Bot, pool: asyncpg.Pool) -> None:
    if callback.data == "queue":
        count = await pool.fetchval("SELECT COUNT(*) FROM queued_tracks WHERE status = 'queued'")
        await callback.answer(f"У черзі: {count}", show_alert=True)
    elif callback.data == "publish_now":
        await pool.execute(
            """
            INSERT INTO bot_state (key, value) VALUES ('publish_now', 1)
            ON CONFLICT (key) DO UPDATE SET value = 1
            """
        )
        await callback.answer("Опублікуємо під час найближчого запуску", show_alert=True)


async def find_image(session: aiohttp.ClientSession, settings: Settings) -> ImageResult:
    LOGGER.info("Searching image...")
    payload = await with_retries(
        lambda: request_json(
            session,
            PEXELS_SEARCH_URL,
            params={"query": "night city neon music Ukraine", "orientation": "landscape", "per_page": 15},
            headers={"Authorization": settings.pexels_api_key},
        ),
        "Pexels search",
    )
    photos = payload.get("photos", [])
    if not photos:
        raise RuntimeError("Pexels returned no photos")
    photo = random.choice(photos)
    return ImageResult(
        download_url=photo["src"]["large"],
        photographer=photo["photographer"],
    )


def make_caption(track: Track, image: ImageResult, settings: Settings) -> str:
    artist = html.escape(track.artist)
    name = html.escape(track.name)
    photographer = html.escape(image.photographer)
    channel_url = html.escape(settings.target_channel_url, quote=True)
    return (
        f"🇺🇦 <b>{artist} - {name}</b>\n"
        f"📷 Фото: {photographer} / Pexels\n\n"
        f"<a href=\"{channel_url}\">🎧MØOD | UA🇺🇦</a>"
    )


async def init_database(pool: asyncpg.Pool) -> None:
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS queued_tracks (
            id BIGSERIAL PRIMARY KEY,
            source_chat_id BIGINT NOT NULL,
            source_message_id BIGINT NOT NULL,
            audio_file_id TEXT NOT NULL,
            artist TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            telegram_message_id BIGINT,
            queued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            published_at TIMESTAMPTZ,
            UNIQUE (source_chat_id, source_message_id)
        )
        """
    )
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value BIGINT NOT NULL
        )
        """
    )


async def publish_once(
    bot: Bot,
    session: aiohttp.ClientSession,
    pool: asyncpg.Pool,
    settings: Settings,
) -> bool:
    row = await pool.fetchrow(
        """
        SELECT id, audio_file_id, artist, name
        FROM queued_tracks
        WHERE status = 'queued'
        ORDER BY queued_at, id
        LIMIT 1
        """
    )
    if not row:
        LOGGER.info("Queue is empty; nothing to publish")
        return False
    track = Track(
        queue_id=row["id"],
        audio_file_id=row["audio_file_id"],
        artist=row["artist"],
        name=row["name"],
    )
    try:
        await pool.execute("UPDATE queued_tracks SET status = 'publishing' WHERE id = $1", track.queue_id)
        image = await find_image(session, settings)
        caption = make_caption(track, image, settings)
        if settings.dry_run:
            LOGGER.info("DRY_RUN=true; skipping publishing")
            await pool.execute("UPDATE queued_tracks SET status = 'queued' WHERE id = $1", track.queue_id)
            return True
        LOGGER.info("Publishing...")
        await with_retries(
            lambda: bot.send_photo(settings.target_chat_id, image.download_url, caption=caption),
            "Telegram photo publish",
        )
        audio_message = await with_retries(
            lambda: bot.send_audio(
                settings.target_chat_id,
                track.audio_file_id,
                title=track.name,
                performer=track.artist,
                caption=caption,
            ),
            "Telegram audio publish",
        )
        await pool.execute(
            "UPDATE queued_tracks SET status = 'published', telegram_message_id = $1, published_at = NOW() WHERE id = $2",
            audio_message.message_id,
            track.queue_id,
        )
        LOGGER.info("Published successfully")
        return True
    except Exception:
        await pool.execute("UPDATE queued_tracks SET status = 'queued' WHERE id = $1", track.queue_id)
        LOGGER.exception("Queued track failed; it will be retried next hour")
        return False


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    settings = Settings.from_env()
    if settings.dry_run:
        LOGGER.info("DRY_RUN=true")
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=3)
    await init_database(pool)
    timeout = aiohttp.ClientTimeout(total=settings.timeout_seconds)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session, Bot(
            settings.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        ) as bot:
            if settings.run_once:
                await ingest_updates(bot, pool, settings)
                await publish_once(bot, session, pool, settings)
                return
            while True:
                try:
                    await ingest_updates(bot, pool, settings)
                    await publish_once(bot, session, pool, settings)
                except Exception:
                    LOGGER.exception("Post cycle failed; the bot will continue")
                LOGGER.info("Next post in %d minutes", settings.interval_minutes)
                await asyncio.sleep(settings.interval_minutes * 60)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
