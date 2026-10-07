"""RSS financial-news collector with optional DeepSeek summary and ServerChan push."""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Iterable
from zoneinfo import ZoneInfo


RSS_FEEDS = {
    "China": {
        "Eastmoney": "http://rss.eastmoney.com/rss_partener.xml",
        "China News": "https://www.chinanews.com.cn/rss/finance.xml",
        "NBS": "https://www.stats.gov.cn/sj/zxfb/rss.xml",
    },
    "Global": {
        "WSJ Markets": "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
        "MarketWatch": "https://www.marketwatch.com/rss/topstories",
        "BBC Business": "http://feeds.bbci.co.uk/news/business/rss.xml",
    },
    "Technology": {
        "36Kr": "https://36kr.com/feed",
    },
}


def today_text() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


def get_client():
    from openai import OpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for AI summarization")
    return OpenAI(
        api_key=api_key,
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1"),
    )


def fetch_feed(url: str, retries: int = 3, delay: int = 3):
    import feedparser

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; AI-Quant-Research-System/1.0)"
    }
    for attempt in range(1, retries + 1):
        try:
            feed = feedparser.parse(url, request_headers=headers)
            if getattr(feed, "entries", None):
                return feed
        except Exception as exc:
            print(f"feed attempt {attempt} failed: {url}: {exc}")
        if attempt < retries:
            time.sleep(delay)
    return None


def fetch_article_text(url: str, max_chars: int = 1800) -> str:
    from newspaper import Article

    try:
        article = Article(url)
        article.download()
        article.parse()
        return (article.text or "")[:max_chars]
    except Exception as exc:
        print(f"article skipped: {url}: {exc}")
        return ""


def collect_news(
    feeds: dict[str, dict[str, str]] = RSS_FEEDS,
    articles_per_source: int = 5,
) -> tuple[dict[str, str], str]:
    link_sections: dict[str, str] = {}
    analysis_parts: list[str] = []

    for category, sources in feeds.items():
        category_lines: list[str] = []
        for source, url in sources.items():
            feed = fetch_feed(url)
            if not feed:
                continue
            source_lines: list[str] = []
            for entry in feed.entries[:articles_per_source]:
                title = entry.get("title", "Untitled")
                link = entry.get("link", "") or entry.get("guid", "")
                if not link:
                    continue
                body = fetch_article_text(link)
                if body:
                    analysis_parts.append(f"[{title}]\n{body}")
                source_lines.append(f"- [{title}]({link})")
            if source_lines:
                category_lines.append(f"### {source}\n" + "\n".join(source_lines))
        link_sections[category] = "\n\n".join(category_lines)

    return link_sections, "\n\n".join(analysis_parts)


def summarize(text: str) -> str:
    if not text.strip():
        return "No article body was available for summarization."

    prompt = (
        "You are a financial research assistant. Produce a concise Chinese briefing. "
        "Separate verified news facts from inference. Cover macro policy, industries, "
        "risk signals and themes worth monitoring. Do not provide guaranteed returns, "
        "direct buy/sell instructions or fabricated price changes. Keep it under 1200 Chinese characters."
    )
    completion = get_client().chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "deepseek-chat"),
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ],
        temperature=0.2,
    )
    return completion.choices[0].message.content.strip()


def serverchan_keys() -> list[str]:
    raw = os.getenv("SERVER_CHAN_KEYS", "")
    return [key.strip() for key in raw.split(",") if key.strip()]


def send_report(title: str, content: str, keys: Iterable[str] | None = None) -> None:
    import requests

    keys = list(keys if keys is not None else serverchan_keys())
    if not keys:
        print(f"\n# {title}\n\n{content}")
        return

    for key in keys:
        response = requests.post(
            f"https://sctapi.ftqq.com/{key}.send",
            data={"title": title, "desp": content},
            timeout=15,
        )
        response.raise_for_status()


def render_report(summary: str, link_sections: dict[str, str]) -> str:
    blocks = [f"## AI research summary\n\n{summary}"]
    for category, content in link_sections.items():
        if content.strip():
            blocks.append(f"## {category}\n\n{content}")
    blocks.append(
        "> Research use only. The summary may contain errors and does not constitute investment advice."
    )
    return "\n\n---\n\n".join(blocks)


def main() -> None:
    sections, analysis_text = collect_news()
    summary = summarize(analysis_text)
    report = render_report(summary, sections)
    send_report(f"{today_text()} Financial News Brief", report)


if __name__ == "__main__":
    main()
