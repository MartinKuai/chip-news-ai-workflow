from __future__ import annotations

import unittest

from _support import DRAFT, RESEARCH, SRC, review  # noqa: F401

from daily_chip_news.graph import (
    STATUS_DRAFTED,
    STATUS_HOLD,
    STATUS_PASS,
    STATUS_PUBLISHED,
    STATUS_REJECT,
    STATUS_RESEARCHED,
    STATUS_REVISE,
    STATUS_SKIP,
    NodeExecutionError,
    build_editorial_graph,
)


class FakeNode:
    """Replay queued updates; a single update is reused on every call."""

    def __init__(self, *updates: dict) -> None:
        self.updates = list(updates)
        self.states: list[dict] = []

    def __call__(self, state: dict) -> dict:
        self.states.append(dict(state))
        if len(self.updates) > 1:
            return self.updates.pop(0)
        return self.updates[0]


def researcher_keep() -> dict:
    return {"research_notes": dict(RESEARCH), "status": STATUS_RESEARCHED}


def researcher_skip() -> dict:
    return {
        "research_notes": {
            "decision": "SKIP",
            "reason": "off scope",
            "topic": "",
            "notes": [],
        },
        "status": STATUS_SKIP,
    }


def writer_update() -> dict:
    return {"draft": dict(DRAFT), "status": STATUS_DRAFTED}


def review_update(status: str) -> dict:
    return {"review": review(status)}


def build(
    *,
    research: dict | None = None,
    drafts: tuple[dict, ...] = (),
    reviews: tuple[dict, ...] = (),
    publisher: FakeNode | None = None,
    max_revisions: int = 1,
    on_step=None,
):
    researcher = FakeNode(research or researcher_keep())
    writer = FakeNode(*(drafts or (writer_update(),)))
    reviewer = FakeNode(*(reviews or (review_update("PASS"),)))
    publisher = publisher or FakeNode({"published": True, "status": STATUS_PASS})
    graph = build_editorial_graph(
        researcher=researcher,
        writer=writer,
        reviewer=reviewer,
        publisher=publisher,
        max_revisions=max_revisions,
        on_step=on_step,
    )
    return graph, researcher, writer, reviewer, publisher


def invoke(graph, **overrides):
    state = {
        "revision_brief": [],
        "revision_count": 0,
        "status": "NEW",
        "published": False,
    }
    state.update(overrides)
    return graph.invoke(state)


class PublishPathTests(unittest.TestCase):
    def test_normal_publish_path(self) -> None:
        steps: list[tuple[str, str]] = []
        graph, researcher, writer, reviewer, publisher = build(
            on_step=lambda stage, status: steps.append((stage, status))
        )
        result = invoke(graph)
        self.assertEqual(STATUS_PUBLISHED, result["status"])
        self.assertTrue(result["published"])
        self.assertEqual(
            [
                ("researcher", STATUS_RESEARCHED),
                ("writer", STATUS_DRAFTED),
                ("reviewer", STATUS_PASS),
                ("publisher", STATUS_PUBLISHED),
            ],
            steps,
        )
        self.assertEqual(1, len(researcher.states))
        self.assertEqual(1, len(writer.states))
        self.assertEqual(1, len(reviewer.states))
        self.assertEqual(1, len(publisher.states))

    def test_publisher_receives_the_pass_state(self) -> None:
        graph, _, _, _, publisher = build()
        invoke(graph)
        state = publisher.states[0]
        self.assertEqual(STATUS_PASS, state["status"])
        self.assertEqual("PASS", state["review"]["status"])


class SkipAndRejectTests(unittest.TestCase):
    def test_researcher_skip_stops_before_writing(self) -> None:
        graph, _, writer, reviewer, publisher = build(research=researcher_skip())
        result = invoke(graph)
        self.assertEqual(STATUS_SKIP, result["status"])
        self.assertFalse(result["published"])
        self.assertEqual([], writer.states)
        self.assertEqual([], reviewer.states)
        self.assertEqual([], publisher.states)

    def test_reviewer_reject_is_terminal(self) -> None:
        steps: list[tuple[str, str]] = []
        graph, _, writer, _, publisher = build(
            reviews=(review_update("REJECT"),),
            on_step=lambda stage, status: steps.append((stage, status)),
        )
        result = invoke(graph)
        self.assertEqual(STATUS_REJECT, result["status"])
        self.assertFalse(result["published"])
        self.assertEqual(1, len(writer.states))
        self.assertEqual([], publisher.states)
        self.assertEqual(
            [
                ("researcher", STATUS_RESEARCHED),
                ("writer", STATUS_DRAFTED),
                ("reviewer", STATUS_REJECT),
            ],
            steps,
        )


class RevisionLoopTests(unittest.TestCase):
    def test_revise_then_pass_republishes_the_revised_draft(self) -> None:
        steps: list[tuple[str, str]] = []
        graph, _, writer, reviewer, _ = build(
            reviews=(review_update("REVISE"), review_update("PASS")),
            on_step=lambda stage, status: steps.append((stage, status)),
        )
        result = invoke(graph)
        self.assertEqual(STATUS_PUBLISHED, result["status"])
        self.assertEqual(1, result["revision_count"])
        self.assertEqual(2, len(writer.states))
        self.assertEqual(2, len(reviewer.states))
        revised_state = writer.states[1]
        self.assertEqual(DRAFT, revised_state["draft"])
        self.assertEqual(["删除缺乏依据的表述"], revised_state["revision_brief"])
        self.assertIn(("reviewer", STATUS_REVISE), steps)

    def test_revision_limit_holds_the_item(self) -> None:
        graph, _, writer, reviewer, publisher = build(
            reviews=(review_update("REVISE"),), max_revisions=1
        )
        result = invoke(graph)
        self.assertEqual(STATUS_HOLD, result["status"])
        self.assertFalse(result["published"])
        self.assertEqual(1, result["revision_count"])
        self.assertEqual(2, len(writer.states))
        self.assertEqual(2, len(reviewer.states))
        self.assertEqual([], publisher.states)

    def test_zero_revisions_holds_immediately(self) -> None:
        graph, _, writer, _, publisher = build(
            reviews=(review_update("REVISE"),), max_revisions=0
        )
        result = invoke(graph)
        self.assertEqual(STATUS_HOLD, result["status"])
        self.assertEqual(1, len(writer.states))
        self.assertEqual([], publisher.states)

    def test_negative_revision_limit_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build(max_revisions=-1)


class NodeFailureTests(unittest.TestCase):
    def test_researcher_schema_violation_is_a_node_failure(self) -> None:
        graph, _, writer, _, _ = build(research={"research_notes": "not a mapping"})
        with self.assertRaises(NodeExecutionError) as context:
            invoke(graph)
        self.assertEqual("researcher", context.exception.stage)
        self.assertEqual([], writer.states)

    def test_writer_empty_draft_is_a_node_failure(self) -> None:
        graph, _, _, _, _ = build(drafts=({"draft": {}},))
        with self.assertRaises(NodeExecutionError) as context:
            invoke(graph)
        self.assertEqual("writer", context.exception.stage)

    def test_reviewer_unknown_status_is_a_node_failure(self) -> None:
        graph, _, _, _, _ = build(reviews=({"review": {"status": "MAYBE"}},))
        with self.assertRaises(NodeExecutionError) as context:
            invoke(graph)
        self.assertEqual("reviewer", context.exception.stage)

    def test_publisher_failure_keeps_the_stage(self) -> None:
        def broken_publisher(state: dict) -> dict:
            raise RuntimeError("telegram down")

        graph = build_editorial_graph(
            researcher=FakeNode(researcher_keep()),
            writer=FakeNode(writer_update()),
            reviewer=FakeNode(review_update("PASS")),
            publisher=broken_publisher,
            max_revisions=1,
        )
        with self.assertRaises(NodeExecutionError) as context:
            invoke(graph)
        self.assertEqual("publisher", context.exception.stage)

    def test_on_step_is_not_called_for_a_failed_node(self) -> None:
        steps: list[tuple[str, str]] = []
        graph, _, _, _, _ = build(
            reviews=({"review": {"status": "MAYBE"}},),
            on_step=lambda stage, status: steps.append((stage, status)),
        )
        with self.assertRaises(NodeExecutionError):
            invoke(graph)
        self.assertEqual(
            [("researcher", STATUS_RESEARCHED), ("writer", STATUS_DRAFTED)], steps
        )


if __name__ == "__main__":
    unittest.main()
