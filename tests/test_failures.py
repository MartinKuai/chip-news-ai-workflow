from __future__ import annotations

import unittest

from _support import ARTICLE, DRAFT, RESEARCH, SRC, review  # noqa: F401
from daily_chip_news.gemini import GeminiAPIError
from daily_chip_news.graph import NodeExecutionError, build_editorial_graph
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
    def build(self, *, researcher=None, writer=None, publisher=None):
        return build_editorial_graph(
            researcher=researcher
            or (lambda state: {"research_notes": dict(RESEARCH)}),
            writer=writer or (lambda state: {"draft": dict(DRAFT)}),
            reviewer=lambda state: {"review": review("PASS")},
            publisher=publisher or (lambda state: {"published": True}),
            max_revisions=1,
        )

    def test_researcher_gemini_error_propagates_instead_of_becoming_skip(self) -> None:
        def researcher(state):
            raise GeminiAPIError("authentication failed")

        graph = self.build(researcher=researcher)
        with self.assertRaises(NodeExecutionError) as context:
            graph.invoke(initial_state())
        self.assertEqual("researcher", context.exception.stage)
        self.assertIsInstance(context.exception.cause, GeminiAPIError)

    def test_writer_gemini_error_propagates(self) -> None:
        def writer(state):
            raise GeminiAPIError("HTTP 503", status_code=503, transient=True)

        graph = self.build(writer=writer)
        with self.assertRaises(NodeExecutionError) as context:
            graph.invoke(initial_state())
        self.assertEqual("writer", context.exception.stage)
        self.assertIsInstance(context.exception.cause, GeminiAPIError)

    def test_publisher_failure_fails_the_graph(self) -> None:
        def publisher(state):
            raise PublisherError("delivery failed")

        graph = self.build(publisher=publisher)
        with self.assertRaises(NodeExecutionError) as context:
            graph.invoke(initial_state())
        self.assertEqual("publisher", context.exception.stage)
        self.assertIsInstance(context.exception.cause, PublisherError)


if __name__ == "__main__":
    unittest.main()
