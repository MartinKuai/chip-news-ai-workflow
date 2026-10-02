from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401

from daily_chip_news.config import DEFAULT_SOURCES, ConfigError, Settings

BASE_ENV = {
    "GEMINI_API_KEY": "gemini-key",
    "RESEARCHER_MODEL": "researcher-model",
    "WRITER_MODEL": "writer-model",
    "REVIEWER_MODEL": "reviewer-model",
    "TELEGRAM_BOT_TOKEN": "telegram-token",
    "TELEGRAM_CHAT_ID": "telegram-chat",
}


def env(**overrides: str) -> dict[str, str]:
    values = dict(BASE_ENV)
    values.update(overrides)
    return values


class ConfigDefaultsTests(unittest.TestCase):
    def test_node_profiles_come_from_their_own_variables(self) -> None:
        settings = Settings.from_env(env())
        self.assertEqual("researcher-model", settings.researcher.model)
        self.assertEqual("writer-model", settings.writer.model)
        self.assertEqual("reviewer-model", settings.reviewer.model)

    def test_generation_defaults_follow_the_verified_values(self) -> None:
        settings = Settings.from_env(env())
        self.assertEqual("low", settings.researcher.thinking_level)
        self.assertEqual("low", settings.writer.thinking_level)
        self.assertEqual("low", settings.reviewer.thinking_level)
        self.assertEqual(3072, settings.researcher.max_output_tokens)
        self.assertEqual(2560, settings.writer.max_output_tokens)
        self.assertEqual(1024, settings.reviewer.max_output_tokens)

    def test_pipeline_defaults(self) -> None:
        settings = Settings.from_env(env())
        self.assertEqual(DEFAULT_SOURCES, settings.source_feeds)
        self.assertEqual(2, settings.articles_per_feed)
        self.assertEqual(6, settings.max_candidates_per_run)
        self.assertEqual(72.0, settings.max_candidate_age_hours)
        self.assertEqual(1, settings.max_revisions)
        self.assertEqual(12000, settings.article_content_chars)

    def test_client_and_breaker_defaults(self) -> None:
        settings = Settings.from_env(env())
        self.assertEqual(180.0, settings.gemini_timeout_seconds)
        self.assertEqual(3, settings.gemini_max_attempts)
        self.assertEqual(240.0, settings.gemini_call_budget_seconds)
        self.assertTrue(settings.gemini_structured_output)
        self.assertEqual((), settings.gemini_fallback_models)
        self.assertEqual(5, settings.breaker_window_size)
        self.assertEqual(3, settings.breaker_failure_threshold)
        self.assertEqual(2100.0, settings.run_budget_seconds)

    def test_fallback_models_are_an_ordered_comma_list(self) -> None:
        settings = Settings.from_env(env(GEMINI_FALLBACK_MODELS=" model-x , ,model-y "))
        self.assertEqual(("model-x", "model-y"), settings.gemini_fallback_models)

    def test_publishing_defaults(self) -> None:
        settings = Settings.from_env(env())
        self.assertTrue(settings.publish_enabled)
        self.assertEqual(15.0, settings.telegram_timeout_seconds)
        self.assertEqual("", settings.summary_path)


class ConfigOverrideTests(unittest.TestCase):
    def test_per_node_overrides(self) -> None:
        settings = Settings.from_env(
            env(
                RESEARCHER_THINKING_LEVEL="high",
                WRITER_THINKING_LEVEL="off",
                REVIEWER_MAX_OUTPUT_TOKENS="2048",
                MAX_CANDIDATES_PER_RUN="3",
                MAX_REVISIONS="2",
                ARTICLES_PER_FEED="4",
                ARTICLE_CONTENT_CHARS="8000",
            )
        )
        self.assertEqual("high", settings.researcher.thinking_level)
        self.assertEqual("", settings.writer.thinking_level)
        self.assertEqual(2048, settings.reviewer.max_output_tokens)
        self.assertEqual(3, settings.max_candidates_per_run)
        self.assertEqual(2, settings.max_revisions)
        self.assertEqual(4, settings.articles_per_feed)
        self.assertEqual(8000, settings.article_content_chars)

    def test_source_feeds_accept_commas_and_newlines(self) -> None:
        settings = Settings.from_env(
            env(SOURCE_FEEDS="https://a.example/feed,\nhttps://b.example/feed")
        )
        self.assertEqual(
            ("https://a.example/feed", "https://b.example/feed"),
            settings.source_feeds,
        )

    def test_recency_filter_can_be_disabled(self) -> None:
        settings = Settings.from_env(env(MAX_CANDIDATE_AGE_HOURS="0"))
        self.assertEqual(0.0, settings.max_candidate_age_hours)

    def test_publishing_overrides(self) -> None:
        settings = Settings.from_env(
            env(
                PUBLISH_ENABLED="0",
                RUN_SUMMARY_PATH="output/run-summary.json",
                BREAKER_WINDOW="8",
                BREAKER_THRESHOLD="4",
            )
        )
        self.assertFalse(settings.publish_enabled)
        self.assertEqual("output/run-summary.json", settings.summary_path)
        self.assertEqual(8, settings.breaker_window_size)
        self.assertEqual(4, settings.breaker_failure_threshold)


class ConfigErrorTests(unittest.TestCase):
    def test_missing_required_value(self) -> None:
        values = env()
        values.pop("GEMINI_API_KEY")
        with self.assertRaises(ConfigError):
            Settings.from_env(values)

    def test_placeholder_value_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            Settings.from_env(env(GEMINI_API_KEY="your_gemini_api_key"))

    def test_non_integer_and_non_boolean_values(self) -> None:
        with self.assertRaises(ConfigError):
            Settings.from_env(env(MAX_CANDIDATES_PER_RUN="many"))
        with self.assertRaises(ConfigError):
            Settings.from_env(env(PUBLISH_ENABLED="sometimes"))

    def test_unknown_thinking_level(self) -> None:
        with self.assertRaises(ConfigError):
            Settings.from_env(env(REVIEWER_THINKING_LEVEL="max"))

    def test_threshold_must_not_exceed_window(self) -> None:
        with self.assertRaises(ConfigError):
            Settings.from_env(env(BREAKER_WINDOW="3", BREAKER_THRESHOLD="4"))

    def test_empty_source_feeds_are_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            Settings.from_env(env(SOURCE_FEEDS=" , "))


if __name__ == "__main__":
    unittest.main()
