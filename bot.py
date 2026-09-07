from __future__ import annotations

import asyncio
import logging
import os
import random
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp
import asyncpg
from aiogram import Bot
from aiogram.types import FSInputFile


LOGGER = logging.getLogger("ukrainian_music_bot")
JAMENDO_TRACKS_URL = "https://api.jamendo.com/v3.0/tracks/"
PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"
RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class Settings:
    bot_token: str
    target_chat_id: str
    jamendo_client_id: str
    pexels_api_key: str
    database_url: str
    dry_run: bool
    interval_minutes: int
    image_style: str
    timeout_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        required = ("BOT_TOKEN", "TARGET_CHAT_ID", "JAMENDO_CLIENT_ID", "PEXELS_API_KEY", "DATABASE_URL")
        missing = [name for name in required if not os.getenv(name)]
        if missing and os.getenv("DRY_RUN", "true").lower() != "true":
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
        return cls(
            bot_token=os.getenv("BOT_TOKEN", "dry-run-token"),
            target_chat_id=os.getenv("TARGET_CHAT_ID", "dry-run-chat"),
            jamendo_client_id=os.getenv("JAMENDO_CLIENT_ID", "dry-run-client"),
            pexels_api_key=os.getenv("PEXELS_API_KEY", "dry-run-key"),
            database_url=os.getenv("DATABASE_URL", "postgresql://localhost/ukrainian_music"),
            dry_run=os.getenv("DRY_RUN", "true").lower() == "true",
            interval_minutes=int(os.getenv("POST_INTERVAL_MINUTES", "360")),
            image_style=os.getenv(
                "IMAGE_STYLE",
                "dark cinematic Ukrainian music aesthetic, night city, neon, film grain, blue and yellow accents",
            ),
            timeout_seconds=int(os.getenv("HTTP_TIMEOUT_SECONDS", "45")),
        )


@dataclass(frozen=True)
class Track:
    track_id: str
    artist: str
    name: str
    download_url: str
    share_url: str
    license_url: str


@dataclass(frozen=True)
class ImageResult:
    download_url: str
    page_url: str
    photographer: str
    photographer_url: str


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


async def download_file(session: aiohttp.ClientSession, url: str, destination: Path) -> None:
    async with session.get(url) as response:
        if response.status in RETRYABLE_STATUS_CODES:
            raise RuntimeError(f"HTTP {response.status}")
        response.raise_for_status()
        with destination.open("wb") as file:
            async for chunk in response.content.iter_chunked(64 * 1024):
                file.write(chunk)


async def find_tracks(session: aiohttp.ClientSession, settings: Settings, pool: asyncpg.Pool) -> list[Track]:
    LOGGER.info("Searching Ukrainian music...")
    payload = await with_retries(
        lambda: request_json(
            session,
            JAMENDO_TRACKS_URL,
            params={
                "client_id": settings.jamendo_client_id,
                "format": "json",
                "limit": 30,
                "fuzzytags": "ukrainian",
                "audiodlformat": "mp32",
                "include": "licenses",
                "type": "single albumtrack",
            },
        ),
        "Jamendo search",
    )
    candidates = payload.get("results", [])
    tracks: list[Track] = []
    random.shuffle(candidates)
    for item in candidates:
        if not item.get("audiodownload_allowed") or not item.get("audiodownload"):
            continue
        track_id = str(item["id"])
        exists = await pool.fetchval("SELECT 1 FROM published_tracks WHERE track_id = $1", track_id)
        LOGGER.info("Checking duplicate...")
        if exists:
            continue
        track = Track(
            track_id=track_id,
            artist=item.get("artist_name", "Unknown artist"),
            name=item.get("name", "Untitled"),
            download_url=item["audiodownload"],
            share_url=item.get("shareurl", item.get("shorturl", "https://www.jamendo.com/")),
            license_url=item.get("license_ccurl", "https://www.jamendo.com/legal/attribution"),
        )
        LOGGER.info("Found: %s - %s", track.artist, track.name)
        tracks.append(track)
    return tracks


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
        page_url=photo["url"],
        photographer=photo["photographer"],
        photographer_url=photo["photographer_url"],
    )


def make_caption(track: Track, image: ImageResult) -> str:
    return (
        f"🇺🇦 <b>{track.artist} - {track.name}</b>\n\n"
        f"🎵 Licensed download from <a href=\"{track.share_url}\">Jamendo</a>\n"
        f"📷 Photo by <a href=\"{image.photographer_url}\">{image.photographer}</a> on "
        f"<a href=\"https://www.pexels.com\">Pexels</a>\n"
        f"🔗 <a href=\"{track.license_url}\">Track license</a>"
    )


async def init_database(pool: asyncpg.Pool) -> None:
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS published_tracks (
            track_id TEXT PRIMARY KEY,
            artist TEXT NOT NULL,
            name TEXT NOT NULL,
            telegram_message_id BIGINT,
            published_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


async def publish_once(
    bot: Bot,
    session: aiohttp.ClientSession,
    pool: asyncpg.Pool,
    settings: Settings,
) -> bool:
    tracks = await find_tracks(session, settings, pool)
    if not tracks:
        LOGGER.warning("No downloadable, unpublished track found")
        return False
    for track in tracks:
        try:
            image = await find_image(session, settings)
            caption = make_caption(track, image)
            if settings.dry_run:
                LOGGER.info("DRY_RUN=true; skipping download, publishing, and database insert")
                return True

            with tempfile.TemporaryDirectory(prefix="ukrainian-music-") as temporary_dir:
                image_path = Path(temporary_dir) / "cover.jpg"
                audio_path = Path(temporary_dir) / "track.mp3"
                LOGGER.info("Downloading audio...")
                await with_retries(lambda: download_file(session, track.download_url, audio_path), "audio download")
                await with_retries(lambda: download_file(session, image.download_url, image_path), "image download")
                LOGGER.info("Publishing...")
                photo_message = await with_retries(
                    lambda: bot.send_photo(settings.target_chat_id, FSInputFile(image_path), caption=caption),
                    "Telegram photo publish",
                )
                await with_retries(
                    lambda: bot.send_audio(
                        settings.target_chat_id,
                        FSInputFile(audio_path),
                        title=track.name,
                        performer=track.artist,
                        caption=caption,
                    ),
                    "Telegram audio publish",
                )
                await pool.execute(
                    "INSERT INTO published_tracks (track_id, artist, name, telegram_message_id) VALUES ($1, $2, $3, $4)",
                    track.track_id,
                    track.artist,
                    track.name,
                    photo_message.message_id,
                )
            LOGGER.info("Published successfully")
            return True
        except Exception:
            LOGGER.exception("Track failed; skipping it and trying another candidate")
    return False


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    settings = Settings.from_env()
    if settings.dry_run:
        LOGGER.info("DRY_RUN=true")
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=3)
    await init_database(pool)
    timeout = aiohttp.ClientTimeout(total=settings.timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session, Bot(settings.bot_token) as bot:
        while True:
            try:
                await publish_once(bot, session, pool, settings)
            except Exception:
                LOGGER.exception("Post cycle failed; the bot will continue")
            LOGGER.info("Next post in %d minutes", settings.interval_minutes)
            await asyncio.sleep(settings.interval_minutes * 60)
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
