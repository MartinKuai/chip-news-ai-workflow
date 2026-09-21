from __future__ import annotations

import unittest

from _support import ARTICLE, DRAFT, RESEARCH, SRC, review  # noqa: F401
from daily_chip_news.graph import NodeExecutionError, build_editorial_graph
from daily_chip_news.schemas import SchemaError


def initial_state():
    return {
        "article": ARTICLE,
        "revision_brief": [],
        "revision_count": 0,
        "status": "NEW",
        "published": False,
    }


class GraphTests(unittest.TestCase):
    def make_graph(
        self,
        decisions: list[str],
        *,
        keep: bool = True,
        max_revisions: int = 1,
    ):
        calls = {"writer": 0, "reviewer": 0, "publisher": 0}

        def writer(state):
            calls["writer"] += 1
            if keep:
                return {
                    "research_notes": dict(RESEARCH),
                    "draft": dict(DRAFT),
                    "status": "DRAFTED",
                }
            skipped = dict(RESEARCH)
            skipped.update({"decision": "SKIP", "notes": [], "topic": ""})
            return {"research_notes": skipped, "draft": {}, "status": "SKIP"}

        scripted = list(decisions)

        def reviewer(state):
            calls["reviewer"] += 1
            return {"review": review(scripted.pop(0))}

        def publisher(state):
            calls["publisher"] += 1
            return {"published": True}

        graph = build_editorial_graph(
            writer=writer,
            reviewer=reviewer,
            publisher=publisher,
            max_revisions=max_revisions,
        )
        return graph, calls

    @staticmethod
    def invoke(graph):
        return graph.invoke(initial_state())

    def test_writer_skip_does_not_call_reviewer(self) -> None:
        graph, calls = self.make_graph([], keep=False)
        result = self.invoke(graph)
        self.assertEqual("SKIP", result["status"])
        self.assertEqual(0, calls["reviewer"])
        self.assertEqual(0, calls["publisher"])

    def test_keep_pass_publishes(self) -> None:
        graph, calls = self.make_graph(["PASS"])
        result = self.invoke(graph)
        self.assertEqual("PASS", result["status"])
        self.assertTrue(result["published"])
        self.assertEqual(1, calls["writer"])
        self.assertEqual(1, calls["reviewer"])
        self.assertEqual(1, calls["publisher"])

    def test_reject_routes_back_to_writer_once(self) -> None:
        graph, calls = self.make_graph(["REJECT", "PASS"], max_revisions=1)
        result = self.invoke(graph)
        self.assertEqual("PASS", result["status"])
        self.assertEqual(1, result["revision_count"])
        self.assertEqual(2, calls["writer"])
        self.assertEqual(2, calls["reviewer"])
        self.assertEqual(1, calls["publisher"])

    def test_reject_after_revision_limit_holds_without_publish(self) -> None:
        graph, calls = self.make_graph(["REJECT", "REJECT"], max_revisions=1)
        result = self.invoke(graph)
        self.assertEqual("HOLD", result["status"])
        self.assertEqual(1, result["revision_count"])
        self.assertFalse(result["published"])
        self.assertEqual(2, calls["writer"])
        self.assertEqual(2, calls["reviewer"])
        self.assertEqual(0, calls["publisher"])

    def test_two_revisions_can_pass_when_explicitly_allowed(self) -> None:
        graph, calls = self.make_graph(
            ["REJECT", "REJECT", "PASS"], max_revisions=2
        )
        result = self.invoke(graph)
        self.assertEqual("PASS", result["status"])
        self.assertEqual(2, result["revision_count"])
        self.assertEqual(3, calls["writer"])

    def test_no_infinite_writer_reviewer_loop(self) -> None:
        graph, calls = self.make_graph(["REJECT"] * 6, max_revisions=2)
        result = self.invoke(graph)
        self.assertEqual("HOLD", result["status"])
        self.assertEqual(3, calls["writer"])
        self.assertEqual(3, calls["reviewer"])

    def test_writer_keep_without_draft_fails_the_article(self) -> None:
        def writer(state):
            return {"research_notes": dict(RESEARCH), "draft": {}}

        graph = build_editorial_graph(
            writer=writer,
            reviewer=lambda state: {"review": review("PASS")},
            publisher=lambda state: {"published": True},
            max_revisions=1,
        )
        with self.assertRaises(NodeExecutionError) as context:
            self.invoke(graph)
        self.assertEqual("writer", context.exception.stage)
        self.assertIsInstance(context.exception.cause, SchemaError)


if __name__ == "__main__":
    unittest.main()
