"""Daily Chip News V2.

A small three-node editorial micro-graph for semiconductor intelligence:
Researcher -> Writer -> Reviewer -> deterministic Telegram publisher.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import feedparser
import requests


GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
ARTICLES_PER_FEED = int(os.getenv("ARTICLES_PER_FEED", "2"))
MAX_REVISIONS = int(os.getenv("MAX_REVISIONS", "2"))

RSS_FEEDS = [
    "https://www.eetimes.com/feed/",
    "https://semiengineering.com/feed/",
    "https://www.servethehome.com/feed/",
    "https://www.trendforce.com/rss",
    "https://hnrss.org/newest?points=100",
]

EDITORIAL_SCOPE = [
    "semiconductors",
    "foundries and process nodes",
    "memory and storage",
    "chip vendors and product launches",
    "AI infrastructure and server hardware",
    "supply-chain pricing, shortages and inventory",
    "B2B hardware technologies such as HBM, CXL and RISC-V",
]

REVIEW_RUBRIC = [
    "factual grounding",
    "source coverage",
    "unsupported claims",
    "recency",
    "commercial relevance",
    "clarity",
    "duplication",
    "tone",
    "length",
    "output formatting",
]


def check_available_model() -> None:
    """Validate the configured Gemini model before the graph starts."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
    try:
        response = requests.get(url, timeout=20)
    except requests.RequestException as exc:
        raise RuntimeError(f"Unable to reach Gemini model endpoint: {exc}") from exc

    if response.status_code != 200:
        raise RuntimeError(
            f"Gemini model discovery failed: HTTP {response.status_code} - {response.text[:500]}"
        )

    available = {
        model["name"].replace("models/", "")
        for model in response.json().get("models", [])
        if "generateContent" in model.get("supportedGenerationMethods", [])
    }
    if GEMINI_MODEL not in available:
        raise RuntimeError(
            f"Configured model '{GEMINI_MODEL}' is unavailable. "
            f"Choose one of: {sorted(available)}"
        )

    print(f"Gemini model ready: {GEMINI_MODEL}")


def gemini_json(prompt: str, *, max_retries: int = 3) -> dict[str, Any]:
    """Call Gemini and require a JSON object response."""
    api_url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(api_url, json=payload, timeout=45)
        except requests.RequestException as exc:
            if attempt == max_retries:
                raise RuntimeError(f"Gemini request failed: {exc}") from exc
            time.sleep(2 * attempt)
            continue

        if response.status_code == 200:
            try:
                text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)
            except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"Gemini returned invalid structured output: {response.text[:800]}"
                ) from exc

        if response.status_code == 429 and attempt < max_retries:
            print(f"Gemini rate limited; retrying ({attempt}/{max_retries})")
            time.sleep(4 * attempt)
            continue

        if 500 <= response.status_code < 600 and attempt < max_retries:
            print(f"Gemini server error {response.status_code}; retrying")
            time.sleep(3 * attempt)
            continue

        raise RuntimeError(
            f"Gemini API failed: HTTP {response.status_code} - {response.text[:800]}"
        )

    raise RuntimeError("Gemini request ended without a result")


def extract_article(url: str) -> str | None:
    """Use Jina Reader to strip page chrome before research."""
    jina_url = f"https://r.jina.ai/{url}"
    for attempt in range(1, 4):
        try:
            response = requests.get(jina_url, timeout=30)
            if response.status_code == 200 and len(response.text) >= 200:
                return response.text
            print(f"Jina returned HTTP {response.status_code} ({attempt}/3)")
        except requests.RequestException as exc:
            print(f"Jina request failed ({attempt}/3): {exc}")

        if attempt < 3:
            time.sleep(3)

    return None


def researcher_node(article: dict[str, str]) -> dict[str, Any]:
    """Retrieve evidence and emit structured notes only."""
    content = extract_article(article["url"])
    if not content:
        return {
            "decision": "SKIP",
            "reason": "content_extraction_failed",
            "source": article["source"],
            "url": article["url"],
            "title": article["title"],
            "notes": [],
        }

    prompt = f"""
你是 Researcher（研究员），负责半导体行业情报检索与证据整理。
你不写最终稿，只输出结构化研究笔记。

关注范围：{json.dumps(EDITORIAL_SCOPE, ensure_ascii=False)}

文章元数据：
{json.dumps(article, ensure_ascii=False)}

文章正文：
{content[:12000]}

任务：
1. 判断这篇内容对半导体销售、解决方案、业务决策是否具有技术或商业价值。
2. 无关内容返回 decision=SKIP。
3. 有价值内容返回 decision=KEEP，并提取 1-4 条可由原文支持的事实笔记。
4. 不允许补充文章中没有的数字、市场份额、因果结论或预测。
5. evidence 必须是对原文证据的简短转述；why_it_matters 只解释业务意义，不制造新事实。

仅输出 JSON：
{{
  "decision": "KEEP or SKIP",
  "reason": "简短原因",
  "topic": "主题",
  "source": "来源",
  "url": "原文链接",
  "published_at": "发布日期或空字符串",
  "notes": [
    {{
      "claim": "事实陈述",
      "evidence": "证据摘要",
      "why_it_matters": "业务意义",
      "confidence": 0.0
    }}
  ]
}}
"""
    result = gemini_json(prompt)
    result.setdefault("source", article["source"])
    result.setdefault("url", article["url"])
    result.setdefault("published_at", article.get("published_at", ""))
    result.setdefault("title", article["title"])
    return result


def writer_node(
    research_notes: dict[str, Any],
    revision_brief: list[str] | None = None,
) -> dict[str, Any]:
    """Create a draft from a fresh context containing only approved research notes."""
    clean_context = {
        "editorial_brief": {
            "audience": "关注半导体、AI 基础设施与 B2B 硬件的中文商业读者",
            "goal": "快速说明发生了什么、关键事实是什么、为什么值得关注",
            "style": "简洁、克制、事实优先，不写营销腔",
        },
        "research_notes": research_notes,
        "revision_brief": revision_brief or [],
    }

    prompt = f"""
你是 Writer（写手）。这是一次全新的干净上下文。
你只能使用下面提供的 Research Notes，不得利用未提供的背景知识补事实。

输入：
{json.dumps(clean_context, ensure_ascii=False)}

要求：
- headline：一句清晰标题。
- summary：80-160 个中文字，概括核心变化。
- key_facts：2-4 条，只能来自研究笔记。
- why_it_matters：用 1-2 句话说明对半导体产业、客户或供应链的意义。
- telegram_copy：可直接发送到 Telegram 的中文成稿，控制在 500 个中文字以内。
- 如有 revision_brief，必须逐条修正，但仍不得超出 Research Notes。

仅输出 JSON：
{{
  "headline": "...",
  "summary": "...",
  "key_facts": ["..."],
  "why_it_matters": "...",
  "telegram_copy": "..."
}}
"""
    return gemini_json(prompt)


def reviewer_node(
    research_notes: dict[str, Any],
    draft: dict[str, Any],
) -> dict[str, Any]:
    """Score the draft against an explicit QA rubric and return PASS/REJECT."""
    review_context = {
        "rubric": REVIEW_RUBRIC,
        "research_notes": research_notes,
        "draft": draft,
    }

    prompt = f"""
你是 Reviewer（审稿人），只做质量验收，不重写成稿。

输入：
{json.dumps(review_context, ensure_ascii=False)}

验收标准：
- factuality、relevance、clarity 均按 0-10 分评分。
- 任何无 Research Notes 支持的事实、数字、因果或预测都属于 major issue。
- headline、summary、key_facts、why_it_matters 与 telegram_copy 必须相互一致。
- 内容应清晰、简洁、与半导体商业场景相关。
- factuality < 8、relevance < 8，或存在 major issue 时必须 REJECT。
- PASS 时 revision_brief 必须为空数组。
- REJECT 时只给具体修改要求，不要代替 Writer 重写。

仅输出 JSON：
{{
  "status": "PASS or REJECT",
  "scores": {{
    "factuality": 0,
    "relevance": 0,
    "clarity": 0
  }},
  "issues": [
    {{"severity": "major or minor", "problem": "..."}}
  ],
  "revision_brief": ["..."]
}}
"""
    return gemini_json(prompt)


def run_editorial_graph(article: dict[str, str]) -> dict[str, Any]:
    """Run Researcher -> Writer -> Reviewer with a bounded rewrite loop."""
    research = researcher_node(article)
    if str(research.get("decision", "")).upper() != "KEEP":
        return {
            "status": "SKIP",
            "research": research,
            "revision_count": 0,
        }

    revision_brief: list[str] = []

    for revision_count in range(MAX_REVISIONS + 1):
        draft = writer_node(research, revision_brief)
        review = reviewer_node(research, draft)
        status = str(review.get("status", "REJECT")).upper()

        if status == "PASS":
            return {
                "status": "PASS",
                "research": research,
                "draft": draft,
                "review": review,
                "revision_count": revision_count,
            }

        revision_brief = [
            str(item) for item in review.get("revision_brief", []) if str(item).strip()
        ]
        if not revision_brief:
            revision_brief = ["根据审稿问题重新生成，删除所有缺乏研究笔记支持的内容。"]

    return {
        "status": "HOLD",
        "research": research,
        "draft": draft,
        "review": review,
        "revision_count": MAX_REVISIONS,
    }


def send_telegram(message: str, source_url: str) -> None:
    """Deterministic publisher: only receives reviewer-approved copy."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    final_text = f"{message}\n\n🔗 原文：{source_url}"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": final_text,
        "disable_web_page_preview": False,
    }

    try:
        response = requests.post(url, json=payload, timeout=15)
    except requests.RequestException as exc:
        raise RuntimeError(f"Telegram request failed: {exc}") from exc

    if response.status_code != 200:
        raise RuntimeError(
            f"Telegram delivery failed: HTTP {response.status_code} - {response.text[:500]}"
        )


def collect_articles() -> list[dict[str, str]]:
    """Collect a small daily candidate set from configured RSS feeds."""
    articles: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for feed_url in RSS_FEEDS:
        feed = feedparser.parse(feed_url)
        source = feed.feed.get("title", feed_url)

        for entry in feed.entries[:ARTICLES_PER_FEED]:
            url = entry.get("link")
            if not url or url in seen_urls:
                continue

            seen_urls.add(url)
            articles.append(
                {
                    "title": entry.get("title", "Untitled article"),
                    "url": url,
                    "source": source,
                    "published_at": entry.get("published", entry.get("updated", "")),
                }
            )

    return articles


def main() -> None:
    print("Daily Chip News V2 run started")
    check_available_model()
    articles = collect_articles()

    stats = {
        "candidates": len(articles),
        "research_skipped": 0,
        "review_passed": 0,
        "review_rejected_then_revised": 0,
        "held_after_max_revisions": 0,
        "published": 0,
    }

    for article in articles:
        print(f"\nProcessing: {article['title']}")
        result = run_editorial_graph(article)

        if result["status"] == "SKIP":
            stats["research_skipped"] += 1
            print(f"Researcher SKIP: {result['research'].get('reason', '')}")
            continue

        if result["status"] == "HOLD":
            stats["held_after_max_revisions"] += 1
            print("Reviewer did not approve after bounded revisions; item held")
            continue

        if result.get("revision_count", 0) > 0:
            stats["review_rejected_then_revised"] += 1

        stats["review_passed"] += 1
        draft = result["draft"]
        send_telegram(draft["telegram_copy"], article["url"])
        stats["published"] += 1
        print(
            "Published after reviewer PASS "
            f"(revisions={result.get('revision_count', 0)})"
        )
        time.sleep(1)

    print("\nRun summary:")
    for key, value in stats.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
