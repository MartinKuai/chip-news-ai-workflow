"""Deterministic website publishing for martinkuai.com (site BRIEF section 7.2).

After the candidate loop the runner hands every Reviewer-approved item to
``SitePublisher.publish``. One run makes at most one commit in the website repo:

* ``data/news/YYYY-MM-DD.json`` (Asia/Shanghai date of publication), only when
  at least one item passed review. A run where nothing passed (every candidate
  skipped, rejected or held at the revision limit) writes no news file, so the
  homepage keeps showing the latest day that had news.
* ``data/news/stats.json`` on every run that reaches the publisher, including
  "nothing passed review" runs and failed runs.
* ``data/news/YYYY-MM-DD.trace.json`` when the runner supplies its editorial
  snapshot, including runs with no approved news; same-day reruns replace it.

These files are plain data: every string comes from the editorial nodes or RSS
metadata and the website must escape it when rendering.

Stats semantics (what the owner quotes: "270 scheduled runs, about 97% success"):
``total_runs`` counts *scheduled* runs of the workflow and ``success_rate`` is
the share of them whose GitHub Actions conclusion is ``success``. Manual
``workflow_dispatch`` runs may publish news but are not counted. See
``compute_stats`` for how the counter stays correct after GitHub started
expiring run history after 90 days (public repos, since 2026-10-01).
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

import requests

from .config import DEFAULT_BRIEF_NAME
from .publisher import PublisherError
from .trace import TraceRecorder

SITE_TZ = ZoneInfo("Asia/Shanghai")
STATS_FILE = "stats.json"
MAX_SUMMARY_SENTENCES = 3
# Four decimals keep round(success_rate * total_runs) exact below 10,000 runs,
# which is what lets stats.json carry the success count without extra fields.
RATE_DECIMALS = 4
# Feeds that link to third-party articles: name the article's site instead.
AGGREGATOR_FEED_HOSTS = frozenset({"hnrss.org", "news.ycombinator.com"})
GITHUB_API_ROOT = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
USER_AGENT = "chip-news-ai-workflow"

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# A sentence ends at Chinese/Western terminators (runs such as "？！" count once)
# or at a period followed by whitespace; "3.5" and "..." mid-word do not split.
_SENTENCE_END = re.compile(r"(?:[。！？!?]|(?<!\d)\.(?=\s|$))+[”’\"'）)\]」』]*")
_TITLE_TRAILING = "。！？!?.，,；;：: "


class SitePublishError(PublisherError):
    """Raised when the website files cannot be written (category PUBLISH_ERROR)."""


class SiteConflict(Exception):
    """The site branch moved between reading it and updating the ref."""


# --------------------------------------------------------------------------
# Items and the daily document
# --------------------------------------------------------------------------


def clean_text(value: Any) -> str:
    """Single-line text without control characters."""
    text = _CONTROL_CHARS.sub(" ", str(value or ""))
    return " ".join(text.split())


def safe_url(value: Any) -> str:
    """Only absolute http(s) URLs reach the website; anything else is dropped."""
    url = clean_text(value)
    if " " in url:
        return ""
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return ""
    return url


def limit_sentences(text: str, max_sentences: int = MAX_SUMMARY_SENTENCES) -> str:
    """Keep at most ``max_sentences`` sentences (the contract's 1-3 sentences).

    The Writer is asked for 1-3 sentences and the Reviewer checks format; this
    is a deterministic backstop that only truncates, it never rewrites.
    """
    clean = text.strip()
    for count, match in enumerate(_SENTENCE_END.finditer(clean), start=1):
        if count == max_sentences:
            head, tail = clean[: match.end()], clean[match.end() :]
            return head.strip() if tail.strip() else clean
    return clean


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def source_name(candidate: Mapping[str, Any], url: str) -> str:
    """Feed title, or the article's host for aggregator feeds (Hacker News)."""
    metadata = candidate.get("metadata") or {}
    feed_url = (
        str(metadata.get("feed_url", "")) if isinstance(metadata, Mapping) else ""
    )
    name = clean_text(candidate.get("source"))[:120]
    if not name or _host(feed_url) in AGGREGATOR_FEED_HOSTS:
        return _host(url) or name
    return name


def site_item(
    candidate: Mapping[str, Any], draft: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """Map one approved draft to a contract item; ``None`` if it is unusable.

    Title and summary are the Writer's structured-output fields ``headline`` and
    ``summary`` exactly as the Reviewer approved them (whitespace normalised,
    at most three sentences). Source name and URL are deterministic RSS
    metadata, never model output.
    """
    if not isinstance(draft, Mapping):
        return None
    url = safe_url(candidate.get("url"))
    title = clean_text(draft.get("headline"))
    summary = limit_sentences(clean_text(draft.get("summary")))
    if not (url and title and summary):
        return None
    return {
        "title": title,
        "summary": summary,
        "source": {"name": source_name(candidate, url), "url": url},
    }


def _normalise_item(value: Any) -> dict[str, Any] | None:
    """Re-validate an item read back from an existing day file."""
    if not isinstance(value, Mapping):
        return None
    source = value.get("source")
    if not isinstance(source, Mapping):
        return None
    url = safe_url(source.get("url"))
    title = clean_text(value.get("title"))
    summary = clean_text(value.get("summary"))
    if not (url and title and summary):
        return None
    return {
        "title": title,
        "summary": summary,
        "source": {"name": clean_text(source.get("name")) or _host(url), "url": url},
    }


def brief_title(
    items: Sequence[Mapping[str, Any]], brief_name: str = DEFAULT_BRIEF_NAME
) -> str:
    """Deterministic headline for the day: no extra model call, no new facts."""
    first = str(items[0]["title"])
    if len(items) == 1:
        return first
    ascii_name = brief_name and brief_name[0].isascii() and brief_name[0].isalnum()
    sep = " " if ascii_name else ""
    return f"{first.rstrip(_TITLE_TRAILING)} 等 {len(items)} 条{sep}{brief_name}"


def build_day_document(
    *,
    date: str,
    published_at: str,
    items: Sequence[Mapping[str, Any]],
    run_number: int,
    run_status: str,
    existing: Mapping[str, Any] | None = None,
    brief_name: str = DEFAULT_BRIEF_NAME,
) -> dict[str, Any]:
    """Build ``data/news/YYYY-MM-DD.json``.

    A second run on the same Shanghai date (for example a manual re-run) adds
    its items after the ones already published that day, de-duplicated by
    source URL, and keeps the first ``published_at``.
    """
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    first_published = published_at
    if isinstance(existing, Mapping) and existing.get("date") == date:
        previous_items = existing.get("items")
        for raw in previous_items if isinstance(previous_items, list) else []:
            item = _normalise_item(raw)
            if item and item["source"]["url"] not in seen:
                seen.add(item["source"]["url"])
                merged.append(item)
        if merged and isinstance(existing.get("published_at"), str):
            first_published = existing["published_at"]
    for raw in items:
        item = _normalise_item(raw)
        if item and item["source"]["url"] not in seen:
            seen.add(item["source"]["url"])
            merged.append(item)
    if not merged:
        raise ValueError("A day document needs at least one item")
    return {
        "date": date,
        "published_at": first_published,
        "title": brief_title(merged, brief_name=brief_name),
        "items": merged,
        "run": {"number": run_number, "status": run_status},
    }


# --------------------------------------------------------------------------
# Run statistics
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowRun:
    """One scheduled run as reported by the GitHub Actions API."""

    id: int
    status: str
    conclusion: str | None
    created_at: datetime


@dataclass(frozen=True)
class StatsResult:
    document: dict[str, Any]
    counted_current: bool
    # "actions-api": recomputed from GitHub's run list; "carried": previous
    # stats.json plus the runs since it was written.
    basis: str


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _previous_counts(
    previous: Mapping[str, Any] | None,
) -> tuple[int, int, datetime | None, str | None]:
    """(total, successes, updated_at, since) or zeros when unusable.

    A stats file without a parseable ``updated_at`` cannot tell which runs it
    already contains, so it is ignored rather than risk double counting.
    """
    if not isinstance(previous, Mapping):
        return 0, 0, None, None
    total = previous.get("total_runs")
    rate = previous.get("success_rate")
    updated = parse_time(previous.get("updated_at"))
    if (
        isinstance(total, bool)
        or not isinstance(total, int)
        or total < 0
        or isinstance(rate, bool)
        or not isinstance(rate, (int, float))
        or not 0 <= rate <= 1
        or updated is None
    ):
        return 0, 0, None, None
    since = previous.get("since")
    valid_since = (
        since
        if isinstance(since, str) and re.fullmatch(r"\d{4}-\d{2}", since)
        else None
    )
    return total, round(rate * total), updated, valid_since


def _successes(runs: Sequence[WorkflowRun]) -> int:
    return sum(1 for run in runs if run.conclusion == "success")


def compute_stats(
    *,
    previous: Mapping[str, Any] | None,
    history: Sequence[WorkflowRun] | None,
    current_run_id: int | None,
    scheduled: bool,
    succeeded: bool,
    counted_since: datetime,
    now: datetime,
    default_since: str,
) -> StatsResult:
    """Return the new ``stats.json``.

    Two estimates are computed and the larger total wins (the API on ties):

    * **actions-api**: every completed scheduled run GitHub still lists, plus
      this run. While GitHub keeps the full history this is exactly the number
      the owner quotes, and it self-corrects any earlier miscount.
    * **carried**: the previous ``stats.json`` plus completed scheduled runs
      created after its ``updated_at`` (runs that crashed before publishing are
      back-filled this way), plus this run. Once GitHub expires old runs (90
      days for public repos since 2026-10-01) the API total shrinks and the
      carried total takes over, so ``total_runs`` never goes backwards.

    The current run counts only when it is a scheduled run and the previous
    stats were written before ``counted_since`` (a re-run attempt or a retried
    commit must not count the same run twice). Its success is the pipeline's
    own verdict: any outcome other than FAILED.
    """
    prev_total, prev_success, prev_updated, prev_since = _previous_counts(previous)
    has_previous = prev_updated is not None
    count_current = scheduled and not (has_previous and prev_updated >= counted_since)
    current = int(count_current)
    current_success = int(count_current and succeeded)

    completed = [
        run
        for run in history or ()
        if run.id != current_run_id and run.status == "completed"
    ]
    missed = [
        run for run in completed if not has_previous or run.created_at > prev_updated
    ]
    total = prev_total + len(missed) + current
    success = prev_success + _successes(missed) + current_success
    basis = "carried"
    if history is not None:
        api_total = len(completed) + current
        if api_total >= total:
            total = api_total
            success = _successes(completed) + current_success
            basis = "actions-api"

    rate = round(success / total, RATE_DECIMALS) if total else 0.0
    document = {
        "total_runs": total,
        "success_rate": rate,
        "since": prev_since or default_since,
        "updated_at": now.astimezone(SITE_TZ).replace(microsecond=0).isoformat(),
    }
    return StatsResult(document=document, counted_current=count_current, basis=basis)


# --------------------------------------------------------------------------
# GitHub access
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RunContext:
    """Who is publishing: the GitHub Actions run, or a local dry-run."""

    repository: str = ""
    run_id: int | None = None
    run_attempt: int = 1
    event: str = ""
    server_url: str = "https://github.com"
    token: str = field(default="", repr=False)

    @property
    def scheduled(self) -> bool:
        return self.event == "schedule"

    @property
    def url(self) -> str:
        if not (self.repository and self.run_id):
            return ""
        return f"{self.server_url}/{self.repository}/actions/runs/{self.run_id}"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RunContext:
        source = os.environ if env is None else env

        def number(name: str) -> int | None:
            raw = source.get(name, "").strip()
            return int(raw) if raw.isdigit() else None

        return cls(
            repository=source.get("GITHUB_REPOSITORY", "").strip(),
            run_id=number("GITHUB_RUN_ID"),
            run_attempt=number("GITHUB_RUN_ATTEMPT") or 1,
            event=source.get("GITHUB_EVENT_NAME", "").strip(),
            server_url=source.get("GITHUB_SERVER_URL", "").strip()
            or "https://github.com",
            token=(source.get("GITHUB_TOKEN") or source.get("GH_TOKEN") or "").strip(),
        )


class _GitHubHTTP:
    """Minimal GitHub REST helper with bounded retries and no secret leakage."""

    def __init__(
        self,
        token: str,
        *,
        session: requests.Session | None = None,
        timeout: float = 20.0,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        api_root: str = GITHUB_API_ROOT,
    ) -> None:
        self._token = token
        self._session = session or requests.Session()
        self._timeout = timeout
        self._max_attempts = max(1, max_attempts)
        self._sleep = sleep
        self._api_root = api_root.rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: Mapping[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
        allow_missing: bool = False,
        conflict_statuses: frozenset[int] = frozenset(),
    ) -> requests.Response:
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": USER_AGENT,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        url = f"{self._api_root}{path}"
        status: int | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._session.request(
                    method,
                    url,
                    headers=headers,
                    json=body,
                    params=dict(params) if params else None,
                    timeout=self._timeout,
                )
            except requests.RequestException:
                status = None
            else:
                status = response.status_code
                if 200 <= status < 300 or (status == 404 and allow_missing):
                    return response
                if status in conflict_statuses:
                    raise SiteConflict(status)
                if status != 429 and status < 500:
                    raise SitePublishError(
                        f"GitHub {method} failed with HTTP {status}",
                        status_code=status,
                        global_failure=status in {401, 403, 404},
                    )
            if attempt < self._max_attempts:
                self._sleep(2.0 * attempt)
        if status is None:
            # Never include the exception: it can carry request details.
            raise SitePublishError("GitHub network request failed", transient=True)
        raise SitePublishError(
            f"GitHub {method} failed with HTTP {status}",
            status_code=status,
            transient=True,
        )

    def json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.request(method, path, **kwargs)
        try:
            return response.json()
        except ValueError:
            raise SitePublishError(f"GitHub {method} returned invalid JSON") from None


class ActionsRunHistory:
    """Scheduled runs of the pipeline workflow, newest first, read-only."""

    def __init__(
        self,
        repository: str,
        workflow: str,
        token: str = "",
        *,
        session: requests.Session | None = None,
        timeout: float = 20.0,
        max_pages: int = 10,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._path = (
            f"/repos/{repository}/actions/workflows/{quote(workflow, safe='')}/runs"
        )
        self._http = _GitHubHTTP(token, session=session, timeout=timeout, sleep=sleep)
        self._max_pages = max_pages

    def __call__(self) -> list[WorkflowRun]:
        runs: list[WorkflowRun] = []
        for page in range(1, self._max_pages + 1):
            data = self._http.json(
                "GET",
                self._path,
                params={"event": "schedule", "per_page": 100, "page": page},
            )
            batch = data.get("workflow_runs", []) if isinstance(data, dict) else []
            for raw in batch:
                created = (
                    parse_time(raw.get("created_at")) if isinstance(raw, dict) else None
                )
                if created is None or not isinstance(raw.get("id"), int):
                    continue
                runs.append(
                    WorkflowRun(
                        id=raw["id"],
                        status=str(raw.get("status") or ""),
                        conclusion=raw.get("conclusion"),
                        created_at=created,
                    )
                )
            if len(batch) < 100:
                break
        return runs


class SiteStore(Protocol):
    def describe(self) -> str: ...

    def head(self) -> str | None: ...

    def read(self, path: str, ref: str | None) -> str | None: ...

    def commit(
        self, files: Mapping[str, str], message: str, parent: str | None
    ) -> str: ...


class GitHubSiteStore:
    """Write files to the site repo as one commit via the Git Data API.

    Needs a fine-grained token with Contents read/write on the site repo only.
    The ref update is fast-forward only, so a concurrent push to the branch
    raises ``SiteConflict`` and the publisher re-reads and retries.
    """

    def __init__(
        self,
        repository: str,
        branch: str,
        token: str,
        *,
        session: requests.Session | None = None,
        timeout: float = 20.0,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not token:
            raise ValueError("A site repository token is required")
        self._repo = repository
        self._branch = branch
        self._http = _GitHubHTTP(
            token,
            session=session,
            timeout=timeout,
            max_attempts=max_attempts,
            sleep=sleep,
        )

    def describe(self) -> str:
        return f"github:{self._repo}@{self._branch}"

    def head(self) -> str:
        data = self._http.json(
            "GET", f"/repos/{self._repo}/git/ref/heads/{quote(self._branch)}"
        )
        try:
            return str(data["object"]["sha"])
        except (KeyError, TypeError):
            raise SitePublishError("GitHub ref response has no commit sha") from None

    def read(self, path: str, ref: str | None) -> str | None:
        response = self._http.request(
            "GET",
            f"/repos/{self._repo}/contents/{quote(path)}",
            params={"ref": ref} if ref else None,
            accept="application/vnd.github.raw+json",
            allow_missing=True,
        )
        if response.status_code == 404:
            return None
        return response.content.decode("utf-8")

    def commit(self, files: Mapping[str, str], message: str, parent: str | None) -> str:
        if not parent:
            raise SitePublishError("Cannot commit without a parent commit")
        repo = f"/repos/{self._repo}/git"
        base = self._http.json("GET", f"{repo}/commits/{parent}")
        tree = self._http.json(
            "POST",
            f"{repo}/trees",
            body={
                "base_tree": base["tree"]["sha"],
                "tree": [
                    {"path": path, "mode": "100644", "type": "blob", "content": text}
                    for path, text in files.items()
                ],
            },
        )
        commit = self._http.json(
            "POST",
            f"{repo}/commits",
            body={"message": message, "tree": tree["sha"], "parents": [parent]},
        )
        self._http.request(
            "PATCH",
            f"{repo}/refs/heads/{quote(self._branch)}",
            body={"sha": commit["sha"], "force": False},
            conflict_statuses=frozenset({409, 422}),
        )
        return str(commit["sha"])


class LocalSiteStore:
    """Dry-run store: the same files, written under a local folder."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def describe(self) -> str:
        return f"local:{self._root.as_posix()}"

    def head(self) -> None:
        return None

    def read(self, path: str, ref: str | None) -> str | None:
        target = self._root / path
        return target.read_text(encoding="utf-8") if target.is_file() else None

    def commit(self, files: Mapping[str, str], message: str, parent: str | None) -> str:
        for path, text in files.items():
            target = self._root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".tmp")
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, target)
        return str(self._root)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SitePublishResult:
    location: str
    store: str
    date: str
    items: int
    news_written: bool
    stats: dict[str, Any]
    basis: str
    counted_current: bool


def dump_json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


class SitePublisher:
    """Commit the day's news, stats and optional editorial replay together."""

    def __init__(
        self,
        store: SiteStore,
        *,
        news_dir: str = "data/news",
        brief_name: str = DEFAULT_BRIEF_NAME,
        run: RunContext | None = None,
        history: Callable[[], Sequence[WorkflowRun]] | None = None,
        default_since: str = "2026-01",
        now: Callable[[], datetime] | None = None,
        started_at: datetime | None = None,
        max_attempts: int = 3,
        logger: Callable[[str], None] | None = print,
    ) -> None:
        self._store = store
        self._news_dir = news_dir.strip("/")
        self._brief_name = brief_name
        self._run = run or RunContext()
        self._history = history
        self._default_since = default_since
        self._now = now or (lambda: datetime.now(SITE_TZ))
        # Stats written at or after this instant already contain this run.
        self._started_at = started_at or self._now()
        self._max_attempts = max(1, max_attempts)
        self._logger = logger

    @property
    def store(self) -> str:
        return self._store.describe()

    @property
    def started_at(self) -> datetime:
        return self._started_at

    def publish(
        self,
        items: Sequence[Mapping[str, Any]],
        outcome: str,
        *,
        trace: TraceRecorder | None = None,
    ) -> SitePublishResult:
        history = self._load_history()
        counted_since = self._counted_since(history)
        stats_path = f"{self._news_dir}/{STATS_FILE}"
        for attempt in range(1, self._max_attempts + 1):
            head = self._store.head()
            previous = self._load_json(stats_path, head)
            now = self._now().astimezone(SITE_TZ).replace(microsecond=0)
            stats = compute_stats(
                previous=previous,
                history=history,
                current_run_id=self._run.run_id,
                scheduled=self._run.scheduled,
                succeeded=outcome != "FAILED",
                counted_since=counted_since,
                now=now,
                default_since=self._default_since,
            )
            date = now.date().isoformat()
            files: dict[str, str] = {}
            if items:
                day_path = f"{self._news_dir}/{date}.json"
                day = build_day_document(
                    date=date,
                    published_at=now.isoformat(),
                    items=items,
                    run_number=stats.document["total_runs"],
                    run_status=outcome.lower(),
                    existing=self._load_json(day_path, head),
                    brief_name=self._brief_name,
                )
                files[day_path] = dump_json(day)
            files[stats_path] = dump_json(stats.document)
            if trace is not None:
                files[f"{self._news_dir}/{date}.trace.json"] = trace.finish(
                    date=date,
                    number=stats.document["total_runs"],
                    outcome=outcome,
                    finished_at=now,
                )
            try:
                location = self._store.commit(
                    files, self._message(date, len(items), stats), head
                )
            except SiteConflict:
                self._log(
                    f"Site publish conflict | attempt={attempt} "
                    "| action=reread_and_retry"
                )
                continue
            result = SitePublishResult(
                location=location,
                store=self._store.describe(),
                date=date,
                items=len(items),
                news_written=bool(items),
                stats=stats.document,
                basis=stats.basis,
                counted_current=stats.counted_current,
            )
            self._log(
                f"Site published | store={result.store} | date={date} "
                f"| items={len(items)} | news_file={'yes' if items else 'no'} "
                f"| total_runs={stats.document['total_runs']} "
                f"| success_rate={stats.document['success_rate']} "
                f"| basis={stats.basis} | location={location}"
            )
            return result
        raise SitePublishError("Site branch kept moving during publish", transient=True)

    def _message(self, date: str, items: int, stats: StatsResult) -> str:
        if items:
            subject = f"news: {date} daily chip news ({items} items)"
        else:
            subject = f"news: {date} stats only (no item passed review)"
        lines = [
            subject,
            "",
            f"total_runs={stats.document['total_runs']} "
            f"success_rate={stats.document['success_rate']} basis={stats.basis}",
        ]
        if self._run.url:
            lines.append(f"Run: {self._run.url}")
        return "\n".join(lines) + "\n"

    def _load_history(self) -> list[WorkflowRun] | None:
        if self._history is None:
            return None
        try:
            return list(self._history())
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            suffix = f" | status_code={status}" if isinstance(status, int) else ""
            self._log(
                "Site stats | run history unavailable "
                f"| error={type(exc).__name__}{suffix} | action=carry_forward"
            )
            return None

    def _counted_since(self, history: Sequence[WorkflowRun] | None) -> datetime:
        # A re-run attempt keeps the run's created_at; stats written after it
        # came from an earlier attempt of this same run.
        if self._run.run_attempt > 1 and history:
            for run in history:
                if run.id == self._run.run_id:
                    return run.created_at
        return self._started_at

    def _load_json(self, path: str, ref: str | None) -> dict[str, Any] | None:
        text = self._store.read(path, ref)
        if text is None:
            return None
        try:
            value = json.loads(text)
        except ValueError:
            value = None
        if not isinstance(value, dict):
            self._log(f"Site file ignored | path={path} | reason=not-a-json-object")
            return None
        return value

    def _log(self, message: str) -> None:
        if self._logger is not None:
            self._logger(message)


def create_site_publisher(
    settings: Any, env: Mapping[str, str] | None = None
) -> SitePublisher | None:
    """Wire the configured website mode; ``None`` when SITE_PUBLISH_MODE=off."""
    mode = settings.site_mode
    if mode == "off":
        return None
    run = RunContext.from_env(env)
    store: SiteStore
    if mode == "github":
        store = GitHubSiteStore(
            settings.site_repo,
            settings.site_repo_branch,
            settings.site_repo_token,
            timeout=settings.site_timeout_seconds,
        )
    else:
        store = LocalSiteStore(settings.site_local_dir)
    history = (
        ActionsRunHistory(
            run.repository,
            settings.site_stats_workflow,
            run.token,
            timeout=settings.site_timeout_seconds,
        )
        if run.repository
        else None
    )
    return SitePublisher(
        store,
        news_dir=settings.site_news_dir,
        brief_name=getattr(settings, "brief_name", DEFAULT_BRIEF_NAME),
        run=run,
        history=history,
        default_since=settings.site_stats_since,
    )
