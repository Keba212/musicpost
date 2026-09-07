from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl

import aiohttp
import asyncpg
from aiohttp import web
from dotenv import load_dotenv
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo


LOGGER = logging.getLogger("ukrainian_music_bot")
PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"
RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}

load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    target_chat_id: str
    pexels_api_key: str
    database_url: str
    admin_user_id: int | None
    dry_run: bool
    interval_minutes: int
    image_style: str
    timeout_seconds: int
    run_once: bool
    channel_username: str
    channel_link: str
    webapp_url: str
    webapp_host: str
    webapp_port: int
    web_only: bool

    @classmethod
    def from_env(cls) -> "Settings":
        required = ("BOT_TOKEN", "TARGET_CHAT_ID", "PEXELS_API_KEY", "DATABASE_URL")
        missing = [name for name in required if not os.getenv(name)]
        if missing and os.getenv("DRY_RUN", "true").lower() != "true":
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
        return cls(
            bot_token=os.getenv("BOT_TOKEN", "dry-run-token"),
            target_chat_id=os.getenv("TARGET_CHAT_ID", "dry-run-chat"),
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
            channel_username=os.getenv("CHANNEL_USERNAME", "").strip(),
            channel_link=os.getenv("CHANNEL_LINK", "").strip(),
            webapp_url=os.getenv("WEBAPP_URL", "").strip(),
            webapp_host=os.getenv("WEBAPP_HOST", "0.0.0.0"),
            webapp_port=int(os.getenv("WEBAPP_PORT", "8080")),
            web_only=os.getenv("WEB_ONLY", "false").lower() == "true",
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


async def answer_callback_safely(bot: Bot, callback_id: str, text: str | None = None) -> None:
    try:
        await bot.answer_callback_query(callback_id, text=text)
    except TelegramBadRequest as error:
        if "query is too old" in str(error).lower() or "query id is invalid" in str(error).lower():
            LOGGER.warning("Skipping expired callback query %s", callback_id)
            return
        raise


async def ingest_updates(bot: Bot, session: aiohttp.ClientSession, pool: asyncpg.Pool, settings: Settings) -> None:
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
        if update.callback_query:
            callback = update.callback_query
            if callback.message and callback.message.chat.type == "private" and is_allowed_user(callback.from_user, settings):
                data = callback.data or ""
                if data == "menu:home":
                    rows = await get_queue_rows(pool)
                    await callback.message.edit_text(build_menu_text(rows), reply_markup=build_menu_keyboard(settings))
                    await answer_callback_safely(bot, callback.id)
                elif data == "menu:queue":
                    rows = await get_queue_rows(pool)
                    await callback.message.edit_text(build_queue_message(rows), reply_markup=build_queue_keyboard(rows))
                    await answer_callback_safely(bot, callback.id)
                elif data == "menu:publish":
                    published = await publish_next_track(bot, session, pool, settings)
                    rows = await get_queue_rows(pool)
                    await callback.message.edit_text(
                        ("✅ Трек опубліковано.\n\n" if published else "ℹ️ Черга порожня або публікація не вдалася.\n\n")
                        + build_menu_text(rows),
                        reply_markup=build_menu_keyboard(settings),
                    )
                    await answer_callback_safely(bot, callback.id, text="Опубліковано" if published else "Немає треку")
                elif data == "menu:clear_confirm":
                    await callback.message.edit_text(
                        "⚠️ Точно очистити всі треки зі статусом «у черзі»?",
                        reply_markup=build_clear_confirmation_keyboard(),
                    )
                    await answer_callback_safely(bot, callback.id)
                elif data == "menu:clear":
                    await pool.execute("DELETE FROM queued_tracks WHERE status = 'queued'")
                    await callback.message.edit_text(
                        "✅ Чергу очищено.\n\n" + build_menu_text([]),
                        reply_markup=build_menu_keyboard(settings),
                    )
                    await answer_callback_safely(bot, callback.id, text="Чергу очищено")
                elif data == "menu:help":
                    await callback.message.edit_text(
                        "Надішли боту аудіо, щоб додати його в чергу.\n\n"
                        "Публікація відбувається автоматично раз на годину або вручну через меню.",
                        reply_markup=build_menu_keyboard(settings),
                    )
                    await answer_callback_safely(bot, callback.id)
                elif data.startswith("publish_now:"):
                    queue_id = int(data.split(":", 1)[1])
                    published = await publish_specific_track(bot, session, pool, settings, queue_id)
                    rows = await get_queue_rows(pool)
                    await callback.message.edit_text(
                        ("✅ Трек опубліковано.\n\n" if published else "❌ Не вдалося опублікувати трек.\n\n")
                        + build_queue_message(rows),
                        reply_markup=build_queue_keyboard(rows),
                    )
                    await answer_callback_safely(bot, callback.id, text="Опубліковано" if published else "Помилка")

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
            elif message.text:
                command = message.text.strip().lower()
                if command in {"/start", "/help"}:
                    rows = await get_queue_rows(pool)
                    await bot.send_message(
                        message.chat.id,
                        build_menu_text(rows),
                        reply_markup=build_menu_keyboard(settings),
                    )
                elif command == "/menu":
                    rows = await get_queue_rows(pool)
                    await bot.send_message(
                        message.chat.id,
                        build_menu_text(rows),
                        reply_markup=build_menu_keyboard(settings),
                    )
                elif command == "/queue":
                    rows = await get_queue_rows(pool)
                    await bot.send_message(
                        message.chat.id,
                        build_queue_message(rows),
                        reply_markup=build_queue_keyboard(rows),
                    )
                elif command == "/publish_now":
                    published = await publish_next_track(bot, session, pool, settings)
                    await bot.send_message(
                        message.chat.id,
                        "Опубліковано зараз" if published else "Нічого не було в черзі або публікація не пройшла.",
                    )
                elif command == "/clear_queue":
                    deleted = await pool.execute("DELETE FROM queued_tracks WHERE status = 'queued'")
                    await bot.send_message(message.chat.id, f"Чергу очищено. {deleted}")
        await pool.execute(
            """
            INSERT INTO bot_state (key, value) VALUES ('last_update_id', $1)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            update.update_id,
        )


async def update_loop(
    bot: Bot,
    session: aiohttp.ClientSession,
    pool: asyncpg.Pool,
    settings: Settings,
) -> None:
    while True:
        try:
            await ingest_updates(bot, session, pool, settings)
        except Exception:
            LOGGER.exception("Telegram update cycle failed; retrying soon")
        await asyncio.sleep(2)


async def publisher_loop(
    bot: Bot,
    session: aiohttp.ClientSession,
    pool: asyncpg.Pool,
    settings: Settings,
) -> None:
    while True:
        try:
            await publish_once(bot, session, pool, settings)
        except Exception:
            LOGGER.exception("Post cycle failed; the bot will continue")
        LOGGER.info("Next scheduled post in %d minutes", settings.interval_minutes)
        await asyncio.sleep(settings.interval_minutes * 60)


def is_allowed_sender(message: Message, settings: Settings) -> bool:
    return is_allowed_user(message.from_user, settings)


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


def make_footer(settings: Settings) -> str:
    channel_link = settings.channel_link
    if channel_link:
        footer = f"<a href=\"{channel_link}\">🎧MØOD | UA🇺🇦</a>"
    elif settings.channel_username:
        username = settings.channel_username.lstrip("@")
        footer = f"<a href=\"https://t.me/{username}\">🎧MØOD | UA🇺🇦</a>"
    else:
        footer = "🎧MØOD | UA🇺🇦"
    return footer


def build_queue_message(rows: list[asyncpg.Record]) -> str:
    if not rows:
        return "📋 Черга порожня. Нові треки, що надіслали в бот, з'являться тут."
    lines = ["📋 Черга на публікацію:"]
    for index, row in enumerate(rows, start=1):
        queued_at = row["queued_at"].strftime("%H:%M") if row["queued_at"] else "—"
        lines.append(f"{index}. {row['artist']} — {row['name']} · {queued_at}")
    return "\n".join(lines)


def build_queue_keyboard(rows: list[asyncpg.Record]) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for row in rows:
        label = f"Опублікувати #{row['id']}"
        keyboard.inline_keyboard.append(
            [InlineKeyboardButton(text=label, callback_data=f"publish_now:{row['id']}")]
        )
    keyboard.inline_keyboard.append(
        [InlineKeyboardButton(text="← Меню", callback_data="menu:home")]
    )
    return keyboard


def build_menu_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📋 Черга треків", callback_data="menu:queue")],
        [InlineKeyboardButton(text="▶️ Опублікувати наступний", callback_data="menu:publish")],
        [InlineKeyboardButton(text="🔄 Оновити", callback_data="menu:home")],
        [InlineKeyboardButton(text="🗑 Очистити чергу", callback_data="menu:clear_confirm")],
    ]
    if settings.webapp_url:
        buttons.insert(0, [InlineKeyboardButton(text="🚀 Відкрити програму", web_app=WebAppInfo(url=settings.webapp_url))])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_clear_confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Так, очистити", callback_data="menu:clear")],
            [InlineKeyboardButton(text="← Скасувати", callback_data="menu:home")],
        ]
    )


async def get_queue_rows(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    return await pool.fetch(
        """
        SELECT id, artist, name, queued_at
        FROM queued_tracks
        WHERE status = 'queued'
        ORDER BY queued_at, id
        LIMIT 20
        """
    )


def build_menu_text(rows: list[asyncpg.Record]) -> str:
    return f"🎧 <b>MØOD | UA</b>\n\nУ черзі: <b>{len(rows)}</b> треків\n\nОбери дію:"


def is_valid_webapp_request(request: web.Request, settings: Settings) -> bool:
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if not init_data:
        return False
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    if not received_hash:
        return False
    check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", settings.bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        return False
    try:
        user = json.loads(values.get("user", "{}"))
        auth_date = int(values.get("auth_date", "0"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return (
        settings.admin_user_id is not None
        and user.get("id") == settings.admin_user_id
        and 0 <= time.time() - auth_date < 86400
    )


def webapp_json_rows(rows: list[asyncpg.Record]) -> list[dict[str, Any]]:
    return [
        {
            "id": row["id"],
            "artist": row["artist"],
            "name": row["name"],
            "queued_at": row["queued_at"].isoformat() if row["queued_at"] else None,
        }
        for row in rows
    ]


def create_webapp_server(
    bot: Bot,
    session: aiohttp.ClientSession,
    pool: asyncpg.Pool,
    settings: Settings,
) -> web.Application:
    app = web.Application()
    webapp_dir = Path(__file__).parent / "webapp"

    async def authorize(request: web.Request) -> web.Response | None:
        if not is_valid_webapp_request(request, settings):
            return web.json_response({"error": "Unauthorized"}, status=401)
        return None

    async def queue_handler(request: web.Request) -> web.Response:
        unauthorized = await authorize(request)
        if unauthorized:
            return unauthorized
        rows = await get_queue_rows(pool)
        return web.json_response({"items": webapp_json_rows(rows)})

    async def publish_handler(request: web.Request) -> web.Response:
        unauthorized = await authorize(request)
        if unauthorized:
            return unauthorized
        payload = await request.json() if request.can_read_body else {}
        queue_id = payload.get("id")
        published = (
            await publish_specific_track(bot, session, pool, settings, int(queue_id))
            if queue_id is not None
            else await publish_next_track(bot, session, pool, settings)
        )
        return web.json_response({"published": published})

    async def clear_handler(request: web.Request) -> web.Response:
        unauthorized = await authorize(request)
        if unauthorized:
            return unauthorized
        await pool.execute("DELETE FROM queued_tracks WHERE status = 'queued'")
        return web.json_response({"cleared": True})

    async def index_handler(_: web.Request) -> web.StreamResponse:
        return web.FileResponse(webapp_dir / "index.html")

    async def app_js_handler(_: web.Request) -> web.StreamResponse:
        return web.FileResponse(webapp_dir / "app.js")

    async def styles_handler(_: web.Request) -> web.StreamResponse:
        return web.FileResponse(webapp_dir / "styles.css")

    app.router.add_get("/", index_handler)
    app.router.add_get("/app.js", app_js_handler)
    app.router.add_get("/styles.css", styles_handler)
    app.router.add_get("/api/queue", queue_handler)
    app.router.add_post("/api/publish", publish_handler)
    app.router.add_post("/api/clear", clear_handler)
    return app


def is_allowed_user(user: Any, settings: Settings) -> bool:
    return settings.admin_user_id is None or (user is not None and user.id == settings.admin_user_id)


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


async def publish_specific_track(
    bot: Bot,
    session: aiohttp.ClientSession,
    pool: asyncpg.Pool,
    settings: Settings,
    queue_id: int,
) -> bool:
    row = await pool.fetchrow(
        """
        SELECT id, audio_file_id, artist, name
        FROM queued_tracks
        WHERE id = $1 AND status = 'queued'
        LIMIT 1
        """,
        queue_id,
    )
    if not row:
        LOGGER.info("Track %s is not queued or not found", queue_id)
        return False
    return await _publish_track(bot, session, pool, settings, row)


async def publish_next_track(
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
    return await _publish_track(bot, session, pool, settings, row)


async def publish_once(
    bot: Bot,
    session: aiohttp.ClientSession,
    pool: asyncpg.Pool,
    settings: Settings,
) -> bool:
    return await publish_next_track(bot, session, pool, settings)


async def _publish_track(
    bot: Bot,
    session: aiohttp.ClientSession,
    pool: asyncpg.Pool,
    settings: Settings,
    row: asyncpg.Record,
) -> bool:
    track = Track(
        queue_id=row["id"],
        audio_file_id=row["audio_file_id"],
        artist=row["artist"],
        name=row["name"],
    )
    try:
        await pool.execute("UPDATE queued_tracks SET status = 'publishing' WHERE id = $1", track.queue_id)
        image = await find_image(session, settings)
        caption = f"\n{make_footer(settings)}"
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
            web_runner: web.AppRunner | None = None
            if settings.webapp_url or settings.web_only:
                web_runner = web.AppRunner(create_webapp_server(bot, session, pool, settings))
                await web_runner.setup()
                await web.TCPSite(web_runner, settings.webapp_host, settings.webapp_port).start()
                LOGGER.info("Mini App API listening on %s:%d", settings.webapp_host, settings.webapp_port)
            if settings.web_only:
                await asyncio.Event().wait()
                return
            if settings.run_once:
                await ingest_updates(bot, session, pool, settings)
                await publish_once(bot, session, pool, settings)
                if web_runner:
                    await web_runner.cleanup()
                return
            try:
                await asyncio.gather(
                    update_loop(bot, session, pool, settings),
                    publisher_loop(bot, session, pool, settings),
                )
            finally:
                if web_runner:
                    await web_runner.cleanup()
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
