#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
from typing import Dict, List, Optional

import feedparser
from notion_client import Client
from dateutil import parser as dateparser
import backoff

NOTION_KEY = os.getenv("NOTION_API_KEY", "").strip()
DB_ID = os.getenv("NOTION_DATABASE_ID", "").strip()
RSS_FEEDS = [u.strip() for u in os.getenv("RSS_FEEDS", "").split(",") if u.strip()]
MAX_ITEMS = int(os.getenv("MAX_ITEMS_PER_FEED", "30"))
ALLOW_UPDATES = os.getenv("ALLOW_UPDATES", "true").lower() in {"1", "true", "yes"}
USER_AGENT = os.getenv("USER_AGENT", "notion-rss-bot/1.0 (+https://github.com/your/repo)")

if not (NOTION_KEY and DB_ID and RSS_FEEDS):
    raise SystemExit("Missing NOTION_API_KEY, NOTION_DATABASE_ID or RSS_FEEDS envs.")

notion = Client(auth=NOTION_KEY)


# ---------- Notion helpers ----------


def to_iso(dt_str: Optional[str]) -> Optional[str]:
    if not dt_str:
        return None
    try:
        return dateparser.parse(dt_str).isoformat()
    except Exception:
        return None


def build_properties(entry: Dict, source_name: str) -> Dict:
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
        "Summary": {"rich_text": [{"text": {"content": summary[:1900]}}]} if summary else {"rich_text": []},
        "Author": {"rich_text": [{"text": {"content": author[:200]}}]} if author else {"rich_text": []},
    }

    # Only include Tags if property exists; otherwise Notion errors
    try:
        # quick property presence probe
        db = notion.databases.retrieve(DB_ID)
        if "Tags" in db.get("properties", {}):
            props["Tags"] = {"multi_select": tags}
    except Exception:
        pass

    return props


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

    for entry in (parsed.entries or [])[:MAX_ITEMS]:
        # normalize essentials
        entry_link = entry.get("link") or ""
        if not entry_link:
            count_skipped += 1
            continue

        props = build_properties(entry, src)
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
