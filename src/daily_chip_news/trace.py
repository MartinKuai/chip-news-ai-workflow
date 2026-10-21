"""Bounded, allowlisted snapshots for the website's fixed replay contract.

This is an editorial snapshot, not an HTTP/model debug log. Only validated
node outputs enter it; prompts, article bodies, metadata and errors never do.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit
from zoneinfo import ZoneInfo

TRACE_TZ = ZoneInfo("Asia/Shanghai")
MAX_TRACE_BYTES = 60_000  # Conservative interpretation of 60 KB, including LF.
MAX_CANDIDATES = 40
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")
_CREDENTIAL = re.compile(
    r"(?i)\b(?:authorization|proxy-authorization|cookie|set-cookie|"
    r"x-api-key|x-goog-api-key|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|password|accept|accept-language|content-type|"
    r"user-agent|referer|origin|host|connection)\s*[:=]\s*[^\r\n]+"
    r"|\bbearer\s+[a-z0-9._~+/-]+=*"
    r"|\b(?:gh[pousr]_[a-z0-9_]{10,}|github_pat_[a-z0-9_]+|sk-[a-z0-9_-]{16,})"
    r"|\beyJ[a-z0-9_-]+\.[a-z0-9_-]+\.[a-z0-9_-]+"
)
_SENSITIVE_QUERY = re.compile(
    r"(?i)(?:token|secret|password|authorization|credential|signature|api.?key|^key$)"
)
_SENTENCE_END = re.compile(r"(?:[。！？!?]|(?<!\d)\.(?=\s|$))+[”’\"'）)\]」』]*")


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class TraceRecorder:
    """Collect successful stages, retaining progress when a later stage fails."""

    def __init__(
        self,
        *,
        started_at: datetime | None = None,
        redactions: Iterable[str] = (),
        system_instructions: Iterable[str] = (),
    ) -> None:
        self.started_at = started_at or datetime.now(TRACE_TZ)
        # Also suppress individually echoed prompt lines. Never serialize these.
        forbidden = set(redactions)
        for instruction in system_instructions:
            forbidden.add(instruction)
            forbidden.update(
                line.strip()
                for line in instruction.splitlines()
                if len(line.strip()) >= 12
            )
        self._forbidden = sorted(filter(None, forbidden), key=len, reverse=True)
        self._rows: dict[str, dict[str, Any]] = {}

    def text(self, value: Any, limit: int) -> str:
        raw = value if isinstance(value, str) else ""
        for forbidden in self._forbidden:
            raw = raw.replace(forbidden, "[已隐藏]")
        raw = _CREDENTIAL.sub("[已隐藏]", raw)
        # Decode before parsing so encoded tags do not survive as markup.
        parser = _PlainText()
        parser.feed(unescape(raw))
        parser.close()
        plain = " ".join(_CONTROL.sub(" ", " ".join(parser.parts)).split())
        # Entity nesting must not leave a tag in the final plain-text field.
        plain = re.sub(r"<[^>]*>", "", unescape(plain))
        for forbidden in self._forbidden:
            plain = plain.replace(" ".join(forbidden.split()), "[已隐藏]")
        plain = _CREDENTIAL.sub("[已隐藏]", plain)
        return _clip(plain, limit)

    def _chinese(self, value: Any, limit: int, fallback: str) -> str:
        plain = self.text(value, limit)
        return plain if re.search(r"[\u3400-\u9fff]", plain) else fallback

    def _summary(self, value: Any) -> str:
        plain = self.text(value, 240)
        for count, match in enumerate(_SENTENCE_END.finditer(plain), start=1):
            if count == 3:
                return plain[: match.end()].strip()
        return plain

    def url(self, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        url = value.strip()
        decoded = unquote(unescape(url))
        if (
            not url
            or any(c.isspace() for c in url)
            or _CONTROL.search(url)
            or any(c in url for c in '<>"')
            or any(secret in decoded for secret in self._forbidden)
            or _CREDENTIAL.search(decoded)
        ):
            return None
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.port == 0
                or any(_SENSITIVE_QUERY.search(k) for k, _ in parse_qsl(parsed.query))
                or _SENSITIVE_QUERY.search(parsed.fragment)
            ):
                return None
        except ValueError:
            return None
        return url

    def add_candidate(self, candidate: Mapping[str, Any], *, source_name: str) -> None:
        key = str(candidate.get("id", ""))
        if key in self._rows or len(self._rows) >= MAX_CANDIDATES:
            return
        self._rows[key] = {
            "id": self.text(key, 64) or f"c{len(self._rows) + 1:02d}",
            "title": self.text(candidate.get("title"), 140) or "无标题",
            "source": {
                "name": self.text(source_name, 120),
                "url": self.url(candidate.get("url")),
            },
            "researcher": None,
            "writer": None,
            "reviewer": None,
            "published": False,
        }

    def observe(
        self, candidate: Mapping[str, Any], stage: str, update: Mapping[str, Any]
    ) -> None:
        row = self._rows.get(str(candidate.get("id", "")))
        if row is None:
            return
        if stage == "researcher":
            notes = update.get("research_notes")
            if isinstance(notes, Mapping) and notes.get("decision") in {"KEEP", "SKIP"}:
                row["researcher"] = {
                    "decision": str(notes["decision"]).lower(),
                    "reason": self._chinese(
                        notes.get("reason"), 80, "研究员未提供中文原因。"
                    ),
                    "notes": [
                        self._chinese(
                            note.get("claim"), 120, "研究员未提供中文研究要点。"
                        )
                        for note in notes.get("notes", [])[:4]
                        if isinstance(note, Mapping)
                    ]
                    if notes["decision"] == "KEEP"
                    else [],
                }
        elif stage == "writer" and row["researcher"]:
            draft = update.get("draft")
            if isinstance(draft, Mapping) and row["researcher"]["decision"] == "keep":
                row["writer"] = {
                    "headline": self.text(draft.get("headline"), 80),
                    "summary": self._summary(draft.get("summary")),
                }
        elif stage == "reviewer" and row["writer"]:
            review = update.get("review")
            verdicts = {"PASS": "approve", "REVISE": "revise", "REJECT": "reject"}
            if isinstance(review, Mapping) and review.get("status") in verdicts:
                problems = [
                    issue.get("problem", "")
                    for issue in review.get("issues", [])
                    if isinstance(issue, Mapping)
                ]
                # REVISE can legally contain only a revision_brief.
                problems.extend(review.get("revision_brief", []))
                issues = list(
                    dict.fromkeys(
                        self._chinese(problem, 100, "审查结果未提供中文问题说明。")
                        for problem in problems
                        if problem
                    )
                )[:4]
                previous = row["reviewer"]
                row["reviewer"] = {
                    "verdict": verdicts[review["status"]],
                    "rounds": (previous["rounds"] if previous else 0) + 1,
                    "issues": [] if review["status"] == "PASS" else issues,
                }

    def mark_published(self, candidate: Mapping[str, Any]) -> None:
        row = self._rows.get(str(candidate.get("id", "")))
        if row and row["reviewer"] and row["reviewer"]["verdict"] == "approve":
            row["published"] = True

    def finish(
        self, *, date: str, number: int, outcome: str, finished_at: datetime
    ) -> str:
        """Serialize one bounded snapshot; counts describe only exported rows."""
        rows = deepcopy(list(self._rows.values()))
        counts = {
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
            "published": sum(r["published"] for r in rows),
        }
        clean_status = (
            "partial_success"
            if counts["approved"] > counts["published"]
            else "success"
            if counts["published"]
            else "no_items"
        )
        statuses = {
            "SUCCESS": clean_status,
            "PARTIAL_SUCCESS": "partial_success",
            "QUOTA_STOPPED": "quota_stopped",
            # The fixed contract has no failed state. The operational outcome,
            # exit code and alerts retain FAILED; no_items is only normal runs.
            "FAILED": "partial_success",
        }
        document = {
            "date": date,
            "run": {
                "number": number,
                "status": statuses[outcome],
                "started_at": self.started_at.astimezone(TRACE_TZ)
                .replace(microsecond=0)
                .isoformat(),
                "finished_at": finished_at.astimezone(TRACE_TZ)
                .replace(microsecond=0)
                .isoformat(),
            },
            "counts": counts,
            "candidates": rows,
        }
        shrink_passes = 0
        while True:
            encoded = (
                json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            if len(encoded.encode("utf-8")) <= MAX_TRACE_BYTES:
                return encoded
            shrink_passes += 1
            # Preserve rows, stages, decisions, rounds and counts. Reduce text
            # uniformly instead of dropping late rejected/deferred candidates.
            for row in rows:
                row["title"] = _clip(row["title"], max(1, len(row["title"]) // 2))
                source = row["source"]
                source["name"] = _clip(source["name"], max(1, len(source["name"]) // 2))
                if source["url"] and (
                    len(json.dumps(source["url"], ensure_ascii=False).encode("utf-8"))
                    > 512
                    or shrink_passes >= 3
                ):
                    source["url"] = (
                        None  # Do not turn a shortened URL into another URL.
                    )
                for stage in ("researcher", "writer", "reviewer"):
                    for key, value in (row[stage] or {}).items():
                        if key in {"reason", "headline", "summary"}:
                            row[stage][key] = _clip(value, max(1, len(value) // 2))
                        elif key in {"notes", "issues"}:
                            row[stage][key] = [
                                _clip(text, max(1, len(text) // 2)) for text in value
                            ]
