"""Daily Chip News V1.

A small scheduled workflow that collects semiconductor news, filters it for
business relevance with Gemini, and delivers concise Chinese briefs to Telegram.
"""

import os
import time

import feedparser
import requests


GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

RSS_FEEDS = [
    # Industry media
    "https://www.eetimes.com/feed/",
    # Process and manufacturing
    "https://semiengineering.com/feed/",
    # Enterprise / server hardware
    "https://www.servethehome.com/feed/",
    # Market and supply-chain trends
    "https://www.trendforce.com/rss",
    # Broader technology signals
    "https://hnrss.org/newest?points=100",
]


def check_available_models():
    """Validate the API key and return models that support generateContent."""
    print("Checking Gemini model availability...")
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"

    try:
        response = requests.get(url, timeout=20)
    except requests.RequestException as exc:
        raise RuntimeError(f"Unable to reach Gemini model endpoint: {exc}") from exc

    if response.status_code != 200:
        raise RuntimeError(
            f"Gemini model discovery failed: HTTP {response.status_code} - {response.text[:500]}"
        )

    models = response.json().get("models", [])
    available = [
        model["name"].replace("models/", "")
        for model in models
        if "generateContent" in model.get("supportedGenerationMethods", [])
    ]

    if GEMINI_MODEL not in available:
        raise RuntimeError(
            f"Configured model '{GEMINI_MODEL}' is unavailable. "
            f"Available generateContent models: {available}"
        )

    print(f"Using Gemini model: {GEMINI_MODEL}")
    return available


def clean_content_jina(url):
    """Convert an article page to cleaner text with Jina Reader."""
    jina_url = f"https://r.jina.ai/{url}"
    max_retries = 3

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(jina_url, timeout=30)
            if response.status_code == 200:
                print(f"Jina extraction succeeded ({len(response.text)} chars)")
                return response.text
            print(
                f"Jina returned HTTP {response.status_code} "
                f"(attempt {attempt}/{max_retries})"
            )
        except requests.RequestException as exc:
            print(f"Jina request failed (attempt {attempt}/{max_retries}): {exc}")

        if attempt < max_retries:
            time.sleep(5)

    print(f"Skipping article after extraction failure: {url}")
    return None


def analyze_via_rest_api(text, title, model_name=GEMINI_MODEL):
    """Return SKIP for irrelevant content or a short structured Chinese brief."""
    if len(text) < 200:
        return "SKIP"

    api_url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model_name}:generateContent?key={GEMINI_API_KEY}"
    )
    headers = {"Content-Type": "application/json"}

    prompt = f"""
你是一位专业的半导体芯片销售工程师。请阅读这篇来自专业媒体的新闻。

标题：{title}
内容：
{text[:8000]}

【任务判断】
请判断这篇文章对于“销售工程师”是否有商业或技术价值。

如果是以下内容，请直接回复 "SKIP"：
- 纯粹的消费级数码产品评测。
- 过于基础的编程教程。
- 与芯片、半导体供应链、服务器硬件无关的社会新闻。

如果是以下内容，必须生成总结：
- 晶圆厂（TSMC、Intel、Samsung）的工艺进展或扩产计划。
- 关键原厂（NVIDIA、AMD、TI、ADI 等）的新产品发布或财报。
- 供应链涨价、缺货或库存预警。
- 具体的 B2B 硬件技术突破（如 CXL、HBM、RISC-V）。

【总结格式】
如果通过筛选，请用中文总结，控制在 100 字左右：
🚨 *核心情报*：一句话概括发生了什么。
📉 *关键数据*：提取文中的金额、制程纳米数、良率或日期等关键数据。
"""

    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    max_retries = 2

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(
                api_url,
                headers=headers,
                json=payload,
                timeout=30,
            )
        except requests.RequestException as exc:
            if attempt == max_retries:
                raise RuntimeError(f"Gemini request failed: {exc}") from exc
            print(f"Gemini request failed; retrying ({attempt}/{max_retries}): {exc}")
            time.sleep(3)
            continue

        if response.status_code == 200:
            try:
                return response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            except (KeyError, IndexError, TypeError) as exc:
                raise RuntimeError(
                    f"Gemini returned an unexpected response: {response.text[:500]}"
                ) from exc

        # Authentication, quota, model and other 4xx failures should not be
        # converted into an editorial SKIP decision.
        if 400 <= response.status_code < 500:
            raise RuntimeError(
                f"Gemini API failed: HTTP {response.status_code} - {response.text[:500]}"
            )

        if attempt == max_retries:
            raise RuntimeError(
                f"Gemini API failed after retries: HTTP {response.status_code} - "
                f"{response.text[:500]}"
            )

        print(
            f"Gemini returned HTTP {response.status_code}; "
            f"retrying ({attempt}/{max_retries})"
        )
        time.sleep(3)

    raise RuntimeError("Gemini request ended without a result")


def send_telegram(message):
    """Send Markdown first, then fall back to plain text if formatting fails."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            print("Telegram message sent")
            return
        print(f"Telegram Markdown send failed: {response.text[:300]}")
    except requests.RequestException as exc:
        print(f"Telegram Markdown request failed: {exc}")

    payload.pop("parse_mode", None)
    try:
        response = requests.post(url, json=payload, timeout=10)
    except requests.RequestException as exc:
        raise RuntimeError(f"Telegram fallback request failed: {exc}") from exc

    if response.status_code != 200:
        raise RuntimeError(
            f"Telegram delivery failed: HTTP {response.status_code} - {response.text[:500]}"
        )

    print("Telegram plain-text fallback sent")


def main():
    print("Daily Chip News run started")
    check_available_models()

    stats = {
        "articles_seen": 0,
        "articles_extracted": 0,
        "articles_skipped": 0,
        "articles_sent": 0,
    }
    seen_urls = set()

    for feed_url in RSS_FEEDS:
        print(f"Checking source: {feed_url}")
        feed = feedparser.parse(feed_url)

        for entry in feed.entries[:2]:
            link = entry.get("link")
            title = entry.get("title", "Untitled article")

            if not link or link in seen_urls:
                continue

            seen_urls.add(link)
            stats["articles_seen"] += 1
            print(f"Article: {title}")

            content = clean_content_jina(link)
            if not content:
                continue

            stats["articles_extracted"] += 1
            analysis = analyze_via_rest_api(content, title)

            if not analysis or "SKIP" in analysis:
                stats["articles_skipped"] += 1
                print("Editorial decision: SKIP")
                continue

            message = f"🚨 *{title}*\n\n{analysis}\n\n🔗 [原文链接]({link})"
            send_telegram(message)
            stats["articles_sent"] += 1
            time.sleep(2)

    print("Run summary:")
    for key, value in stats.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
