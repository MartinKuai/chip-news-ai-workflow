from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401
from daily_chip_news.config import ConfigError, Settings


def valid_env() -> dict[str, str]:
    return {
        "GEMINI_API_KEY": "test-key",
        "RESEARCHER_MODEL": "researcher-model",
        "WRITER_MODEL": "writer-model",
        "REVIEWER_MODEL": "review-model",
        "TELEGRAM_BOT_TOKEN": "test-token",
        "TELEGRAM_CHAT_ID": "test-chat",
        "ARTICLES_PER_FEED": "3",
        "MAX_REVISIONS": "1",
    }


class SettingsTests(unittest.TestCase):
    def test_missing_api_key_fails_clearly(self) -> None:
        env = valid_env()
        env.pop("GEMINI_API_KEY")
        with self.assertRaisesRegex(ConfigError, "GEMINI_API_KEY is not configured"):
            Settings.from_env(env)

    def test_models_are_read_independently(self) -> None:
        settings = Settings.from_env(valid_env())
        self.assertEqual("researcher-model", settings.researcher_model)
        self.assertEqual("writer-model", settings.writer_model)
        self.assertEqual("review-model", settings.reviewer_model)
        self.assertEqual(3, settings.articles_per_feed)

    def test_quality_oriented_defaults(self) -> None:
        settings = Settings.from_env(valid_env())
        self.assertEqual(1, settings.max_revisions)
        self.assertEqual("low", settings.researcher_thinking_level)
        self.assertEqual("low", settings.writer_thinking_level)
        self.assertEqual("low", settings.reviewer_thinking_level)
        self.assertGreaterEqual(settings.gemini_timeout_seconds, 120.0)
        self.assertGreaterEqual(settings.gemini_max_attempts, 5)
        self.assertEqual(3072, settings.researcher_max_output_tokens)
        self.assertEqual(2560, settings.writer_max_output_tokens)
        self.assertEqual(1024, settings.reviewer_max_output_tokens)
        self.assertEqual(6, settings.max_candidates_per_run)
        self.assertEqual(12000, settings.article_content_chars)
        self.assertEqual(5, settings.health_window_size)
        self.assertEqual(3, settings.health_failure_threshold)

    def test_candidate_cap_is_configurable_and_bounded(self) -> None:
        env = valid_env()
        env["MAX_CANDIDATES_PER_RUN"] = "4"
        self.assertEqual(4, Settings.from_env(env).max_candidates_per_run)
        env["MAX_CANDIDATES_PER_RUN"] = "0"
        with self.assertRaisesRegex(ConfigError, "MAX_CANDIDATES_PER_RUN"):
            Settings.from_env(env)

    def test_max_revisions_is_parsed(self) -> None:
        env = valid_env()
        env["MAX_REVISIONS"] = "2"
        self.assertEqual(2, Settings.from_env(env).max_revisions)

    def test_max_revisions_is_bounded(self) -> None:
        env = valid_env()
        env["MAX_REVISIONS"] = "3"
        with self.assertRaisesRegex(ConfigError, "MAX_REVISIONS"):
            Settings.from_env(env)

    def test_placeholder_model_is_rejected(self) -> None:
        env = valid_env()
        env["WRITER_MODEL"] = "your_writer_model"
        with self.assertRaisesRegex(ConfigError, "WRITER_MODEL"):
            Settings.from_env(env)

    def test_invalid_thinking_level_is_rejected(self) -> None:
        env = valid_env()
        env["WRITER_THINKING_LEVEL"] = "extreme"
        with self.assertRaisesRegex(ConfigError, "WRITER_THINKING_LEVEL"):
            Settings.from_env(env)

    def test_threshold_above_window_is_rejected(self) -> None:
        env = valid_env()
        env["GEMINI_HEALTH_WINDOW"] = "4"
        env["GEMINI_HEALTH_THRESHOLD"] = "5"
        with self.assertRaisesRegex(ConfigError, "GEMINI_HEALTH_THRESHOLD"):
            Settings.from_env(env)

    def test_structured_output_flag_is_parsed(self) -> None:
        env = valid_env()
        env["GEMINI_STRUCTURED_OUTPUT"] = "off"
        self.assertFalse(Settings.from_env(env).gemini_structured_output)

    def test_run_budget_depends_on_run_mode(self) -> None:
        env = valid_env()
        env["RUN_MODE"] = "scheduled"
        self.assertEqual(0.20, Settings.from_env(env).run_budget_usd)
        env["RUN_MODE"] = "manual"
        env["MANUAL_RUN_BUDGET_USD"] = "0.03"
        self.assertEqual(0.03, Settings.from_env(env).run_budget_usd)

    def test_manual_budget_hard_max_is_enforced(self) -> None:
        env = valid_env()
        env["MANUAL_RUN_BUDGET_USD"] = "0.50"
        with self.assertRaisesRegex(ConfigError, "MANUAL_RUN_BUDGET_USD"):
            Settings.from_env(env)

    def test_manual_budget_may_be_zero_for_guard_verification(self) -> None:
        env = valid_env()
        env["MANUAL_RUN_BUDGET_USD"] = "0"
        self.assertEqual(0.0, Settings.from_env(env).run_budget_usd)

    def test_rolling_budget_default_is_below_the_promotional_credit(self) -> None:
        settings = Settings.from_env(valid_env())
        self.assertEqual(7.50, settings.gemini_rolling_30d_budget_usd)

    def test_timeout_cannot_be_set_below_the_quality_floor(self) -> None:
        env = valid_env()
        env["GEMINI_TIMEOUT_SECONDS"] = "10"
        with self.assertRaisesRegex(ConfigError, "GEMINI_TIMEOUT_SECONDS"):
            Settings.from_env(env)


if __name__ == "__main__":
    unittest.main()
