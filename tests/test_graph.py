from __future__ import annotations

import unittest

from _support import ARTICLE, DRAFT, RESEARCH, SRC, review  # noqa: F401
from daily_chip_news.graph import build_editorial_graph


class GraphTests(unittest.TestCase):
    def make_graph(self, decisions: list[str], *, keep: bool = True, max_revisions: int = 2):
        calls = {"researcher": 0, "writer": 0, "reviewer": 0, "publisher": 0}

        def researcher(state):
            calls["researcher"] += 1
            if keep:
                return {"research_notes": dict(RESEARCH)}
            skipped = dict(RESEARCH)
            skipped.update({"decision": "SKIP", "notes": [], "topic": ""})
            return {"research_notes": skipped}

        def writer(state):
            calls["writer"] += 1
            return {"draft": dict(DRAFT)}

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
        return graph.invoke(
            {
                "article": ARTICLE,
                "revision_brief": [],
                "revision_count": 0,
                "status": "NEW",
                "published": False,
            }
        )

    def test_researcher_skip_does_not_call_writer(self) -> None:
        graph, calls = self.make_graph([], keep=False)
        result = self.invoke(graph)
        self.assertEqual("SKIP", result["status"])
        self.assertEqual(0, calls["writer"])
        self.assertEqual(0, calls["publisher"])

    def test_keep_pass_publishes(self) -> None:
        graph, calls = self.make_graph(["PASS"])
        result = self.invoke(graph)
        self.assertEqual("PASS", result["status"])
        self.assertTrue(result["published"])
        self.assertEqual(1, calls["writer"])
        self.assertEqual(1, calls["reviewer"])
        self.assertEqual(1, calls["publisher"])

    def test_reject_routes_back_to_writer(self) -> None:
        graph, calls = self.make_graph(["REJECT", "PASS"])
        result = self.invoke(graph)
        self.assertEqual("PASS", result["status"])
        self.assertEqual(1, result["revision_count"])
        self.assertEqual(2, calls["writer"])
        self.assertEqual(1, calls["publisher"])

    def test_two_revisions_can_pass(self) -> None:
        graph, calls = self.make_graph(["REJECT", "REJECT", "PASS"])
        result = self.invoke(graph)
        self.assertEqual("PASS", result["status"])
        self.assertEqual(2, result["revision_count"])
        self.assertEqual(3, calls["writer"])
        self.assertEqual(1, calls["publisher"])

    def test_reject_after_two_revisions_holds_without_publish(self) -> None:
        graph, calls = self.make_graph(["REJECT", "REJECT", "REJECT"])
        result = self.invoke(graph)
        self.assertEqual("HOLD", result["status"])
        self.assertEqual(2, result["revision_count"])
        self.assertFalse(result["published"])
        self.assertEqual(3, calls["writer"])
        self.assertEqual(3, calls["reviewer"])
        self.assertEqual(0, calls["publisher"])


if __name__ == "__main__":
    unittest.main()
