from __future__ import annotations

import unittest

from _support import ARTICLE, DRAFT, RESEARCH, SRC, review  # noqa: F401
from daily_chip_news.gemini import GeminiAPIError
from daily_chip_news.graph import build_editorial_graph
from daily_chip_news.publisher import PublisherError


def initial_state():
    return {
        "article": ARTICLE,
        "revision_brief": [],
        "revision_count": 0,
        "status": "NEW",
        "published": False,
    }


class FailureTests(unittest.TestCase):
    def test_gemini_error_propagates_instead_of_becoming_skip(self) -> None:
        def researcher(state):
            raise GeminiAPIError("authentication failed")

        graph = build_editorial_graph(
            researcher=researcher,
            writer=lambda state: {"draft": DRAFT},
            reviewer=lambda state: {"review": review("PASS")},
            publisher=lambda state: {"published": True},
            max_revisions=2,
        )
        with self.assertRaises(GeminiAPIError):
            graph.invoke(initial_state())

    def test_publisher_failure_fails_the_graph(self) -> None:
        def publisher(state):
            raise PublisherError("delivery failed")

        graph = build_editorial_graph(
            researcher=lambda state: {"research_notes": RESEARCH},
            writer=lambda state: {"draft": DRAFT},
            reviewer=lambda state: {"review": review("PASS")},
            publisher=publisher,
            max_revisions=2,
        )
        with self.assertRaises(PublisherError):
            graph.invoke(initial_state())


if __name__ == "__main__":
    unittest.main()
