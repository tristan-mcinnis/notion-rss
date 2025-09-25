#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional

import feedparser
from notion_client import Client
from dateutil import parser as dateparser
import backoff
import requests
try:
    from readability import Document  # type: ignore
except Exception as exc:  # pragma: no cover - import-time dependency check
    Document = None  # type: ignore[assignment]
    _READABILITY_IMPORT_ERROR = exc
else:
    _READABILITY_IMPORT_ERROR = None
from markdownify import markdownify as html_to_markdown
from bs4 import BeautifulSoup

NOTION_KEY = os.getenv("NOTION_API_KEY", "").strip()
DB_ID = os.getenv("NOTION_DATABASE_ID", "").strip()
RSS_FEEDS = [u.strip() for u in os.getenv("RSS_FEEDS", "").split(",") if u.strip()]


def _int_from_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw_stripped = raw.strip()
    if not raw_stripped:
        return default
    try:
        return int(raw_stripped)
    except ValueError:
        print(f"[warn] invalid value for {name}={raw!r}; using {default}")
        return default


MAX_ITEMS = _int_from_env("MAX_ITEMS_PER_FEED", 30)
ALLOW_UPDATES = os.getenv("ALLOW_UPDATES", "true").lower() in {"1", "true", "yes"}
USER_AGENT = os.getenv("USER_AGENT", "notion-rss-bot/1.0 (+https://github.com/your/repo)")
FETCH_FULL_CONTENT = os.getenv("FETCH_FULL_CONTENT", "true").lower() in {"1", "true", "yes"}
ARTICLE_FETCH_TIMEOUT = float(os.getenv("ARTICLE_FETCH_TIMEOUT", "10"))
INCLUDE_SUMMARY = os.getenv("INCLUDE_SUMMARY", "false").lower() in {"1", "true", "yes"}

if not (NOTION_KEY and DB_ID and RSS_FEEDS):
    raise SystemExit("Missing NOTION_API_KEY, NOTION_DATABASE_ID or RSS_FEEDS envs.")

notion = Client(auth=NOTION_KEY)

session = requests.Session()
if USER_AGENT:
    session.headers.update({"User-Agent": USER_AGENT})


_READABILITY_IMPORT_WARNED = False


def clean_html_for_markdown(html: str) -> str:
    """Clean HTML before converting to markdown - remove data URLs, scripts, styles, etc."""
    if not html:
        return html

    try:
        soup = BeautifulSoup(html, 'html.parser')

        # Remove script and style elements
        for script in soup(["script", "style"]):
            script.decompose()

        # Remove images with data URLs (base64 encoded)
        for img in soup.find_all('img'):
            src = img.get('src', '')
            if src.startswith('data:'):
                # Replace with alt text or remove
                alt_text = img.get('alt', '')
                if alt_text:
                    img.replace_with(f"[Image: {alt_text}]")
                else:
                    img.decompose()

        # Remove iframes
        for iframe in soup.find_all('iframe'):
            iframe.decompose()

        # Convert back to string
        return str(soup)
    except:
        # If parsing fails, return original
        return html


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


def to_rich_text(text: str, chunk_size: int = 1900) -> List[Dict[str, Any]]:
    return [
        {"type": "text", "text": {"content": chunk}}
        for chunk in chunk_text(text, chunk_size)
    ]


def _paragraph_block(text: str) -> Dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": to_rich_text(text)},
    }


def _heading_block(level: int, text: str) -> Dict:
    level = min(max(level, 1), 3)
    block_type = f"heading_{level}"
    return {
        "object": "block",
        "type": block_type,
        block_type: {"rich_text": to_rich_text(text)},
    }


def _list_item_block(block_type: str, text: str) -> Dict:
    return {
        "object": "block",
        "type": block_type,
        block_type: {"rich_text": to_rich_text(text)},
    }


def _quote_block(text: str) -> Dict:
    return {
        "object": "block",
        "type": "quote",
        "quote": {"rich_text": to_rich_text(text)},
    }


def _code_block(text: str, language: str) -> Dict:
    return {
        "object": "block",
        "type": "code",
        "code": {"rich_text": to_rich_text(text), "language": language or "plain text"},
    }


def markdown_to_blocks(markdown: str) -> List[Dict]:
    if not markdown:
        return []

    blocks: List[Dict] = []
    paragraph_buffer: List[str] = []
    code_buffer: List[str] = []
    in_code = False
    code_language = "plain text"

    def flush_paragraph() -> None:
        if not paragraph_buffer:
            return
        paragraph_text = " ".join(paragraph_buffer).strip()
        paragraph_buffer.clear()
        if paragraph_text:
            blocks.append(_paragraph_block(paragraph_text))

    def flush_code() -> None:
        nonlocal in_code, code_buffer
        if not code_buffer:
            in_code = False
            return
        code_text = "\n".join(code_buffer).rstrip("\n")
        code_buffer = []
        in_code = False
        if code_text:
            blocks.append(_code_block(code_text, code_language))

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip("\n")

        if in_code:
            if line.strip().startswith("```"):
                flush_code()
            else:
                code_buffer.append(raw_line)
            continue

        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            continue

        if stripped.startswith("```"):
            flush_paragraph()
            in_code = True
            code_language = stripped.strip("`").strip() or "plain text"
            code_buffer = []
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading_match:
            flush_paragraph()
            level = min(len(heading_match.group(1)), 3)
            content = heading_match.group(2).strip()
            if content:
                blocks.append(_heading_block(level, content))
            continue

        if re.match(r"^[-*]\s+", stripped):
            flush_paragraph()
            content = re.sub(r"^[-*]\s+", "", stripped).strip()
            if content:
                blocks.append(_list_item_block("bulleted_list_item", content))
            continue

        if re.match(r"^\d+\.\s+", stripped):
            flush_paragraph()
            content = re.sub(r"^\d+\.\s+", "", stripped).strip()
            if content:
                blocks.append(_list_item_block("numbered_list_item", content))
            continue

        if stripped.startswith(">"):
            flush_paragraph()
            content = stripped.lstrip(">").strip()
            if content:
                blocks.append(_quote_block(content))
            continue

        paragraph_buffer.append(stripped)

    if in_code:
        flush_code()
    flush_paragraph()

    if not blocks:
        blocks.append(_paragraph_block(markdown.strip()))

    return blocks


def build_properties(entry: Dict, source_name: str) -> Dict:
    title = entry.get("title") or entry.get("link") or "Untitled"
    url = entry.get("link") or ""
    published = (
        to_iso(entry.get("published"))
        or to_iso(entry.get("updated"))
        or to_iso(entry.get("created"))
    )

    # Build base properties
    props = {
        "Title": {"title": [{"text": {"content": title[:200]}}]},
        "URL": {"url": url},
        "Published": {"date": {"start": published} if published else None},
        "Source": {"select": {"name": source_name[:90]}},
    }

    # Only include summary if enabled (disabled by default since full content is in page)
    if INCLUDE_SUMMARY:
        summary_raw = entry.get("summary") or entry.get("subtitle") or ""
        summary = ""
        if summary_raw:
            try:
                # Convert HTML to clean text for the summary property
                soup = BeautifulSoup(summary_raw, 'html.parser')
                summary = soup.get_text(separator=' ', strip=True)
                # Keep summary brief - just first 200 chars as a preview
                if len(summary) > 200:
                    # Try to break at word boundary
                    summary = summary[:197].rsplit(' ', 1)[0] + "..."
            except:
                # If parsing fails, try to strip basic HTML tags
                import re
                summary = re.sub('<[^<]+?>', '', summary_raw).strip()
                if len(summary) > 200:
                    summary = summary[:197].rsplit(' ', 1)[0] + "..."

        props["Summary"] = {"rich_text": to_rich_text(summary)} if summary else {"rich_text": []}

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
        print(f"[info] Fetching article from: {url}")
        resp = session.get(url, timeout=ARTICLE_FETCH_TIMEOUT)
        resp.raise_for_status()
        if not resp.encoding:
            resp.encoding = resp.apparent_encoding
        print(f"[info] Successfully fetched {len(resp.text)} characters from {url}")
        return resp.text
    except requests.RequestException as exc:
        print(f"[warn] Unable to fetch article content from {url}: {exc}")
        return None


def extract_article_markdown(entry: Dict) -> Optional[str]:
    url = entry.get("link") or ""

    # Try to fetch and extract full article content first
    if url:
        html = fetch_article_html(url)
        if html:
            if Document is None:
                global _READABILITY_IMPORT_WARNED
                if _READABILITY_IMPORT_ERROR and not _READABILITY_IMPORT_WARNED:
                    print(
                        "[warn] readability library unavailable (missing dependency?). "
                        "Install 'lxml-html-clean' to enable full-article extraction."
                    )
                    _READABILITY_IMPORT_WARNED = True
            else:
                try:
                    print(f"[info] Extracting article content with readability for: {entry.get('title', 'Untitled')[:50]}...")
                    doc = Document(html)
                    article_html = doc.summary()
                    if article_html:
                        cleaned_html = clean_html_for_markdown(article_html)
                        markdown = html_to_markdown(cleaned_html, heading_style="ATX", strip=['img']).strip()
                        if markdown:
                            print(f"[info] Successfully extracted {len(markdown)} characters of content")
                            return markdown
                except Exception as exc:
                    print(f"[warn] Readability extraction failed for {url}: {exc}")

    # Fall back to RSS content if full article extraction failed
    print(f"[info] Falling back to RSS content for: {entry.get('title', 'Untitled')[:50]}...")
    for candidate in _normalize_html_value(entry):
        try:
            cleaned_html = clean_html_for_markdown(candidate)
            markdown = html_to_markdown(cleaned_html, heading_style="ATX", strip=['img']).strip()
            if markdown:
                print(f"[info] Converted RSS HTML to {len(markdown)} characters of markdown")
                return markdown
        except Exception as e:
            print(f"[warn] Failed to convert RSS HTML: {e}")
            continue

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


def _chunk_blocks(blocks: List[Dict], size: int = 50) -> Iterable[List[Dict]]:
    for idx in range(0, len(blocks), size):
        yield blocks[idx : idx + size]


@backoff.on_exception(backoff.expo, Exception, max_tries=5, jitter=backoff.full_jitter)
def replace_page_children(page_id: str, children: List[Dict]) -> None:
    existing: List[Dict] = []
    cursor: Optional[str] = None
    while True:
        resp = notion.blocks.children.list(block_id=page_id, start_cursor=cursor)
        existing.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")

    for child in existing:
        child_type = child.get("type")
        if child_type in {"child_database", "child_page"}:
            continue
        try:
            notion.blocks.delete(block_id=child["id"])
        except Exception as exc:
            print(f"[warn] unable to remove block {child.get('id')}: {exc}")

    if not children:
        return

    for chunk in _chunk_blocks(children, size=50):
        notion.blocks.children.append(block_id=page_id, children=chunk)


@backoff.on_exception(backoff.expo, Exception, max_tries=5, jitter=backoff.full_jitter)
def create_page(props: Dict, children: Optional[List[Dict]] = None):
    payload: Dict = {"parent": {"database_id": DB_ID}, "properties": props}
    if children:
        payload["children"] = children
    return notion.pages.create(**payload)


@backoff.on_exception(backoff.expo, Exception, max_tries=5, jitter=backoff.full_jitter)
def update_page(page_id: str, props: Dict, children: Optional[List[Dict]] = None):
    notion.pages.update(page_id=page_id, properties=props)
    if children is not None:
        replace_page_children(page_id, children)


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
    should_fetch_content = FETCH_FULL_CONTENT

    for entry in (parsed.entries or [])[:MAX_ITEMS]:
        # normalize essentials
        entry_link = entry.get("link") or ""
        if not entry_link:
            count_skipped += 1
            continue

        # Extract article content
        article_markdown = None
        if should_fetch_content:
            article_markdown = extract_article_markdown(entry)
            if article_markdown:
                print(f"[info] Extracted full article content for: {entry.get('title', 'Untitled')[:50]}...")

        # Fallback to RSS summary if no full content was extracted
        if not article_markdown:
            print(f"[info] Using RSS content for: {entry.get('title', 'Untitled')[:50]}...")
            # Try to get content from multiple sources in the RSS entry
            article_markdown = None

            # First try the content field if available
            content = entry.get("content")
            if content and isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and "value" in item:
                        try:
                            cleaned_html = clean_html_for_markdown(item["value"])
                            article_markdown = html_to_markdown(cleaned_html, heading_style="ATX", strip=['img']).strip()
                            if article_markdown:
                                break
                        except Exception as e:
                            print(f"[warn] Failed to convert content HTML: {e}")

            # If no content field, try summary
            if not article_markdown:
                summary = entry.get("summary") or entry.get("subtitle") or ""
                if summary:
                    try:
                        cleaned_html = clean_html_for_markdown(summary)
                        article_markdown = html_to_markdown(cleaned_html, heading_style="ATX", strip=['img']).strip()
                    except Exception as e:
                        print(f"[warn] Failed to convert summary HTML: {e}")
                        # Last resort - strip HTML tags manually
                        import re
                        article_markdown = re.sub('<[^<]+?>', '', summary).strip()

            # If still no content, use title and link
            if not article_markdown:
                title = entry.get("title") or "Untitled"
                article_markdown = f"# {title}\n\n[Read full article]({entry_link})"

        props = build_properties(entry, src)
        article_blocks = markdown_to_blocks(article_markdown) if article_markdown else []
        existing_id = query_page_by_url(entry_link)

        if existing_id is None:
            create_page(props, article_blocks)
            count_new += 1
        else:
            if ALLOW_UPDATES:
                update_page(existing_id, props, article_blocks)
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
