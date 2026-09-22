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
        calls = {"researcher": 0, "writer": 0, "reviewer": 0, "publisher": 0}

        def researcher(state):
            calls["researcher"] += 1
            if keep:
                return {
                    "research_notes": dict(RESEARCH),
                    "status": "RESEARCHED",
                }
            skipped = dict(RESEARCH)
            skipped.update({"decision": "SKIP", "notes": [], "topic": ""})
            return {"research_notes": skipped, "status": "SKIP"}

        def writer(state):
            calls["writer"] += 1
            return {"draft": dict(DRAFT), "status": "DRAFTED"}

        scripted = list(decisions)

        def reviewer(state):
            calls["reviewer"] += 1
            return {"review": review(scripted.pop(0))}

        def publisher(state):
            calls["publisher"] += 1
            return {"published": True}

        graph = build_editorial_graph(
            researcher=researcher,
            writer=writer,
            reviewer=reviewer,
            publisher=publisher,
            max_revisions=max_revisions,
        )
        return graph, calls

    @staticmethod
    def invoke(graph):
        return graph.invoke(initial_state())

    def test_researcher_skip_does_not_call_writer_or_reviewer(self) -> None:
        graph, calls = self.make_graph([], keep=False)
        result = self.invoke(graph)
        self.assertEqual("SKIP", result["status"])
        self.assertEqual(1, calls["researcher"])
        self.assertEqual(0, calls["writer"])
        self.assertEqual(0, calls["reviewer"])
        self.assertEqual(0, calls["publisher"])

    def test_keep_pass_publishes(self) -> None:
        graph, calls = self.make_graph(["PASS"])
        result = self.invoke(graph)
        self.assertEqual("PASS", result["status"])
        self.assertTrue(result["published"])
        self.assertEqual(1, calls["researcher"])
        self.assertEqual(1, calls["writer"])
        self.assertEqual(1, calls["reviewer"])
        self.assertEqual(1, calls["publisher"])

    def test_reject_routes_back_to_writer_without_researching_again(self) -> None:
        graph, calls = self.make_graph(["REJECT", "PASS"], max_revisions=1)
        result = self.invoke(graph)
        self.assertEqual("PASS", result["status"])
        self.assertEqual(1, result["revision_count"])
        self.assertEqual(1, calls["researcher"])
        self.assertEqual(2, calls["writer"])
        self.assertEqual(2, calls["reviewer"])
        self.assertEqual(1, calls["publisher"])

    def test_reject_after_revision_limit_holds_without_publish(self) -> None:
        graph, calls = self.make_graph(["REJECT", "REJECT"], max_revisions=1)
        result = self.invoke(graph)
        self.assertEqual("HOLD", result["status"])
        self.assertEqual(1, result["revision_count"])
        self.assertFalse(result["published"])
        self.assertEqual(1, calls["researcher"])
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
        self.assertEqual(1, calls["researcher"])

    def test_no_infinite_writer_reviewer_loop(self) -> None:
        graph, calls = self.make_graph(["REJECT"] * 6, max_revisions=2)
        result = self.invoke(graph)
        self.assertEqual("HOLD", result["status"])
        self.assertEqual(3, calls["writer"])
        self.assertEqual(3, calls["reviewer"])
        self.assertEqual(1, calls["researcher"])

    def test_writer_without_draft_fails_the_article(self) -> None:
        graph = build_editorial_graph(
            researcher=lambda state: {
                "research_notes": dict(RESEARCH),
                "status": "RESEARCHED",
            },
            writer=lambda state: {"draft": {}},
            reviewer=lambda state: {"review": review("PASS")},
            publisher=lambda state: {"published": True},
            max_revisions=1,
        )
        with self.assertRaises(NodeExecutionError) as context:
            self.invoke(graph)
        self.assertEqual("writer", context.exception.stage)
        self.assertIsInstance(context.exception.cause, SchemaError)

    def test_invalid_researcher_decision_fails_the_article(self) -> None:
        graph = build_editorial_graph(
            researcher=lambda state: {
                "research_notes": dict(RESEARCH, decision="MAYBE"),
            },
            writer=lambda state: {"draft": dict(DRAFT)},
            reviewer=lambda state: {"review": review("PASS")},
            publisher=lambda state: {"published": True},
            max_revisions=1,
        )
        with self.assertRaises(NodeExecutionError) as context:
            self.invoke(graph)
        self.assertEqual("researcher", context.exception.stage)


if __name__ == "__main__":
    unittest.main()
