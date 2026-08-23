from __future__ import annotations

import json
import unittest

from _support import ARTICLE, DRAFT, RESEARCH, SRC, CaptureClient, review  # noqa: F401
from daily_chip_news.nodes import ResearcherNode, ReviewerNode, WriterNode
from daily_chip_news.schemas import SchemaError


class ContextIsolationTests(unittest.TestCase):
    def test_writer_receives_only_allow_listed_context(self) -> None:
        client = CaptureClient(DRAFT)
        writer = WriterNode(client, "writer-model")
        writer(
            {
                "article": ARTICLE,
                "research_notes": RESEARCH,
                "revision_brief": ["缩短标题"],
                "raw_article": "must not leak",
                "raw_content": "must not leak",
                "researcher_prompt": "must not leak",
            }
        )
        payload = client.calls[0]["payload"]
        self.assertEqual(
            {
                "editorial_brief",
                "research_notes",
                "revision_brief",
                "output_schema",
            },
            set(payload),
        )
        serialized = json.dumps(payload)
        for forbidden in ("raw_article", "raw_content", "researcher_prompt"):
            self.assertNotIn(forbidden, serialized)

    def test_three_nodes_route_to_three_models(self) -> None:
        client = CaptureClient(RESEARCH, DRAFT, review("PASS"))
        research_update = ResearcherNode(
            client, "research-model", lambda url: "x" * 300
        )({"article": ARTICLE})
        draft_update = WriterNode(client, "writer-model")(
            {"article": ARTICLE, **research_update, "revision_brief": []}
        )
        ReviewerNode(client, "review-model")(
            {"article": ARTICLE, **research_update, **draft_update}
        )
        self.assertEqual(
            ["research-model", "writer-model", "review-model"],
            [call["model"] for call in client.calls],
        )

    def test_reviewer_cannot_pass_below_qa_threshold(self) -> None:
        weak_pass = review("PASS")
        weak_pass["scores"]["factuality"] = 7
        client = CaptureClient(weak_pass)
        with self.assertRaises(SchemaError):
            ReviewerNode(client, "review-model")(
                {"article": ARTICLE, "research_notes": RESEARCH, "draft": DRAFT}
            )


if __name__ == "__main__":
    unittest.main()
