from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from _support import CANDIDATE, CaptureClient, FakeResponse, make_settings
from test_website import ScriptedGitHub, github_publisher

from daily_chip_news.graph import build_editorial_graph
from daily_chip_news.nodes import ResearcherNode, ReviewerNode, WriterNode
from daily_chip_news.publisher import PublisherNode
from daily_chip_news.quota import QuotaStateError, QuotaStopped
from daily_chip_news.runner import run_daily
from daily_chip_news.sources import SourceCollectionError, SourceCollectionResult
from daily_chip_news.trace import MAX_TRACE_BYTES, TRACE_TZ, TraceRecorder
from daily_chip_news.website import LocalSiteStore, RunContext, SitePublisher
from scripts.export_trace_sample import draft, render, research, review

NOW = datetime(2026, 10, 21, 0, 5, 12, tzinfo=TRACE_TZ)


def recorder(
    count: int = 1, *, title: str = "原文标题", url: str | None = None, **kwargs
) -> TraceRecorder:
    trace = TraceRecorder(started_at=NOW, **kwargs)
    for index in range(count):
        trace.add_candidate(
            {
                **CANDIDATE,
                "id": f"c{index:02d}",
                "title": title,
                "url": url or CANDIDATE["url"],
            },
            source_name="示例来源",
        )
    return trace


def complete(
    trace: TraceRecorder,
    index: int = 0,
    *,
    verdict: str = "PASS",
    published: bool = True,
) -> None:
    candidate = {"id": f"c{index:02d}"}
    trace.observe(candidate, "researcher", {"research_notes": research()})
    trace.observe(candidate, "writer", {"draft": draft("示例模型更新")})
    trace.observe(candidate, "reviewer", {"review": review(verdict)})
    if verdict == "PASS" and published:
        trace.mark_published(candidate)


def finish(trace: TraceRecorder, outcome: str = "SUCCESS") -> dict:
    return json.loads(
        trace.finish(
            date="2026-10-21",
            number=282,
            outcome=outcome,
            finished_at=NOW + timedelta(minutes=2),
        )
    )


class TraceContractTests(unittest.TestCase):
    def assert_counts(self, document: dict) -> None:
        rows = document["candidates"]
        expected = {
            "fetched": len(rows),
            "kept": sum(
                bool(r["researcher"] and r["researcher"]["decision"] == "keep")
                for r in rows
            ),
            "drafted": sum(r["writer"] is not None for r in rows),
            "approved": sum(
                bool(r["reviewer"] and r["reviewer"]["verdict"] == "approve")
                for r in rows
            ),
            "published": sum(r["published"] is True for r in rows),
        }
        self.assertEqual(expected, document["counts"])

    def test_exact_keys_types_statuses_and_counts(self) -> None:
        trace = recorder(4)
        complete(trace)
        complete(trace, 1, verdict="REJECT")
        trace.observe({"id": "c02"}, "researcher", {"research_notes": research("SKIP")})
        document = finish(trace)
        self.assertEqual({"date", "run", "counts", "candidates"}, set(document))
        self.assertEqual(
            {"number", "status", "started_at", "finished_at"}, set(document["run"])
        )
        self.assertEqual("success", document["run"]["status"])
        self.assertEqual("2026-10-21T00:05:12+08:00", document["run"]["started_at"])
        for row in document["candidates"]:
            self.assertEqual(
                {
                    "id",
                    "title",
                    "source",
                    "researcher",
                    "writer",
                    "reviewer",
                    "published",
                },
                set(row),
            )
            self.assertIs(type(row["published"]), bool)
            if row["researcher"]:
                self.assertEqual(
                    {"decision", "reason", "notes"}, set(row["researcher"])
                )
                self.assertIn(row["researcher"]["decision"], {"keep", "skip"})
            if row["writer"]:
                self.assertEqual({"headline", "summary"}, set(row["writer"]))
            if row["reviewer"]:
                self.assertEqual({"verdict", "rounds", "issues"}, set(row["reviewer"]))
                self.assertIn(
                    row["reviewer"]["verdict"], {"approve", "revise", "reject"}
                )
                self.assertGreaterEqual(row["reviewer"]["rounds"], 1)
        self.assertIsNone(document["candidates"][2]["writer"])
        self.assertIsNone(document["candidates"][2]["reviewer"])
        self.assertIsNone(document["candidates"][3]["researcher"])
        self.assert_counts(document)

    def test_status_mapping_including_operational_failure(self) -> None:
        for outcome, expected in [
            ("SUCCESS", "no_items"),
            ("PARTIAL_SUCCESS", "partial_success"),
            ("QUOTA_STOPPED", "quota_stopped"),
            ("FAILED", "partial_success"),
        ]:
            with self.subTest(outcome=outcome):
                self.assertEqual(expected, finish(recorder(), outcome)["run"]["status"])

    def test_approved_but_not_written_is_partial_and_false(self) -> None:
        trace = recorder()
        complete(trace, published=False)
        document = finish(trace)
        self.assertEqual("partial_success", document["run"]["status"])
        self.assertEqual(1, document["counts"]["approved"])
        self.assertEqual(0, document["counts"]["published"])

    def test_final_revision_snapshot_counts_reviews_not_revision_limit(self) -> None:
        trace = recorder()
        complete(trace, verdict="REVISE")
        trace.observe(
            {"id": "c00"}, "writer", {"draft": draft("修改后的标题", revised=True)}
        )
        passed = review("PASS")
        passed["issues"] = [{"severity": "minor", "problem": "已解决的问题"}]
        trace.observe({"id": "c00"}, "reviewer", {"review": passed})
        trace.mark_published({"id": "c00"})
        row = finish(trace)["candidates"][0]
        self.assertEqual("修改后的标题", row["writer"]["headline"])
        self.assertEqual(
            {"verdict": "approve", "rounds": 2, "issues": []}, row["reviewer"]
        )

    def test_text_limits_include_ellipsis_and_list_limits(self) -> None:
        text = "新" * 500
        trace = recorder(title=text)
        notes = research()
        notes.update(reason=text, notes=[{"claim": text}] * 8)
        trace.observe({"id": "c00"}, "researcher", {"research_notes": notes})
        trace.observe(
            {"id": "c00"}, "writer", {"draft": {"headline": text, "summary": text}}
        )
        rejected = review("REJECT")
        rejected["issues"] = [{"problem": text + str(i)} for i in range(8)]
        trace.observe({"id": "c00"}, "reviewer", {"review": rejected})
        row = finish(trace)["candidates"][0]
        for value, maximum in [
            (row["title"], 140),
            (row["researcher"]["reason"], 80),
            (row["researcher"]["notes"][0], 120),
            (row["writer"]["headline"], 80),
            (row["writer"]["summary"], 240),
            (row["reviewer"]["issues"][0], 100),
        ]:
            self.assertEqual(maximum, len(value))
            self.assertTrue(value.endswith("…"))
        self.assertEqual(4, len(row["researcher"]["notes"]))
        self.assertLessEqual(len(row["reviewer"]["issues"]), 4)
        self.assertEqual("😀😀😀…", trace.text("😀" * 8, 4))

    def test_summary_sentences_and_non_chinese_diagnostic_fallback(
        self,
    ) -> None:
        trace = recorder()
        notes = research()
        notes.update(reason="English only", notes=[{"claim": "English only"}])
        trace.observe({"id": "c00"}, "researcher", {"research_notes": notes})
        trace.observe(
            {"id": "c00"},
            "writer",
            {
                "draft": {
                    "headline": "示例",
                    "summary": "第一句。第二句。第三句。第四句。",
                }
            },
        )
        trace.observe(
            {"id": "c00"},
            "reviewer",
            {
                "review": {
                    **review("REJECT"),
                    "issues": [{"problem": "English only"}],
                }
            },
        )
        row = finish(trace)["candidates"][0]
        self.assertEqual("研究员未提供中文原因。", row["researcher"]["reason"])
        self.assertEqual(["研究员未提供中文研究要点。"], row["researcher"]["notes"])
        self.assertEqual("第一句。第二句。第三句。", row["writer"]["summary"])
        self.assertEqual(["审查结果未提供中文问题说明。"], row["reviewer"]["issues"])

    def test_urls_reject_invalid_schemes_malformed_and_credential_urls(self) -> None:
        trace = recorder(redactions=["fictional-sensitive-value"])
        for value in [
            None,
            "javascript:alert(1)",
            "file:///x",
            "/relative",
            "https://",
            "https://[broken",
            "https://host:bad/path",
            "https://host:0/path",
            "https://user:pass@example.com/",
            "https://example.com/?api_key=example",
            "https://example.com/?token=example",
            "https://example.com/#access_token=x",
            "https://example.com/fictional-sensitive-value",
            "https://host/a b",
        ]:
            with self.subTest(value=value):
                self.assertIsNone(trace.url(value))
        self.assertEqual("http://example.com/a", trace.url("http://example.com/a"))
        self.assertEqual(
            "https://example.com/文章", trace.url("https://example.com/文章")
        )

    def test_plain_text_redacts_credentials_and_prompt_echoes_in_all_fields(
        self,
    ) -> None:
        secret = "fictional-sensitive-value"
        instruction = "这是一段只用于测试的内部指令，不应出现在导出记录里。"
        trace = recorder(redactions=[secret], system_instructions=[instruction])
        unsafe = (
            f"<b>正常新闻</b><script>恶意脚本</script> {secret} {instruction}\n"
            "Authorization: Bearer fictional-value\n"
            "x-goog-api-key: fictional-value\n"
            "&lt;img src=x onerror=x&gt;"
        )
        candidate = {**CANDIDATE, "id": "unsafe", "title": unsafe}
        trace.add_candidate(candidate, source_name=unsafe)
        trace.observe(
            candidate,
            "researcher",
            {
                "research_notes": {
                    **research(),
                    "reason": unsafe,
                    "notes": [{"claim": unsafe}],
                    "raw_content": unsafe,
                    "system_instruction": instruction,
                }
            },
        )
        trace.observe(
            candidate,
            "writer",
            {
                "draft": {
                    "headline": unsafe,
                    "summary": unsafe,
                    "request_headers": unsafe,
                }
            },
        )
        trace.observe(
            candidate,
            "reviewer",
            {
                "review": {
                    **review("REJECT"),
                    "issues": [{"problem": unsafe}],
                }
            },
        )
        encoded = json.dumps(finish(trace), ensure_ascii=False)
        for forbidden in [
            secret,
            instruction,
            "Authorization",
            "Bearer",
            "x-goog-api-key",
            "<b>",
            "<img",
            "恶意脚本",
            "system_instruction",
            "raw_content",
            "request_headers",
        ]:
            self.assertNotIn(forbidden, encoded)

    def test_size_cap_preserves_all_40_rows_and_counts_with_worst_case_unicode(
        self,
    ) -> None:
        text = "新😀" * 300
        trace = recorder(50, title=text, url="https://example.com/" + text)
        for index in range(40):
            candidate = {"id": f"c{index:02d}"}
            trace.observe(
                candidate,
                "researcher",
                {
                    "research_notes": {
                        **research(),
                        "reason": text,
                        "notes": [{"claim": text}] * 4,
                    }
                },
            )
            trace.observe(
                candidate,
                "writer",
                {
                    "draft": {
                        "headline": text,
                        "summary": text,
                    }
                },
            )
            trace.observe(
                candidate,
                "reviewer",
                {
                    "review": {
                        **review("REJECT"),
                        "issues": [{"problem": text + str(i)} for i in range(4)],
                    }
                },
            )
        encoded = trace.finish(
            date="2026-10-21", number=1, outcome="SUCCESS", finished_at=NOW
        )
        self.assertLessEqual(len(encoded.encode("utf-8")), MAX_TRACE_BYTES)
        document = json.loads(encoded)
        self.assertEqual(40, len(document["candidates"]))
        self.assertEqual(40, len({r["id"] for r in document["candidates"]}))
        self.assert_counts(document)


class TracePipelineTests(unittest.TestCase):
    def run_local(self, root: Path, responses: list, *, count: int = 1) -> dict:
        settings = make_settings(max_candidate_age_hours=0)
        client = CaptureClient(*responses)
        graph = build_editorial_graph(
            researcher=ResearcherNode(
                client, settings.researcher, lambda url: "示例正文。" * 60
            ),
            writer=WriterNode(client, settings.writer),
            reviewer=ReviewerNode(client, settings.reviewer),
            publisher=PublisherNode(),
            max_revisions=1,
        )
        candidates = [
            {**CANDIDATE, "id": f"c{i}", "url": f"https://example.com/story-{i}"}
            for i in range(count)
        ]
        site = SitePublisher(
            LocalSiteStore(root),
            run=RunContext(event="schedule"),
            now=lambda: NOW,
            started_at=NOW,
            logger=None,
        )
        return run_daily(
            settings, graph=graph, candidates=candidates, site_publisher=site
        )

    def read_trace(self, root: Path) -> dict:
        return json.loads(
            (root / "data/news/2026-10-21.trace.json").read_text(encoding="utf-8")
        )

    def test_local_sample_exercises_pass_revision_skip_reject_and_hold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = render(root, now=NOW)
            trace = self.read_trace(root)
            self.assertEqual("SUCCESS", result["run_outcome"])
            self.assertEqual(
                {"fetched": 5, "kept": 4, "drafted": 4, "approved": 2, "published": 2},
                trace["counts"],
            )
            rows = trace["candidates"]
            self.assertEqual(2, rows[1]["reviewer"]["rounds"])
            self.assertIsNone(rows[2]["writer"])
            self.assertEqual("reject", rows[3]["reviewer"]["verdict"])
            self.assertEqual("revise", rows[4]["reviewer"]["verdict"])
            self.assertEqual(2, rows[4]["reviewer"]["rounds"])
            self.assertFalse(rows[4]["published"])

    def test_all_skipped_rejected_or_held_still_writes_trace_without_news(self) -> None:
        for responses in [
            [research("SKIP")],
            [research(), draft("示例"), review("REJECT")],
            [
                research(),
                draft("示例"),
                review("REVISE"),
                draft("修改"),
                review("REVISE"),
            ],
        ]:
            with (
                self.subTest(responses=len(responses)),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                result = self.run_local(root, responses)
                self.assertEqual("SUCCESS", result["run_outcome"])
                self.assertEqual("no_items", self.read_trace(root)["run"]["status"])
                self.assertEqual(
                    ["2026-10-21.trace.json", "stats.json"],
                    sorted(p.name for p in (root / "data/news").iterdir()),
                )

    def test_quota_stop_preserves_completed_nodes_and_deferred_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.run_local(
                root, [research(), QuotaStopped("offline", "daily-limit")], count=2
            )
            trace = self.read_trace(root)
            self.assertEqual("quota_stopped", trace["run"]["status"])
            self.assertEqual("keep", trace["candidates"][0]["researcher"]["decision"])
            self.assertIsNone(trace["candidates"][0]["writer"])
            self.assertIsNone(trace["candidates"][1]["researcher"])

    def test_reviewer_failure_preserves_writer_and_failure_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_local(
                root, [research(), draft("保留草稿"), RuntimeError("offline")]
            )
            trace = self.read_trace(root)
            self.assertEqual(1, result["exit_code"])
            self.assertEqual("FAILED", result["run_outcome"])
            self.assertEqual("partial_success", trace["run"]["status"])
            self.assertEqual("保留草稿", trace["candidates"][0]["writer"]["headline"])
            self.assertIsNone(trace["candidates"][0]["reviewer"])

    def test_rerun_overwrites_trace_while_existing_day_and_stats_contracts_survive(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.run_local(root, [research(), draft("首次发布"), review("PASS")])
            news = root / "data/news/2026-10-21.json"
            original = news.read_bytes()
            self.run_local(root, [research("SKIP")])
            trace = self.read_trace(root)
            self.assertEqual("no_items", trace["run"]["status"])
            self.assertEqual(0, trace["counts"]["published"])
            self.assertEqual(original, news.read_bytes())
            stats = json.loads(
                (root / "data/news/stats.json").read_text(encoding="utf-8")
            )
            self.assertEqual(trace["run"]["number"], stats["total_runs"])
            self.assertEqual(
                {"total_runs", "success_rate", "since", "updated_at"}, set(stats)
            )

    def test_sources_and_quota_initialization_failures_write_empty_trace(self) -> None:
        for target, error in [
            ("daily_chip_news.runner.create_runtime_graph", QuotaStateError("offline")),
            (
                "daily_chip_news.runner.collect_articles",
                SourceCollectionError(SourceCollectionResult([], 1, 0, 1, [])),
            ),
        ]:
            with (
                self.subTest(target=target),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                site = SitePublisher(
                    LocalSiteStore(root), now=lambda: NOW, started_at=NOW, logger=None
                )
                with patch(target, side_effect=error):
                    result = run_daily(
                        make_settings(),
                        site_publisher=site,
                        graph=None if "create_runtime_graph" in target else object(),
                    )
                self.assertEqual(1, result["exit_code"])
                self.assertEqual([], self.read_trace(root)["candidates"])


class TraceGitHubTests(unittest.TestCase):
    def test_trace_news_and_stats_share_one_commit_and_counter(self) -> None:
        github = ScriptedGitHub()
        trace = recorder()
        complete(trace)
        site = github_publisher(github)
        result = site.publish(
            [
                {
                    "title": "示例",
                    "summary": "示例摘要。",
                    "source": {"name": "示例来源", "url": CANDIDATE["url"]},
                }
            ],
            "SUCCESS",
            trace=trace,
        )
        files = github.tree_files()
        self.assertEqual(1, len(github.commits))
        self.assertEqual(
            {
                "data/news/2026-10-20.json",
                "data/news/stats.json",
                "data/news/2026-10-20.trace.json",
            },
            set(files),
        )
        document = files["data/news/2026-10-20.trace.json"]
        self.assertEqual(result.stats["total_runs"], document["run"]["number"])

    def test_empty_run_also_commits_trace_and_stats_together(self) -> None:
        github = ScriptedGitHub()
        github_publisher(github).publish([], "SUCCESS", trace=recorder())
        self.assertEqual(
            {"data/news/stats.json", "data/news/2026-10-20.trace.json"},
            set(github.tree_files()),
        )
        self.assertEqual(1, len(github.commits))

    def test_conflict_recomputes_trace_counter_against_new_branch_head(self) -> None:
        github = ScriptedGitHub(ref_responses=[FakeResponse(422, {})])
        original = github.handle
        reads = 0

        def concurrent_writer(url: str, kwargs: dict):
            nonlocal reads
            if url.endswith("/git/ref/heads/main"):
                reads += 1
                if reads == 2:
                    github.files["data/news/stats.json"] = json.dumps(
                        {
                            "total_runs": 99,
                            "success_rate": 1.0,
                            "since": "2026-01",
                            "updated_at": "2026-10-19T01:00:00+08:00",
                        }
                    )
            return original(url, kwargs)

        github.session.handler = concurrent_writer
        github_publisher(github).publish([], "SUCCESS", trace=recorder())
        files = github.tree_files()
        self.assertEqual(2, len(github.commits))
        self.assertEqual(100, files["data/news/stats.json"]["total_runs"])
        self.assertEqual(100, files["data/news/2026-10-20.trace.json"]["run"]["number"])


if __name__ == "__main__":
    unittest.main()
