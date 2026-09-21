from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401
from daily_chip_news.gemini import GeminiAPIError
from daily_chip_news.graph import NodeExecutionError, build_editorial_graph
from daily_chip_news.publisher import PublisherError


def initial_state():
    from _support import ARTICLE

    return {
        "article": ARTICLE,
        "revision_brief": [],
        "revision_count": 0,
        "status": "NEW",
        "published": False,
    }


class FailureTests(unittest.TestCase):
    def test_gemini_error_propagates_instead_of_becoming_skip(self) -> None:
        def writer(state):
            raise GeminiAPIError("authentication failed")

        graph = build_editorial_graph(
            writer=writer,
            reviewer=lambda state: {"review": {}},
            publisher=lambda state: {"published": True},
            max_revisions=1,
        )
        with self.assertRaises(NodeExecutionError) as context:
            graph.invoke(initial_state())
        self.assertEqual("writer", context.exception.stage)
        self.assertIsInstance(context.exception.cause, GeminiAPIError)

    def test_publisher_failure_fails_the_graph(self) -> None:
        from _support import DRAFT, RESEARCH, review

        def publisher(state):
            raise PublisherError("delivery failed")

        graph = build_editorial_graph(
            writer=lambda state: {
                "research_notes": RESEARCH,
                "draft": DRAFT,
            },
            reviewer=lambda state: {"review": review("PASS")},
            publisher=publisher,
            max_revisions=1,
        )
        with self.assertRaises(NodeExecutionError) as context:
            graph.invoke(initial_state())
        self.assertEqual("publisher", context.exception.stage)
        self.assertIsInstance(context.exception.cause, PublisherError)


if __name__ == "__main__":
    unittest.main()
