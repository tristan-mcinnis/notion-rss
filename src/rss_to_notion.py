#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
from typing import Dict, Iterable, List, Optional

import feedparser
from notion_client import Client
from dateutil import parser as dateparser
import backoff
import requests
from readability import Document
from html2markdown import convert as html2markdown_convert

NOTION_KEY = os.getenv("NOTION_API_KEY", "").strip()
DB_ID = os.getenv("NOTION_DATABASE_ID", "").strip()
RSS_FEEDS = [u.strip() for u in os.getenv("RSS_FEEDS", "").split(",") if u.strip()]
MAX_ITEMS = int(os.getenv("MAX_ITEMS_PER_FEED", "30"))
ALLOW_UPDATES = os.getenv("ALLOW_UPDATES", "true").lower() in {"1", "true", "yes"}
USER_AGENT = os.getenv("USER_AGENT", "notion-rss-bot/1.0 (+https://github.com/your/repo)")
FETCH_FULL_CONTENT = os.getenv("FETCH_FULL_CONTENT", "true").lower() in {"1", "true", "yes"}
ARTICLE_CONTENT_PROPERTY = os.getenv("ARTICLE_CONTENT_PROPERTY", "Content").strip()
ARTICLE_FETCH_TIMEOUT = float(os.getenv("ARTICLE_FETCH_TIMEOUT", "10"))

if not (NOTION_KEY and DB_ID and RSS_FEEDS):
    raise SystemExit("Missing NOTION_API_KEY, NOTION_DATABASE_ID or RSS_FEEDS envs.")

notion = Client(auth=NOTION_KEY)

session = requests.Session()
if USER_AGENT:
    session.headers.update({"User-Agent": USER_AGENT})


_DB_PROPERTIES_CACHE: Optional[Dict[str, Dict]] = None
_CONTENT_PROPERTY_WARNED = False


# ---------- Notion helpers ----------


def to_iso(dt_str: Optional[str]) -> Optional[str]:
    if not dt_str:
        return None
    try:
        return dateparser.parse(dt_str).isoformat()
    except Exception:
        return None


def chunk_text(text: str, chunk_size: int = 1900) -> Iterable[str]:
    for idx in range(0, len(text), chunk_size):
        yield text[idx : idx + chunk_size]


def to_rich_text(text: str, chunk_size: int = 1900) -> List[Dict[str, Dict[str, str]]]:
    return [{"text": {"content": chunk}} for chunk in chunk_text(text, chunk_size)]


@backoff.on_exception(backoff.expo, Exception, max_tries=5, jitter=backoff.full_jitter)
def fetch_database_properties() -> Dict[str, Dict]:
    db = notion.databases.retrieve(DB_ID)
    return db.get("properties", {})


def database_has_property(name: str) -> bool:
    global _DB_PROPERTIES_CACHE
    if not name:
        return False
    if _DB_PROPERTIES_CACHE is None:
        try:
            _DB_PROPERTIES_CACHE = fetch_database_properties()
        except Exception as exc:
            print(f"[warn] unable to inspect database properties: {exc}")
            _DB_PROPERTIES_CACHE = {}
    return name in _DB_PROPERTIES_CACHE


def build_properties(entry: Dict, source_name: str, article_markdown: Optional[str]) -> Dict:
    global _CONTENT_PROPERTY_WARNED
    title = entry.get("title") or entry.get("link") or "Untitled"
    url = entry.get("link") or ""
    published = (
        to_iso(entry.get("published"))
        or to_iso(entry.get("updated"))
        or to_iso(entry.get("created"))
    )
    author = entry.get("author") or ""
    summary = entry.get("summary") or entry.get("subtitle") or ""

    # Tags from RSS categories if present
    tags: List[Dict[str, str]] = []
    if "tags" in entry and isinstance(entry["tags"], list):
        for t in entry["tags"]:
            name = t.get("term") or t.get("label")
            if name:
                tags.append({"name": name[:90]})

    props = {
        "Title": {"title": [{"text": {"content": title[:200]}}]},
        "URL": {"url": url},
        "Published": {"date": {"start": published} if published else None},
        "Source": {"select": {"name": source_name[:90]}},
        "Summary": {"rich_text": to_rich_text(summary)} if summary else {"rich_text": []},
        "Author": {"rich_text": [{"text": {"content": author[:200]}}]} if author else {"rich_text": []},
    }

    # Only include Tags if property exists; otherwise Notion errors
    if database_has_property("Tags"):
        props["Tags"] = {"multi_select": tags}

    if article_markdown and ARTICLE_CONTENT_PROPERTY:
        if database_has_property(ARTICLE_CONTENT_PROPERTY):
            props[ARTICLE_CONTENT_PROPERTY] = {"rich_text": to_rich_text(article_markdown)}
        else:
            if not _CONTENT_PROPERTY_WARNED:
                print(
                    f"[warn] Notion database is missing the '{ARTICLE_CONTENT_PROPERTY}' property. "
                    "Full article content will be skipped."
                )
                _CONTENT_PROPERTY_WARNED = True

    return props


def _normalize_html_value(entry: Dict) -> List[str]:
    html_parts: List[str] = []
    content = entry.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                value = part.get("value")
                if value:
                    html_parts.append(str(value))
    summary_detail = entry.get("summary_detail")
    if isinstance(summary_detail, dict):
        if summary_detail.get("type", "").lower().startswith("text/html"):
            value = summary_detail.get("value")
            if value:
                html_parts.append(str(value))
    summary = entry.get("summary")
    if summary:
        html_parts.append(str(summary))
    return html_parts


def fetch_article_html(url: str) -> Optional[str]:
    try:
        resp = session.get(url, timeout=ARTICLE_FETCH_TIMEOUT)
        resp.raise_for_status()
        if not resp.encoding:
            resp.encoding = resp.apparent_encoding
        return resp.text
    except requests.RequestException as exc:
        print(f"[warn] unable to fetch article content from {url}: {exc}")
        return None


def extract_article_markdown(entry: Dict) -> Optional[str]:
    url = entry.get("link") or ""
    if not url:
        return None

    html = fetch_article_html(url)
    if html:
        try:
            doc = Document(html)
            article_html = doc.summary()
            markdown = html2markdown_convert(article_html or "").strip()
            if markdown:
                return markdown
        except Exception as exc:
            print(f"[warn] readability extraction failed for {url}: {exc}")

    for candidate in _normalize_html_value(entry):
        try:
            markdown = html2markdown_convert(candidate).strip()
        except Exception:
            continue
        if markdown:
            return markdown

    return None


@backoff.on_exception(backoff.expo, Exception, max_tries=5, jitter=backoff.full_jitter)
def query_page_by_url(url: str) -> Optional[str]:
    """
    Find an existing page by URL (exact match) to ensure idempotent upsert.
    """
    if not url:
        return None
    resp = notion.databases.query(
        **{
            "database_id": DB_ID,
            "filter": {
                "property": "URL",
                "url": {"equals": url}
            },
            "page_size": 1,
        }
    )
    results = resp.get("results", [])
    return results[0]["id"] if results else None


@backoff.on_exception(backoff.expo, Exception, max_tries=5, jitter=backoff.full_jitter)
def create_page(props: Dict):
    return notion.pages.create(parent={"database_id": DB_ID}, properties=props)


@backoff.on_exception(backoff.expo, Exception, max_tries=5, jitter=backoff.full_jitter)
def update_page(page_id: str, props: Dict):
    return notion.pages.update(page_id=page_id, properties=props)


# ---------- RSS ingestion ----------


def source_name_from_feed(feed: feedparser.FeedParserDict) -> str:
    title = (feed.get("feed", {}) or {}).get("title")
    link = (feed.get("feed", {}) or {}).get("link")
    return (title or link or "RSS").strip()


def harvest_feed(url: str) -> int:
    parsed = feedparser.parse(url, agent=USER_AGENT)
    if parsed.bozo and getattr(parsed.bozo_exception, "getMessage", None):
        print(f"[warn] feed parse issue for {url}: {parsed.bozo_exception}")

    src = source_name_from_feed(parsed)
    count_new, count_updated, count_skipped = 0, 0, 0
    should_fetch_content = FETCH_FULL_CONTENT and bool(ARTICLE_CONTENT_PROPERTY)
    if should_fetch_content and not database_has_property(ARTICLE_CONTENT_PROPERTY):
        global _CONTENT_PROPERTY_WARNED
        if not _CONTENT_PROPERTY_WARNED:
            print(
                f"[warn] Notion database is missing the '{ARTICLE_CONTENT_PROPERTY}' property. "
                "Full article content will be skipped."
            )
            _CONTENT_PROPERTY_WARNED = True
        should_fetch_content = False

    for entry in (parsed.entries or [])[:MAX_ITEMS]:
        # normalize essentials
        entry_link = entry.get("link") or ""
        if not entry_link:
            count_skipped += 1
            continue

        article_markdown = extract_article_markdown(entry) if should_fetch_content else None
        props = build_properties(entry, src, article_markdown)
        existing_id = query_page_by_url(entry_link)

        if existing_id is None:
            create_page(props)
            count_new += 1
        else:
            if ALLOW_UPDATES:
                update_page(existing_id, props)
                count_updated += 1
            else:
                count_skipped += 1

        # polite pacing vs. Notion rate limits
        time.sleep(0.2)

    print(f"[{src}] new={count_new} updated={count_updated} skipped={count_skipped}")
    return count_new + count_updated


def main():
    total = 0
    for url in RSS_FEEDS:
        try:
            total += harvest_feed(url)
        except Exception as e:
            print(f"[error] {url}: {e}")
    print(f"[done] pages upserted: {total}")


if __name__ == "__main__":
    main()
