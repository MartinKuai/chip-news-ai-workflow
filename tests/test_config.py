from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401
from daily_chip_news.config import ConfigError, Settings


def valid_env() -> dict[str, str]:
    return {
        "GEMINI_API_KEY": "test-key",
        "RESEARCHER_MODEL": "research-model",
        "WRITER_MODEL": "writer-model",
        "REVIEWER_MODEL": "review-model",
        "TELEGRAM_BOT_TOKEN": "test-token",
        "TELEGRAM_CHAT_ID": "test-chat",
        "ARTICLES_PER_FEED": "3",
        "MAX_REVISIONS": "2",
    }


class SettingsTests(unittest.TestCase):
    def test_missing_api_key_fails_clearly(self) -> None:
        env = valid_env()
        env.pop("GEMINI_API_KEY")
        with self.assertRaisesRegex(ConfigError, "GEMINI_API_KEY is not configured"):
            Settings.from_env(env)

    def test_models_are_read_independently(self) -> None:
        settings = Settings.from_env(valid_env())
        self.assertEqual("research-model", settings.researcher_model)
        self.assertEqual("writer-model", settings.writer_model)
        self.assertEqual("review-model", settings.reviewer_model)
        self.assertEqual(3, settings.articles_per_feed)

    def test_max_revisions_is_parsed(self) -> None:
        env = valid_env()
        env["MAX_REVISIONS"] = "4"
        self.assertEqual(4, Settings.from_env(env).max_revisions)

    def test_placeholder_model_is_rejected(self) -> None:
        env = valid_env()
        env["WRITER_MODEL"] = "your_writer_model"
        with self.assertRaisesRegex(ConfigError, "WRITER_MODEL"):
            Settings.from_env(env)


if __name__ == "__main__":
    unittest.main()
