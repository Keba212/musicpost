import os
import unittest
from unittest.mock import patch

from bot import ImageResult, Settings, Track, make_caption


class SettingsTests(unittest.TestCase):
    @patch.dict(
        os.environ,
        {
            "BOT_TOKEN": "token",
            "TARGET_CHAT_ID": "@channel",
            "PEXELS_API_KEY": "pexels",
            "DATABASE_URL": "postgresql://example/db",
            "DRY_RUN": "false",
        },
        clear=True,
    )
    def test_target_channel_url_is_optional(self) -> None:
        settings = Settings.from_env()

        self.assertIsNone(settings.target_channel_url)


class CaptionTests(unittest.TestCase):
    def test_caption_omits_link_when_channel_url_missing(self) -> None:
        caption = make_caption(
            Track(queue_id=1, audio_file_id="audio", artist="Artist", name="Song"),
            ImageResult(download_url="https://example.com/image.jpg", photographer="Photographer"),
            Settings(
                bot_token="token",
                target_chat_id="@channel",
                target_channel_url=None,
                pexels_api_key="pexels",
                database_url="postgresql://example/db",
                admin_user_id=None,
                dry_run=False,
                interval_minutes=60,
                image_style="style",
                timeout_seconds=45,
                run_once=True,
            ),
        )

        self.assertIn("🎧MØOD | UA🇺🇦", caption)
        self.assertNotIn("<a href=", caption)


if __name__ == "__main__":
    unittest.main()
