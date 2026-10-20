"""Website publisher: item mapping, stats, GitHub Git Data API and dry-run."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from _support import (  # noqa: F401
    CANDIDATE,
    DRAFT,
    SRC,
    FakeResponse,
    FakeSession,
    make_settings,  # noqa: E402
)

from daily_chip_news.publisher import PublisherError
from daily_chip_news.website import (
    SITE_TZ,
    ActionsRunHistory,
    GitHubSiteStore,
    LocalSiteStore,
    RunContext,
    SitePublisher,
    SitePublishError,
    WorkflowRun,
    brief_title,
    build_day_document,
    compute_stats,
    create_site_publisher,
    limit_sentences,
    safe_url,
    site_item,
)

REPO = "MartinKuai/martinkuai"
API = "https://api.github.com"
NOW = datetime(2026, 10, 20, 5, 1, 32, tzinfo=SITE_TZ)
CURRENT_RUN = 39323473694


def item(index: int) -> dict[str, Any]:
    return {
        "title": f"标题 {index}",
        "summary": f"第 {index} 条摘要。",
        "source": {"name": "Example Feed", "url": f"https://news.example/{index}"},
    }


def runs(
    count: int,
    *,
    failures: int = 0,
    start: datetime = datetime(2026, 1, 12, 23, 10, tzinfo=UTC),
    first_id: int = 1000,
) -> list[WorkflowRun]:
    """``count`` daily scheduled runs; the first ``failures`` of them failed."""
    return [
        WorkflowRun(
            id=first_id + index,
            status="completed",
            conclusion="failure" if index < failures else "success",
            created_at=start + timedelta(days=index),
        )
        for index in range(count)
    ]


def stats_doc(total: int, rate: float, updated_at: str) -> dict[str, Any]:
    return {
        "total_runs": total,
        "success_rate": rate,
        "since": "2026-01",
        "updated_at": updated_at,
    }


def stats(**overrides: Any):
    values: dict[str, Any] = {
        "previous": None,
        "history": None,
        "current_run_id": CURRENT_RUN,
        "scheduled": True,
        "succeeded": True,
        "counted_since": NOW - timedelta(minutes=5),
        "now": NOW,
        "default_since": "2026-01",
    }
    values.update(overrides)
    return compute_stats(**values)


class ItemMappingTests(unittest.TestCase):
    def test_item_uses_structured_draft_fields_and_rss_provenance(self) -> None:
        self.assertEqual(
            {
                "title": DRAFT["headline"],
                "summary": DRAFT["summary"],
                "source": {"name": CANDIDATE["source"], "url": CANDIDATE["url"]},
            },
            site_item(CANDIDATE, DRAFT),
        )

    def test_whitespace_and_control_characters_are_normalised(self) -> None:
        draft = {
            **DRAFT,
            "headline": " HBM\n供应\x07动态 ",
            "summary": "第一句。\n\n第二句。",
        }
        result = site_item(CANDIDATE, draft)
        self.assertEqual("HBM 供应 动态", result["title"])
        self.assertEqual("第一句。 第二句。", result["summary"])

    def test_summary_is_capped_at_three_sentences(self) -> None:
        draft = {**DRAFT, "summary": "一。二！三？四。"}
        self.assertEqual("一。二！三？", site_item(CANDIDATE, draft)["summary"])

    def test_non_http_urls_never_reach_the_website(self) -> None:
        for url in ("javascript:alert(1)", "ftp://x.example/a", "/relative", ""):
            with self.subTest(url=url):
                self.assertIsNone(site_item({**CANDIDATE, "url": url}, DRAFT))

    def test_missing_draft_or_text_is_unusable(self) -> None:
        self.assertIsNone(site_item(CANDIDATE, None))
        self.assertIsNone(site_item(CANDIDATE, {**DRAFT, "headline": "  "}))

    def test_aggregator_items_are_named_after_the_article_site(self) -> None:
        candidate = {
            **CANDIDATE,
            "url": "https://www.servers.example/cxl-pool",
            "source": "Hacker News: Newest",
            "metadata": {"feed_url": "https://hnrss.org/newest?points=100"},
        }
        self.assertEqual(
            "servers.example", site_item(candidate, DRAFT)["source"]["name"]
        )


class SentenceLimitTests(unittest.TestCase):
    def test_short_text_is_unchanged(self) -> None:
        self.assertEqual("一句话。两句话。", limit_sentences("一句话。两句话。"))
        self.assertEqual("没有句号", limit_sentences("没有句号"))

    def test_decimals_and_runs_of_terminators_do_not_split(self) -> None:
        text = "良率提升到 92.5%。真的吗？！是的。多余的一句。"
        self.assertEqual("良率提升到 92.5%。真的吗？！是的。", limit_sentences(text))

    def test_english_sentences(self) -> None:
        text = "One. Two! Three? Four."
        self.assertEqual("One. Two! Three?", limit_sentences(text))


class DayDocumentTests(unittest.TestCase):
    def test_document_matches_the_contract_exactly(self) -> None:
        document = build_day_document(
            date="2026-10-20",
            published_at="2026-10-20T05:01:32+08:00",
            items=[item(1), item(2)],
            run_number=281,
            run_status="success",
        )
        self.assertEqual(
            ["date", "published_at", "title", "items", "run"], list(document)
        )
        self.assertEqual({"number": 281, "status": "success"}, document["run"])
        self.assertEqual(["title", "summary", "source"], list(document["items"][0]))
        self.assertEqual(["name", "url"], list(document["items"][0]["source"]))
        self.assertEqual("标题 1 等 2 条简报", document["title"])

    def test_title_for_a_single_item_is_its_headline(self) -> None:
        self.assertEqual("标题 1", brief_title([item(1)]))
        self.assertEqual(
            "涨价了 等 2 条简报", brief_title([{"title": "涨价了！"}, item(2)])
        )
        self.assertEqual(
            "涨价了 等 2 条芯闻",
            brief_title([{"title": "涨价了！"}, item(2)], brief_name="芯闻"),
        )
        self.assertEqual(
            "涨价了 等 2 条 AI 简报",
            brief_title([{"title": "涨价了！"}, item(2)], brief_name="AI 简报"),
        )

    def test_same_day_rerun_merges_by_url_and_keeps_first_publication(self) -> None:
        existing = build_day_document(
            date="2026-10-20",
            published_at="2026-10-20T05:01:32+08:00",
            items=[item(1), item(2)],
            run_number=281,
            run_status="partial_success",
        )
        document = build_day_document(
            date="2026-10-20",
            published_at="2026-10-20T11:00:00+08:00",
            items=[item(2), item(3)],
            run_number=281,
            run_status="success",
            existing=existing,
        )
        urls = [entry["source"]["url"] for entry in document["items"]]
        self.assertEqual([f"https://news.example/{n}" for n in (1, 2, 3)], urls)
        self.assertEqual("2026-10-20T05:01:32+08:00", document["published_at"])
        self.assertEqual("success", document["run"]["status"])

    def test_existing_file_from_another_date_or_malformed_is_ignored(self) -> None:
        for existing in ({"date": "2026-10-19", "items": [item(9)]}, {"items": "x"}):
            document = build_day_document(
                date="2026-10-20",
                published_at="2026-10-20T05:01:32+08:00",
                items=[item(1)],
                run_number=1,
                run_status="success",
                existing=existing,
            )
            self.assertEqual(1, len(document["items"]))


class StatsTests(unittest.TestCase):
    def test_bootstrap_matches_the_actions_history(self) -> None:
        # 2026-10-20: GitHub lists 280 scheduled runs, 8 of them failed (97.1%).
        result = stats(history=runs(280, failures=8))
        self.assertEqual("actions-api", result.basis)
        self.assertEqual(281, result.document["total_runs"])
        self.assertEqual(round(273 / 281, 4), result.document["success_rate"])
        self.assertEqual("2026-01", result.document["since"])
        self.assertEqual("2026-10-20T05:01:32+08:00", result.document["updated_at"])

    def test_carries_forward_after_github_expires_old_runs(self) -> None:
        previous = stats_doc(280, 0.9714, "2026-10-19T05:01:45+08:00")
        visible = runs(90, start=datetime(2026, 7, 21, 20, 0, tzinfo=UTC))
        result = stats(previous=previous, history=visible)
        self.assertEqual("carried", result.basis)
        self.assertEqual(281, result.document["total_runs"])
        self.assertEqual(round(273 / 281, 4), result.document["success_rate"])

    def test_rate_round_trip_recovers_the_exact_success_count(self) -> None:
        previous = stats_doc(9999, round(9001 / 9999, 4), "2026-10-19T05:00:00+08:00")
        result = stats(previous=previous, history=None, succeeded=False)
        self.assertEqual(10000, result.document["total_runs"])
        self.assertEqual(round(9001 / 10000, 4), result.document["success_rate"])

    def test_runs_that_crashed_before_publishing_are_back_filled(self) -> None:
        previous = stats_doc(100, 1.0, "2026-10-17T05:00:00+08:00")
        crashed = WorkflowRun(
            id=77,
            status="completed",
            conclusion="failure",
            created_at=datetime(2026, 10, 18, 20, 0, tzinfo=UTC),
        )
        older = runs(10, start=datetime(2026, 10, 1, 20, 0, tzinfo=UTC))
        result = stats(previous=previous, history=[*older, crashed])
        self.assertEqual("carried", result.basis)
        self.assertEqual(102, result.document["total_runs"])
        self.assertEqual(round(101 / 102, 4), result.document["success_rate"])

    def test_failed_run_counts_as_failure(self) -> None:
        previous = stats_doc(10, 1.0, "2026-10-19T05:00:00+08:00")
        result = stats(previous=previous, succeeded=False)
        self.assertEqual(11, result.document["total_runs"])
        self.assertEqual(round(10 / 11, 4), result.document["success_rate"])

    def test_manual_runs_update_the_file_but_are_not_counted(self) -> None:
        previous = stats_doc(10, 1.0, "2026-10-19T05:00:00+08:00")
        result = stats(previous=previous, scheduled=False)
        self.assertFalse(result.counted_current)
        self.assertEqual(10, result.document["total_runs"])
        self.assertEqual("2026-10-20T05:01:32+08:00", result.document["updated_at"])

    def test_a_run_is_never_counted_twice(self) -> None:
        # Stats already written by this run (retried commit / re-run attempt).
        previous = stats_doc(11, 1.0, "2026-10-20T05:00:00+08:00")
        result = stats(
            previous=previous,
            counted_since=datetime(2026, 10, 20, 4, 56, tzinfo=SITE_TZ),
        )
        self.assertFalse(result.counted_current)
        self.assertEqual(11, result.document["total_runs"])

    def test_in_progress_runs_are_left_to_count_themselves(self) -> None:
        running = WorkflowRun(
            id=5, status="in_progress", conclusion=None, created_at=NOW
        )
        result = stats(history=[*runs(3), running])
        self.assertEqual(4, result.document["total_runs"])

    def test_a_development_sample_is_healed_by_the_api(self) -> None:
        sample = stats_doc(271, 0.97, "2026-10-20T07:02:00+08:00")
        result = stats(
            previous=sample,
            history=runs(280, failures=8),
            now=NOW + timedelta(days=1),
            counted_since=NOW + timedelta(days=1, minutes=-5),
        )
        self.assertEqual("actions-api", result.basis)
        self.assertEqual(281, result.document["total_runs"])

    def test_unusable_previous_stats_fall_back_to_the_api(self) -> None:
        for previous in (
            {"total_runs": 280, "success_rate": 0.97},  # no updated_at
            {"total_runs": "280", "success_rate": 0.97, "updated_at": "2026-10-19"},
            {"total_runs": 280, "success_rate": 1.5, "updated_at": "2026-10-19T00:00Z"},
        ):
            with self.subTest(previous=previous):
                result = stats(previous=previous, history=runs(5))
                self.assertEqual(6, result.document["total_runs"])

    def test_without_history_or_previous_the_count_starts_at_this_run(self) -> None:
        result = stats()
        self.assertEqual(1, result.document["total_runs"])
        self.assertEqual(1.0, result.document["success_rate"])


class ScriptedGitHub:
    """Scripted GitHub REST API for the site repo and the Actions run list."""

    def __init__(
        self,
        *,
        files: dict[str, str] | None = None,
        ref_responses: list[FakeResponse] | None = None,
        head: str = "head-sha",
    ) -> None:
        self.files = dict(files or {})
        self.head = head
        self.ref_responses = list(ref_responses or [])
        self.trees: list[dict[str, Any]] = []
        self.commits: list[dict[str, Any]] = []
        self.session = FakeSession(handler=self.handle)

    def handle(self, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        method = kwargs["method"]
        path = url.removeprefix(API)
        repo = f"/repos/{REPO}"
        if method == "GET" and path == f"{repo}/git/ref/heads/main":
            return FakeResponse(200, {"object": {"sha": self.head}})
        if method == "GET" and path.startswith(f"{repo}/contents/"):
            name = path.removeprefix(f"{repo}/contents/")
            if name not in self.files:
                return FakeResponse(404, {"message": "Not Found"})
            return FakeResponse(200, text=self.files[name])
        if method == "GET" and path.startswith(f"{repo}/git/commits/"):
            return FakeResponse(200, {"tree": {"sha": "base-tree"}})
        if method == "POST" and path == f"{repo}/git/trees":
            self.trees.append(kwargs["json"])
            return FakeResponse(201, {"sha": f"tree-{len(self.trees)}"})
        if method == "POST" and path == f"{repo}/git/commits":
            self.commits.append(kwargs["json"])
            return FakeResponse(201, {"sha": f"commit-{len(self.commits)}"})
        if method == "PATCH" and path == f"{repo}/git/refs/heads/main":
            if self.ref_responses:
                return self.ref_responses.pop(0)
            # Simulate the push landing: the branch now contains the files.
            for entry in self.trees[-1]["tree"]:
                self.files[entry["path"]] = entry["content"]
            return FakeResponse(200, {"object": {"sha": kwargs["json"]["sha"]}})
        raise AssertionError(f"Unexpected {method} {url}")

    def tree_files(self, index: int = -1) -> dict[str, Any]:
        return {
            entry["path"]: json.loads(entry["content"])
            for entry in self.trees[index]["tree"]
        }


def github_publisher(github: ScriptedGitHub, **kwargs: Any) -> SitePublisher:
    store = GitHubSiteStore(
        REPO, "main", "site-secret-token", session=github.session, sleep=lambda s: None
    )
    values: dict[str, Any] = {
        "run": RunContext(
            repository="MartinKuai/chip-news-ai-workflow",
            run_id=CURRENT_RUN,
            event="schedule",
        ),
        "now": lambda: NOW,
        "started_at": NOW - timedelta(minutes=5),
        "logger": None,
    }
    values.update(kwargs)
    return SitePublisher(store, **values)


class GitHubPublishTests(unittest.TestCase):
    def test_one_atomic_commit_with_news_and_stats(self) -> None:
        github = ScriptedGitHub(
            files={
                "data/news/stats.json": json.dumps(
                    stats_doc(279, 0.9713, "2026-10-19T03:05:10+08:00")
                )
            }
        )
        result = github_publisher(github).publish([item(1), item(2)], "SUCCESS")

        methods = [
            (method, url.removeprefix(API)) for method, url, _ in github.session.calls
        ]
        self.assertEqual(
            [
                ("GET", f"/repos/{REPO}/git/ref/heads/main"),
                ("GET", f"/repos/{REPO}/contents/data/news/stats.json"),
                ("GET", f"/repos/{REPO}/contents/data/news/2026-10-20.json"),
                ("GET", f"/repos/{REPO}/git/commits/head-sha"),
                ("POST", f"/repos/{REPO}/git/trees"),
                ("POST", f"/repos/{REPO}/git/commits"),
                ("PATCH", f"/repos/{REPO}/git/refs/heads/main"),
            ],
            methods,
        )
        for _, _, kwargs in github.session.calls:
            self.assertEqual(
                "Bearer site-secret-token", kwargs["headers"]["Authorization"]
            )
        read = github.session.calls[1][2]
        self.assertEqual({"ref": "head-sha"}, read["params"])
        self.assertEqual("application/vnd.github.raw+json", read["headers"]["Accept"])

        self.assertEqual("base-tree", github.trees[0]["base_tree"])
        self.assertTrue(all(e["mode"] == "100644" for e in github.trees[0]["tree"]))
        files = github.tree_files()
        self.assertEqual(
            {
                "total_runs": 280,
                "success_rate": 0.9714,
                "since": "2026-01",
                "updated_at": "2026-10-20T05:01:32+08:00",
            },
            files["data/news/stats.json"],
        )
        day = files["data/news/2026-10-20.json"]
        self.assertEqual("2026-10-20", day["date"])
        self.assertEqual("2026-10-20T05:01:32+08:00", day["published_at"])
        self.assertEqual({"number": 280, "status": "success"}, day["run"])
        self.assertEqual([item(1), item(2)], day["items"])

        self.assertEqual(["head-sha"], github.commits[0]["parents"])
        self.assertIn(
            "news: 2026-10-20 daily chip news (2 items)", github.commits[0]["message"]
        )
        self.assertIn(f"actions/runs/{CURRENT_RUN}", github.commits[0]["message"])
        patch_body = github.session.calls[-1][2]["json"]
        self.assertEqual({"sha": "commit-1", "force": False}, patch_body)
        self.assertEqual("commit-1", result.location)
        self.assertTrue(result.news_written)

    def test_hold_writes_stats_only(self) -> None:
        github = ScriptedGitHub()
        result = github_publisher(github).publish([], "SUCCESS")
        self.assertEqual(["data/news/stats.json"], list(github.tree_files()))
        self.assertFalse(result.news_written)
        self.assertIn(
            "stats only (no item passed review)", github.commits[0]["message"]
        )
        reads = [url for method, url, _ in github.session.calls if "/contents/" in url]
        self.assertEqual(1, len(reads))

    def test_conflicting_push_rereads_and_retries(self) -> None:
        github = ScriptedGitHub(
            files={
                "data/news/stats.json": json.dumps(
                    stats_doc(10, 1.0, "2026-10-19T05:00:00+08:00")
                )
            },
            ref_responses=[
                FakeResponse(422, {"message": "Update is not a fast forward"})
            ],
        )
        github_publisher(github).publish([item(1)], "SUCCESS")
        self.assertEqual(2, len(github.commits))
        ref_reads = [
            c for c in github.session.calls if c[1].endswith("/git/ref/heads/main")
        ]
        self.assertEqual(2, len(ref_reads))
        self.assertEqual(11, github.tree_files()["data/news/stats.json"]["total_runs"])

    def test_branch_that_keeps_moving_fails_transiently(self) -> None:
        conflict = FakeResponse(422, {"message": "Update is not a fast forward"})
        github = ScriptedGitHub(ref_responses=[conflict, conflict, conflict])
        with self.assertRaises(SitePublishError) as context:
            github_publisher(github).publish([item(1)], "SUCCESS")
        self.assertTrue(context.exception.transient)

    def test_bad_token_is_a_global_publish_error_without_secrets(self) -> None:
        session = FakeSession(FakeResponse(401, {"message": "Bad credentials"}))
        store = GitHubSiteStore(REPO, "main", "site-secret-token", session=session)
        publisher = SitePublisher(store, now=lambda: NOW, logger=None)
        with self.assertRaises(PublisherError) as context:
            publisher.publish([item(1)], "SUCCESS")
        self.assertTrue(context.exception.global_failure)
        self.assertEqual(401, context.exception.status_code)
        self.assertNotIn("site-secret-token", str(context.exception))

    def test_server_errors_are_retried(self) -> None:
        github = ScriptedGitHub()
        original = github.handle
        failures = [FakeResponse(502, text="bad gateway")]

        def flaky(url: str, kwargs: dict[str, Any]) -> FakeResponse:
            return failures.pop(0) if failures else original(url, kwargs)

        github.session.handler = flaky
        github_publisher(github).publish([item(1)], "SUCCESS")
        self.assertEqual(1, len(github.commits))

    def test_network_failure_is_transient_and_leaks_nothing(self) -> None:
        session = FakeSession(handler=lambda url, kwargs: requests.ConnectionError(url))
        store = GitHubSiteStore(
            REPO, "main", "site-secret-token", session=session, sleep=lambda s: None
        )
        with self.assertRaises(SitePublishError) as context:
            SitePublisher(store, now=lambda: NOW, logger=None).publish([], "SUCCESS")
        self.assertTrue(context.exception.transient)
        self.assertNotIn("site-secret-token", str(context.exception))
        self.assertEqual(3, len(session.calls))

    def test_history_failure_still_publishes_with_carried_stats(self) -> None:
        def broken_history() -> list[WorkflowRun]:
            raise SitePublishError("GitHub GET failed with HTTP 503", status_code=503)

        github = ScriptedGitHub(
            files={
                "data/news/stats.json": json.dumps(
                    stats_doc(279, 0.9713, "2026-10-19T03:05:10+08:00")
                )
            }
        )
        result = github_publisher(github, history=broken_history).publish([], "SUCCESS")
        self.assertEqual("carried", result.basis)
        self.assertEqual(280, result.stats["total_runs"])

    def test_rerun_attempt_does_not_count_the_run_again(self) -> None:
        created = datetime(2026, 10, 19, 20, 56, 25, tzinfo=UTC)
        history = [
            WorkflowRun(
                id=CURRENT_RUN,
                status="in_progress",
                conclusion=None,
                created_at=created,
            )
        ]
        github = ScriptedGitHub(
            files={
                # Written by attempt 1 of this run.
                "data/news/stats.json": json.dumps(
                    stats_doc(280, 0.9714, "2026-10-20T05:01:00+08:00")
                )
            }
        )
        publisher = github_publisher(
            github,
            run=RunContext(run_id=CURRENT_RUN, run_attempt=2, event="schedule"),
            history=lambda: history,
            now=lambda: NOW + timedelta(hours=1),
            started_at=NOW + timedelta(minutes=55),
        )
        result = publisher.publish([item(1)], "SUCCESS")
        self.assertFalse(result.counted_current)
        self.assertEqual(280, result.stats["total_runs"])


class ActionsRunHistoryTests(unittest.TestCase):
    def test_reads_every_page_of_scheduled_runs(self) -> None:
        def page(start: int, count: int) -> dict[str, Any]:
            return {
                "total_count": 150,
                "workflow_runs": [
                    {
                        "id": start + offset,
                        "status": "completed",
                        "conclusion": "success",
                        "created_at": "2026-10-19T20:56:25Z",
                    }
                    for offset in range(count)
                ],
            }

        session = FakeSession(
            FakeResponse(200, page(0, 100)), FakeResponse(200, page(100, 50))
        )
        history = ActionsRunHistory(
            "MartinKuai/chip-news-ai-workflow",
            "daily_news.yml",
            "gh-token",
            session=session,
        )
        result = history()
        self.assertEqual(150, len(result))
        self.assertEqual(
            datetime(2026, 10, 19, 20, 56, 25, tzinfo=UTC), result[0].created_at
        )
        method, url, kwargs = session.calls[0]
        self.assertEqual(
            f"{API}/repos/MartinKuai/chip-news-ai-workflow/actions/workflows/daily_news.yml/runs",
            url,
        )
        self.assertEqual(
            {"event": "schedule", "per_page": 100, "page": 1}, kwargs["params"]
        )
        self.assertEqual("Bearer gh-token", kwargs["headers"]["Authorization"])
        self.assertEqual(2, session.calls[1][2]["params"]["page"])


class LocalDryRunTests(unittest.TestCase):
    def test_dry_run_writes_both_files_and_merges_a_same_day_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = RunContext(event="schedule", run_id=CURRENT_RUN)
            publisher = SitePublisher(
                LocalSiteStore(directory), run=run, now=lambda: NOW, logger=None
            )
            publisher.publish([item(1)], "PARTIAL_SUCCESS")
            later = SitePublisher(
                LocalSiteStore(directory),
                run=RunContext(event="workflow_dispatch"),
                now=lambda: NOW + timedelta(hours=6),
                logger=None,
            )
            later.publish([item(1), item(2)], "SUCCESS")
            root = Path(directory) / "data" / "news"
            day = json.loads((root / "2026-10-20.json").read_text(encoding="utf-8"))
            stats_file = json.loads((root / "stats.json").read_text(encoding="utf-8"))
            self.assertEqual(
                ["2026-10-20.json", "stats.json"],
                sorted(p.name for p in root.iterdir()),
            )
        self.assertEqual([item(1), item(2)], day["items"])
        self.assertEqual("2026-10-20T05:01:32+08:00", day["published_at"])
        self.assertEqual({"number": 1, "status": "success"}, day["run"])
        self.assertEqual(1, stats_file["total_runs"])
        self.assertEqual("2026-10-20T11:01:32+08:00", stats_file["updated_at"])

    def test_dry_run_hold_writes_only_stats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            SitePublisher(
                LocalSiteStore(directory),
                run=RunContext(event="schedule"),
                now=lambda: NOW,
                logger=None,
            ).publish([], "SUCCESS")
            names = sorted(
                p.name for p in (Path(directory) / "data" / "news").iterdir()
            )
        self.assertEqual(["stats.json"], names)


class FactoryTests(unittest.TestCase):
    def test_modes(self) -> None:
        self.assertIsNone(create_site_publisher(make_settings(site_publish_mode="off")))
        github = create_site_publisher(
            make_settings(
                site_publish_mode="github", publish_enabled=True, site_repo_token="t"
            ),
            env={},
        )
        self.assertEqual("github:MartinKuai/martinkuai@main", github.store)
        dry = create_site_publisher(
            make_settings(site_publish_mode="github", publish_enabled=False), env={}
        )
        self.assertEqual("local:output/site", dry.store)

    def test_run_context_from_actions_environment(self) -> None:
        run = RunContext.from_env(
            {
                "GITHUB_REPOSITORY": "MartinKuai/chip-news-ai-workflow",
                "GITHUB_RUN_ID": "39323473694",
                "GITHUB_RUN_ATTEMPT": "2",
                "GITHUB_EVENT_NAME": "schedule",
                "GITHUB_TOKEN": "gh-token",
            }
        )
        self.assertTrue(run.scheduled)
        self.assertEqual(2, run.run_attempt)
        self.assertEqual(
            "https://github.com/MartinKuai/chip-news-ai-workflow/actions/runs/39323473694",
            run.url,
        )
        self.assertNotIn("gh-token", repr(run))


if __name__ == "__main__":
    unittest.main()


class CodexRequiredTests(unittest.TestCase):
    """Specific test requirements from docs/codex-tasks/daily-chip-news.md:
    1. 无新闻日只更新 stats (no-news day updates only stats)
    2. quota_stopped (run status and stats counting)
    3. 重复运行 (same day merge and rerun attempt idempotency)
    4. 非法 URL (illegal url rejection)
    5. 超长摘要 (summary capping at 3 sentences with edge cases)
    6. BRIEF_NAME (configurable brief name in output data)
    """

    def test_no_news_day_writes_only_stats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            publisher = SitePublisher(
                LocalSiteStore(directory),
                run=RunContext(event="schedule", run_id=123),
                now=lambda: NOW,
                logger=None,
            )
            result = publisher.publish([], "SUCCESS")
            self.assertFalse(result.news_written)
            self.assertEqual(0, result.items)
            self.assertEqual(1, result.stats["total_runs"])
            self.assertEqual("2026-01", result.stats["since"])

            news_dir = Path(directory) / "data" / "news"
            self.assertTrue((news_dir / "stats.json").is_file())
            self.assertFalse((news_dir / "2026-10-20.json").exists())

    def test_quota_stopped_outcome_and_stats(self) -> None:
        document = build_day_document(
            date="2026-10-20",
            published_at="2026-10-20T05:01:32+08:00",
            items=[item(1)],
            run_number=282,
            run_status="quota_stopped",
        )
        self.assertEqual("quota_stopped", document["run"]["status"])
        self.assertEqual(282, document["run"]["number"])

        stats_res = compute_stats(
            previous={
                "total_runs": 10,
                "success_rate": 1.0,
                "since": "2026-01",
                "updated_at": "2026-10-19T05:00:00+08:00",
            },
            history=None,
            current_run_id=124,
            scheduled=True,
            succeeded=True,  # QUOTA_STOPPED != FAILED -> succeeded=True
            counted_since=NOW,
            now=NOW,
            default_since="2026-01",
        )
        self.assertTrue(stats_res.counted_current)
        self.assertEqual(11, stats_res.document["total_runs"])
        self.assertEqual(1.0, stats_res.document["success_rate"])

    def test_rerun_and_duplicate_runs_idempotency(self) -> None:
        # Same-day merge dedupes by URL
        existing = build_day_document(
            date="2026-10-20",
            published_at="2026-10-20T05:01:32+08:00",
            items=[item(1), item(2)],
            run_number=280,
            run_status="success",
        )
        re_run = build_day_document(
            date="2026-10-20",
            published_at="2026-10-20T08:00:00+08:00",
            items=[item(2), item(3)],  # item(2) is duplicate
            run_number=280,
            run_status="success",
            existing=existing,
        )
        self.assertEqual(3, len(re_run["items"]))
        self.assertEqual(["https://news.example/1", "https://news.example/2", "https://news.example/3"],
                         [it["source"]["url"] for it in re_run["items"]])
        self.assertEqual("2026-10-20T05:01:32+08:00", re_run["published_at"])

        # Actions re-run attempt > 1 does not double count total_runs
        run_history = [
            WorkflowRun(
                id=999,
                status="completed",
                conclusion="success",
                created_at=datetime(2026, 10, 20, 4, 0, 0, tzinfo=SITE_TZ),
            )
        ]
        # Previous stats written at 04:05:00 by attempt 1
        prev_stats = {
            "total_runs": 50,
            "success_rate": 1.0,
            "since": "2026-01",
            "updated_at": "2026-10-20T04:05:00+08:00",
        }
        res = compute_stats(
            previous=prev_stats,
            history=run_history,
            current_run_id=999,
            scheduled=True,
            succeeded=True,
            # attempt's created_at:
            counted_since=datetime(2026, 10, 20, 4, 0, 0, tzinfo=SITE_TZ),
            now=NOW,
            default_since="2026-01",
        )
        self.assertFalse(res.counted_current)
        self.assertEqual(50, res.document["total_runs"])

    def test_illegal_urls_safe_url_and_site_item(self) -> None:
        illegal = [
            "javascript:alert(1)",
            "file:///etc/passwd",
            "ftp://files.example/data",
            "data:text/html;base64,PHNjcmlwdD4=",
            "/path/to/page",
            "relative-url",
            "http://",
            "https://",
            "https://example.com/has space/test",
            "",
            None,
            12345,
        ]
        for url in illegal:
            with self.subTest(url=url):
                self.assertEqual("", safe_url(url))
                self.assertIsNone(site_item({**CANDIDATE, "url": url}, DRAFT))

        valid = [
            "https://openai.com/index/gpt-4o",
            "http://news.ycombinator.com/item?id=123",
            "https://blog.google/technology/ai/?page=1#frag",
        ]
        for url in valid:
            with self.subTest(url=url):
                self.assertEqual(url, safe_url(url))
                res = site_item({**CANDIDATE, "url": url}, DRAFT)
                self.assertIsNotNone(res)
                self.assertEqual(url, res["source"]["url"])

    def test_overly_long_summary_sentence_capping(self) -> None:
        # 5 sentences truncated to 3
        text5 = (
            "第一句关于大模型发布。"
            "第二句介绍参数提升！"
            "第三句说明定价与时间？"
            "第四句是多余评论。"
            "第五句是计划。"
        )
        expected3 = "第一句关于大模型发布。第二句介绍参数提升！第三句说明定价与时间？"
        self.assertEqual(expected3, limit_sentences(text5))

        # Decimals mid-sentence like GPT-4.5 or 98.5% do not split
        text_with_decimals = (
            "OpenAI 推出 GPT-4.5。"
            "准确率达 98.5%！"
            "支持 128k 窗口。"
            "第四句不应出现。"
        )
        expected_decimals = "OpenAI 推出 GPT-4.5。准确率达 98.5%！支持 128k 窗口。"
        self.assertEqual(expected_decimals, limit_sentences(text_with_decimals))

        # Sentences with quotes and brackets
        text_quotes = (
            "专家表示：“技术突破。”"
            "第二句跟进评估！"
            "第三句总结反响。"
            "第四句多余。"
        )
        expected_quotes = "专家表示：“技术突破。”第二句跟进评估！第三句总结反响。"
        self.assertEqual(expected_quotes, limit_sentences(text_quotes))

        # site_item enforces sentence limit on draft
        long_draft = {**DRAFT, "summary": text5}
        mapped = site_item(CANDIDATE, long_draft)
        self.assertIsNotNone(mapped)
        self.assertEqual(expected3, mapped["summary"])

    def test_brief_name_configuration(self) -> None:
        items = [item(1), item(2)]
        # Default brief name is "简报"
        self.assertEqual("标题 1 等 2 条简报", brief_title(items))
        # Custom brief name "芯闻"
        self.assertEqual("标题 1 等 2 条芯闻", brief_title(items, brief_name="芯闻"))
        # Custom brief name "AI 简报"
        self.assertEqual(
            "标题 1 等 2 条 AI 简报", brief_title(items, brief_name="AI 简报")
        )

        doc_default = build_day_document(
            date="2026-10-20",
            published_at="2026-10-20T05:01:32+08:00",
            items=items,
            run_number=1,
            run_status="success",
        )
        self.assertEqual("标题 1 等 2 条简报", doc_default["title"])

        doc_custom = build_day_document(
            date="2026-10-20",
            published_at="2026-10-20T05:01:32+08:00",
            items=items,
            run_number=1,
            run_status="success",
            brief_name="芯闻",
        )
        self.assertEqual("标题 1 等 2 条芯闻", doc_custom["title"])
