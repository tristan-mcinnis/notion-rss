# Notion RSS Sync

Import items from one or more RSS feeds into a Notion database with idempotent upserts and a ready-to-run GitHub Actions workflow.

## Features

- ✅ Multi-feed ingestion with polite pacing to respect rate limits.
- ✅ Idempotent upsert keyed by the Notion `URL` property with optional update suppression.
- ✅ Tag extraction from RSS `<category>` values when the Notion database exposes a `Tags` multi-select field.
- ✅ Optional full-article extraction converted to Markdown (via Readability + `html2markdown`) for richer Notion records.
- ✅ Works great on macOS: the repo ships with a simple `python3` workflow that runs locally or in GitHub Actions without extra tooling.

## Notion database setup

Create (or reuse) a Notion database with at least the following properties:

| Property name | Type        | Notes                                 |
| ------------- | ----------- | ------------------------------------- |
| Title         | Title       | Feed item title (fallback to link).   |
| URL           | URL         | Used as the unique identifier.        |
| Published     | Date        | Automatically parsed if available.    |
| Source        | Select      | Populated with the feed title/domain. |
| Summary       | Rich text   | Raw summary text from the feed.       |
| Content       | Rich text   | Optional; Markdown full article text (defaults to `Content`). |
| Author        | Rich text   | Optional author field.                |
| Tags          | Multi-select| Optional; filled from feed categories.|

> ℹ️ You can extend `build_properties()` in [`src/rss_to_notion.py`](src/rss_to_notion.py) to map additional database properties.

Enable the full-text feature by adding a rich-text property (default name `Content`) to your database. The property name can be customised via `ARTICLE_CONTENT_PROPERTY` if you prefer a different label.

Share the database with your Notion integration: open the database → **Share** → invite the integration you created in [Notion developer settings](https://www.notion.so/my-integrations).

## Configuration

Set the following secrets in **GitHub → Settings → Secrets and variables → Actions** (or export them locally when testing):

| Secret | Required | Description |
| ------ | -------- | ----------- |
| `NOTION_API_KEY` | ✅ | Internal integration key copied from Notion. |
| `NOTION_DATABASE_ID` | ✅ | Short database ID (the characters between the last slash and `?` in the URL). |
| `RSS_FEEDS` | ✅ | Comma-separated list of RSS/Atom feed URLs. |
| `MAX_ITEMS_PER_FEED` | ❌ | Limit items processed per feed (default `30`). |
| `ALLOW_UPDATES` | ❌ | `true` (default) to update existing pages, `false` for create-only mode. |
| `USER_AGENT` | ❌ | Custom HTTP user agent string for feed requests. |
| `FETCH_FULL_CONTENT` | ❌ | `true` (default) to fetch and store article bodies using Readability. |
| `ARTICLE_CONTENT_PROPERTY` | ❌ | Notion rich-text property used to store Markdown full text (default `Content`). |
| `ARTICLE_FETCH_TIMEOUT` | ❌ | Timeout in seconds for article download requests (default `10`). |

## Local development on macOS

1. Install Python 3.11 (macOS ships with `python3`, but you can also use [Homebrew](https://brew.sh/) to install a newer version).
2. Clone the repository and set the required environment variables in your shell:

   ```bash
   export NOTION_API_KEY=... \
          NOTION_DATABASE_ID=... \
          RSS_FEEDS="https://news.ycombinator.com/rss,https://www.xinhuanet.com/english/rss/worldrss.xml"
   ```

3. Create and activate a virtual environment:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate  # on macOS/Linux shells
   ```

4. Install dependencies and run the ingester:

   ```bash
   pip install -r requirements.txt
   python src/rss_to_notion.py
   ```

The script prints a summary for each feed (`new`, `updated`, `skipped`) followed by a total count of pages upserted.

## GitHub Actions automation

The repository includes [`.github/workflows/rss-to-notion.yml`](.github/workflows/rss-to-notion.yml) which:

- Runs on demand (`workflow_dispatch`) or every 30 minutes via cron.
- Uses Python 3.11 on `ubuntu-latest` runners.
- Installs requirements and invokes `python src/rss_to_notion.py` with the configured secrets.

Once secrets are configured, enable the workflow and watch the Action logs for ingestion summaries.

## Extending the flow

- **Tag strategy** – Apply fixed tags per feed by augmenting `build_properties()` with a lookup dictionary.
- **HTML handling** – Feed summaries may contain HTML. Integrate `beautifulsoup4` to strip or transform markup before storing it in Notion.
- **Full-text extraction** – Use readability libraries or paid extraction services to populate a richer property.
- **Backfill** – Temporarily raise `MAX_ITEMS_PER_FEED` to reprocess older entries.
- **Create-only mode** – Set `ALLOW_UPDATES=false` to avoid updating existing pages.

Happy automating! 🚀
