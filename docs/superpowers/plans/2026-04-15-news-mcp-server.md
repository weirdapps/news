# News MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the news pipeline's SQLite database via an MCP server so Claude Code sessions can query digest history, search articles, and retrieve synthesis intelligence across runs.

**Architecture:** A FastMCP server (`news/mcp_server.py`) reads from the existing `data/news.db`. It provides 4 tools: search articles by keyword, get recent digests with synthesis, query articles by category/pipeline/date, and get database stats. Registered globally in `~/.claude/settings.json` alongside the existing `second-brain` MCP server. Launched via a shell wrapper (`run_mcp.sh`) that activates the project venv — same pattern as second-brain.

**Tech Stack:** Python 3.12+, `mcp[cli]` (FastMCP), existing SQLite database (`news/storage.py`).

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `news/mcp_server.py` | Create | FastMCP server with 4 tools |
| `news/query.py` | Create | Query functions for MCP tools (search, digest history, stats) |
| `run_mcp.sh` | Create | Shell wrapper to launch MCP server with venv |
| `tests/test_query.py` | Create | Tests for query functions |
| `tests/test_mcp_server.py` | Create | Tests for MCP tool wiring |

No existing files are modified. The MCP server reads the same `data/news.db` the pipeline writes to.

---

### Task 1: Install `mcp` dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add mcp dependency**

Add `dependencies` to `pyproject.toml`:

```toml
[project]
name = "newsreader"
version = "0.1.0"
description = "Personal news intelligence platform with AI synthesis"
requires-python = ">=3.12"
dependencies = [
    "feedparser",
    "httpx",
    "trafilatura",
    "Jinja2",
    "PyYAML",
    "markupsafe",
    "mcp[cli]",
]
```

- [ ] **Step 2: Install into venv**

Run: `cd ~/news && .venv/bin/pip install -e .`
Expected: mcp package installs successfully

- [ ] **Step 3: Verify import works**

Run: `.venv/bin/python -c "from mcp.server.fastmcp import FastMCP; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "feat: add mcp dependency for news MCP server"
```

---

### Task 2: Build query functions

**Files:**
- Create: `news/query.py`
- Test: `tests/test_query.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_query.py`:

```python
"""Tests for news query functions."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from news.query import get_digest_history, get_news_stats, search_articles
from news.storage import get_connection, init_db, insert_article, insert_digest
from news.models import Article, Digest


@pytest.fixture
def db():
    """In-memory SQLite database with schema and sample data."""
    conn = get_connection(":memory:")
    init_db(conn)

    # Insert sample articles
    now = datetime.now(timezone.utc)
    articles = [
        Article(
            url="https://example.com/ecb-rates",
            title="ECB holds rates steady amid inflation concerns",
            source="Reuters",
            content="The European Central Bank decided to hold interest rates...",
            categories=["banking"],
            language="en",
            relevance_score=80,
            fetched_at=now - timedelta(hours=2),
            published_at=now - timedelta(hours=3),
            pipeline="digest",
        ),
        Article(
            url="https://example.com/nbg-results",
            title="NBG reports strong Q1 results",
            source="Kathimerini",
            content="National Bank of Greece announced quarterly results...",
            categories=["banking", "greece"],
            language="en",
            relevance_score=95,
            fetched_at=now - timedelta(hours=1),
            published_at=now - timedelta(hours=2),
            pipeline="digest",
        ),
        Article(
            url="https://example.com/claude-update",
            title="Claude Code adds new MCP features",
            source="TechCrunch",
            content="Anthropic released new features for Claude Code...",
            categories=["ai"],
            language="en",
            relevance_score=60,
            fetched_at=now - timedelta(hours=5),
            published_at=now - timedelta(hours=6),
            pipeline="digest",
        ),
        Article(
            url="https://example.com/nbg-monitor",
            title="NBG digital banking expansion",
            source="Capital.gr",
            content="NBG expands digital services...",
            categories=["banking"],
            language="el",
            relevance_score=70,
            fetched_at=now - timedelta(hours=1),
            published_at=now - timedelta(hours=2),
            pipeline="monitor",
        ),
    ]
    for article in articles:
        article.compute_hash()
        insert_article(conn, article)

    # Insert sample digests
    synthesis = {
        "executive_brief": [
            "ECB holds rates steady",
            "NBG reports strong Q1",
        ],
        "sections": [
            {
                "category": "banking",
                "display_name": "Banking & ECB",
                "synthesis": "ECB held rates...",
            }
        ],
    }
    digest = Digest(
        digest_type="scheduled",
        created_at=now - timedelta(hours=1),
        article_count=3,
        synthesis_text=json.dumps(synthesis),
        html_output="<html>...</html>",
        sent_at=now - timedelta(hours=1),
        pipeline="digest",
    )
    insert_digest(conn, digest)

    monitor_synthesis = {
        "executive_brief": ["NBG digital expansion noted"],
        "alerts": [],
    }
    monitor_digest = Digest(
        digest_type="scheduled",
        created_at=now - timedelta(hours=1),
        article_count=1,
        synthesis_text=json.dumps(monitor_synthesis),
        html_output="<html>monitor</html>",
        sent_at=now - timedelta(hours=1),
        pipeline="monitor",
    )
    insert_digest(conn, monitor_digest)

    yield conn
    conn.close()


class TestSearchArticles:
    def test_search_by_keyword(self, db):
        results = search_articles(db, query="ECB")
        assert len(results) >= 1
        assert any("ECB" in r["title"] for r in results)

    def test_search_by_keyword_case_insensitive(self, db):
        results = search_articles(db, query="ecb")
        assert len(results) >= 1

    def test_search_with_pipeline_filter(self, db):
        results = search_articles(db, query="NBG", pipeline="monitor")
        assert all(r["pipeline"] == "monitor" for r in results)

    def test_search_with_category_filter(self, db):
        results = search_articles(db, query="NBG", category="banking")
        assert len(results) >= 1
        assert all("banking" in r["categories"] for r in results)

    def test_search_with_days_filter(self, db):
        results = search_articles(db, query="ECB", days=1)
        assert len(results) >= 1

    def test_search_no_results(self, db):
        results = search_articles(db, query="cryptocurrency")
        assert results == []

    def test_search_respects_limit(self, db):
        results = search_articles(db, query="NBG", limit=1)
        assert len(results) <= 1


class TestGetDigestHistory:
    def test_get_digest_history_default(self, db):
        results = get_digest_history(db, pipeline="digest")
        assert len(results) >= 1
        assert "executive_brief" in results[0]
        assert "created_at" in results[0]

    def test_get_monitor_history(self, db):
        results = get_digest_history(db, pipeline="monitor")
        assert len(results) >= 1

    def test_get_digest_history_limit(self, db):
        results = get_digest_history(db, pipeline="digest", limit=1)
        assert len(results) <= 1

    def test_digest_excludes_html(self, db):
        results = get_digest_history(db, pipeline="digest")
        for r in results:
            assert "html_output" not in r


class TestGetNewsStats:
    def test_stats_returns_counts(self, db):
        result = get_news_stats(db)
        assert result["total_articles"] == 4
        assert result["total_digests"] == 2
        assert result["digest_articles"] == 3
        assert result["monitor_articles"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/news && .venv/bin/python -m pytest tests/test_query.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'news.query'`

- [ ] **Step 3: Write the query module**

Create `news/query.py`:

```python
"""Query functions for the news MCP server."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone


def search_articles(
    conn: sqlite3.Connection,
    query: str,
    pipeline: str | None = None,
    category: str | None = None,
    days: int = 30,
    limit: int = 20,
) -> list[dict]:
    """Search articles by keyword in title and content.

    Args:
        conn: SQLite connection
        query: Search keyword (matched against title and content)
        pipeline: Optional filter: 'digest' or 'monitor'
        category: Optional category filter
        days: Lookback period in days (default: 30)
        limit: Maximum results (default: 20)

    Returns:
        List of article dicts with title, source, url, categories, published_at, pipeline
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_str = since.isoformat()
    pattern = f"%{query}%"

    if category:
        sql = """
            SELECT DISTINCT a.url, a.title, a.source, a.published_at,
                   a.relevance_score, a.pipeline, a.fetched_at
            FROM articles a
            JOIN article_categories ac ON a.url = ac.article_url
            WHERE (a.title LIKE ? COLLATE NOCASE OR a.content LIKE ? COLLATE NOCASE)
              AND a.fetched_at >= ?
              AND ac.category = ?
        """
        params: list = [pattern, pattern, since_str, category]
    else:
        sql = """
            SELECT a.url, a.title, a.source, a.published_at,
                   a.relevance_score, a.pipeline, a.fetched_at
            FROM articles a
            WHERE (a.title LIKE ? COLLATE NOCASE OR a.content LIKE ? COLLATE NOCASE)
              AND a.fetched_at >= ?
        """
        params = [pattern, pattern, since_str]

    if pipeline:
        sql += " AND a.pipeline = ?"
        params.append(pipeline)

    sql += " ORDER BY a.fetched_at DESC LIMIT ?"
    params.append(limit)

    cursor = conn.execute(sql, params)
    rows = cursor.fetchall()

    results = []
    for row in rows:
        # Load categories for this article
        cat_cursor = conn.execute(
            "SELECT category FROM article_categories WHERE article_url = ?",
            (row["url"],),
        )
        categories = [c["category"] for c in cat_cursor.fetchall()]

        results.append({
            "title": row["title"],
            "source": row["source"],
            "url": row["url"],
            "published_at": row["published_at"],
            "relevance_score": row["relevance_score"],
            "pipeline": row["pipeline"],
            "categories": categories,
        })

    return results


def get_digest_history(
    conn: sqlite3.Connection,
    pipeline: str = "digest",
    days: int = 7,
    limit: int = 10,
) -> list[dict]:
    """Get recent digest/monitor synthesis history.

    Returns the structured synthesis (executive briefs, sections) without
    the bulky HTML output.

    Args:
        conn: SQLite connection
        pipeline: 'digest' or 'monitor' (default: 'digest')
        days: Lookback period in days (default: 7)
        limit: Maximum results (default: 10)

    Returns:
        List of digest dicts with created_at, article_count, executive_brief, sections
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_str = since.isoformat()

    cursor = conn.execute(
        """
        SELECT id, digest_type, created_at, article_count, synthesis_text, sent_at, pipeline
        FROM digests
        WHERE pipeline = ? AND created_at >= ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (pipeline, since_str, limit),
    )

    results = []
    for row in cursor.fetchall():
        entry = {
            "id": row["id"],
            "digest_type": row["digest_type"],
            "created_at": row["created_at"],
            "article_count": row["article_count"],
            "sent_at": row["sent_at"],
            "pipeline": row["pipeline"],
        }

        # Parse synthesis JSON to extract structured data
        if row["synthesis_text"]:
            try:
                synthesis = json.loads(row["synthesis_text"])
                entry["executive_brief"] = synthesis.get("executive_brief", [])
                entry["what_changed"] = synthesis.get("what_changed", "")
                entry["sections"] = synthesis.get("sections", [])
                # Monitor-specific fields
                if pipeline == "monitor":
                    entry["alerts"] = synthesis.get("alerts", [])
                    entry["sentiment_summary"] = synthesis.get("sentiment_summary")
            except json.JSONDecodeError:
                entry["executive_brief"] = []
                entry["raw_text"] = row["synthesis_text"][:500]

        results.append(entry)

    return results


def get_news_stats(conn: sqlite3.Connection) -> dict:
    """Get database statistics.

    Returns:
        Dict with total_articles, total_digests, digest_articles, monitor_articles,
        categories, sources, date_range
    """
    stats = {}

    row = conn.execute("SELECT COUNT(*) as cnt FROM articles").fetchone()
    stats["total_articles"] = row["cnt"]

    row = conn.execute("SELECT COUNT(*) as cnt FROM digests").fetchone()
    stats["total_digests"] = row["cnt"]

    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM articles WHERE pipeline = 'digest'"
    ).fetchone()
    stats["digest_articles"] = row["cnt"]

    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM articles WHERE pipeline = 'monitor'"
    ).fetchone()
    stats["monitor_articles"] = row["cnt"]

    # Category distribution
    cat_rows = conn.execute(
        "SELECT category, COUNT(*) as cnt FROM article_categories GROUP BY category ORDER BY cnt DESC"
    ).fetchall()
    stats["categories"] = {r["category"]: r["cnt"] for r in cat_rows}

    # Source distribution (top 10)
    src_rows = conn.execute(
        "SELECT source, COUNT(*) as cnt FROM articles GROUP BY source ORDER BY cnt DESC LIMIT 10"
    ).fetchall()
    stats["top_sources"] = {r["source"]: r["cnt"] for r in src_rows}

    # Date range
    row = conn.execute(
        "SELECT MIN(fetched_at) as earliest, MAX(fetched_at) as latest FROM articles"
    ).fetchone()
    stats["earliest_article"] = row["earliest"]
    stats["latest_article"] = row["latest"]

    return stats
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/news && .venv/bin/python -m pytest tests/test_query.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add news/query.py tests/test_query.py
git commit -m "feat: add query functions for news MCP server"
```

---

### Task 3: Build the MCP server

**Files:**
- Create: `news/mcp_server.py`
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mcp_server.py`:

```python
"""Tests for news MCP server tool wiring."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from news.models import Article, Digest
from news.storage import get_connection, init_db, insert_article, insert_digest


@pytest.fixture
def populated_db(tmp_path):
    """Create a populated database file for MCP tests."""
    db_path = tmp_path / "news.db"
    conn = get_connection(str(db_path))
    init_db(conn)

    now = datetime.now(timezone.utc)
    article = Article(
        url="https://example.com/test-article",
        title="ECB rate decision impacts Greek banks",
        source="Reuters",
        content="The European Central Bank announced...",
        categories=["banking"],
        language="en",
        relevance_score=85,
        fetched_at=now,
        published_at=now - timedelta(hours=1),
        pipeline="digest",
    )
    article.compute_hash()
    insert_article(conn, article)

    synthesis = {
        "executive_brief": ["ECB held rates steady"],
        "sections": [],
    }
    digest = Digest(
        digest_type="scheduled",
        created_at=now,
        article_count=1,
        synthesis_text=json.dumps(synthesis),
        html_output="<html>test</html>",
        sent_at=now,
        pipeline="digest",
    )
    insert_digest(conn, digest)
    conn.close()

    return str(db_path)


class TestMcpTools:
    def test_search_news_returns_results(self, populated_db):
        with patch("news.mcp_server._DB_PATH", populated_db):
            from news.mcp_server import search_news

            results = search_news(query="ECB")
            assert len(results) >= 1
            assert "ECB" in results[0]["title"]

    def test_digest_history_returns_briefs(self, populated_db):
        with patch("news.mcp_server._DB_PATH", populated_db):
            from news.mcp_server import digest_history

            results = digest_history()
            assert len(results) >= 1
            assert "executive_brief" in results[0]

    def test_news_stats_returns_counts(self, populated_db):
        with patch("news.mcp_server._DB_PATH", populated_db):
            from news.mcp_server import news_stats

            result = news_stats()
            assert result["total_articles"] >= 1
            assert result["total_digests"] >= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/news && .venv/bin/python -m pytest tests/test_mcp_server.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'news.mcp_server'`

- [ ] **Step 3: Write the MCP server**

Create `news/mcp_server.py`:

```python
"""MCP server for the news intelligence platform.

Exposes the news article database and digest synthesis history as MCP tools
for querying from Claude Code sessions.

Run: python -m news.mcp_server
"""

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from news.storage import get_connection, init_db

# Default database path — same as pipeline uses
_DB_PATH = str(Path(__file__).parent.parent / "data" / "news.db")

mcp = FastMCP(
    "news-reader",
    instructions=(
        "News intelligence platform — search articles from digest and NBG monitor "
        "pipelines, retrieve AI-curated synthesis history, and query article database. "
        "Digest runs 4x daily (09:00, 13:00, 17:00, 21:00 Athens). "
        "Monitor runs bi-hourly for NBG brand mentions."
    ),
)


def _get_conn():
    """Get a database connection with row factory."""
    conn = get_connection(_DB_PATH)
    init_db(conn)
    return conn


@mcp.tool()
def search_news(
    query: str,
    pipeline: str | None = None,
    category: str | None = None,
    days: int = 30,
    limit: int = 20,
) -> list[dict]:
    """Search news articles by keyword across title and content.

    Args:
        query: Search keyword (case-insensitive)
        pipeline: Filter by pipeline — 'digest' or 'monitor' (default: both)
        category: Filter by category — banking, greece, ai, tech, etc.
        days: Lookback period in days (default: 30)
        limit: Maximum results (default: 20)
    """
    from news.query import search_articles

    conn = _get_conn()
    try:
        return search_articles(
            conn, query=query, pipeline=pipeline, category=category,
            days=days, limit=limit,
        )
    finally:
        conn.close()


@mcp.tool()
def digest_history(
    pipeline: str = "digest",
    days: int = 7,
    limit: int = 10,
) -> list[dict]:
    """Get recent digest or monitor synthesis history.

    Returns AI-curated executive briefs and section syntheses from past runs.
    Use this to track how news narratives evolve over days.

    Args:
        pipeline: 'digest' for news digests, 'monitor' for NBG brand monitoring (default: digest)
        days: Lookback period in days (default: 7)
        limit: Maximum digests to return (default: 10)
    """
    from news.query import get_digest_history

    conn = _get_conn()
    try:
        return get_digest_history(conn, pipeline=pipeline, days=days, limit=limit)
    finally:
        conn.close()


@mcp.tool()
def news_stats() -> dict:
    """Get news database statistics: article counts, category distribution, source distribution, date range."""
    from news.query import get_news_stats

    conn = _get_conn()
    try:
        return get_news_stats(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/news && .venv/bin/python -m pytest tests/test_mcp_server.py -v`
Expected: All tests PASS

- [ ] **Step 5: Run all tests to check for regressions**

Run: `cd ~/news && .venv/bin/python -m pytest -v`
Expected: All existing tests still PASS

- [ ] **Step 6: Commit**

```bash
git add news/mcp_server.py tests/test_mcp_server.py
git commit -m "feat: add MCP server exposing news database as 3 tools"
```

---

### Task 4: Create the launch wrapper and register the MCP server

**Files:**
- Create: `run_mcp.sh`

- [ ] **Step 1: Create the shell wrapper**

Create `run_mcp.sh`:

```bash
#!/bin/bash
cd ~/news
exec ~/news/.venv/bin/python -m news.mcp_server
```

- [ ] **Step 2: Make it executable**

Run: `chmod +x ~/news/run_mcp.sh`

- [ ] **Step 3: Test the server starts**

Run: `cd ~/news && echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}' | timeout 5 .venv/bin/python -m news.mcp_server 2>/dev/null | head -1`
Expected: JSON response with server capabilities (confirms the server boots and responds to MCP protocol)

- [ ] **Step 4: Register in Claude Code settings**

The user needs to add the MCP server to their global settings. The registration goes in `~/.claude/settings.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "news-reader": {
      "type": "stdio",
      "command": "~/news/run_mcp.sh",
      "args": [],
      "env": {}
    }
  }
}
```

**Note:** Since the user's `settings.json` currently has no `mcpServers` key (it was previously backed up to `claude-config/backups/mcp-servers.json`), this key needs to be added. Check the current state of settings.json before editing to avoid overwriting existing keys.

- [ ] **Step 5: Commit**

```bash
git add run_mcp.sh
git commit -m "feat: add MCP launch wrapper"
```

---

### Task 5: Add FTS5 index for faster search (enhancement)

The current `search_articles` uses `LIKE` which scans all rows. Add an FTS5 virtual table for fast full-text search on article titles and content.

**Files:**
- Modify: `news/storage.py` (add FTS5 table + triggers to `init_db`)
- Modify: `news/query.py` (use FTS5 in `search_articles`)
- Modify: `tests/test_query.py` (update search tests)

- [ ] **Step 1: Update the failing test**

Add to `tests/test_query.py` in `TestSearchArticles`:

```python
    def test_search_finds_content_match(self, db):
        """FTS should find matches in content, not just title."""
        results = search_articles(db, query="quarterly results")
        assert len(results) >= 1
        assert any("NBG" in r["title"] for r in results)
```

- [ ] **Step 2: Run test to verify it passes with LIKE (baseline)**

Run: `cd ~/news && .venv/bin/python -m pytest tests/test_query.py::TestSearchArticles::test_search_finds_content_match -v`
Expected: PASS (LIKE already searches content)

- [ ] **Step 3: Add FTS5 table to storage.py**

In `news/storage.py`, add FTS5 creation inside `init_db()` after the existing `CREATE INDEX` statements, before `conn.commit()`:

```python
    # FTS5 index for full-text search on articles
    conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
            title, content, source,
            content='articles',
            content_rowid='rowid'
        );

        CREATE TRIGGER IF NOT EXISTS articles_fts_insert AFTER INSERT ON articles BEGIN
            INSERT INTO articles_fts(rowid, title, content, source)
            VALUES (NEW.rowid, NEW.title, NEW.content, NEW.source);
        END;

        CREATE TRIGGER IF NOT EXISTS articles_fts_delete AFTER DELETE ON articles BEGIN
            INSERT INTO articles_fts(articles_fts, rowid, title, content, source)
            VALUES ('delete', OLD.rowid, OLD.title, OLD.content, OLD.source);
        END;
    """)
```

Also add a backfill function at module level:

```python
def _backfill_fts(conn: sqlite3.Connection) -> None:
    """Backfill FTS5 index from existing articles (idempotent)."""
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM articles_fts"
    ).fetchone()
    if row["cnt"] == 0:
        article_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM articles"
        ).fetchone()["cnt"]
        if article_count > 0:
            conn.execute("""
                INSERT INTO articles_fts(rowid, title, content, source)
                SELECT rowid, title, content, source FROM articles
            """)
            conn.commit()
```

Call `_backfill_fts(conn)` at the end of `init_db()`.

- [ ] **Step 4: Update search_articles to use FTS5**

In `news/query.py`, replace the `search_articles` function's SQL to use FTS5 with a LIKE fallback:

```python
def search_articles(
    conn: sqlite3.Connection,
    query: str,
    pipeline: str | None = None,
    category: str | None = None,
    days: int = 30,
    limit: int = 20,
) -> list[dict]:
    """Search articles by keyword using FTS5 full-text search.

    Args:
        conn: SQLite connection
        query: Search keyword (FTS5 syntax supported)
        pipeline: Optional filter: 'digest' or 'monitor'
        category: Optional category filter
        days: Lookback period in days (default: 30)
        limit: Maximum results (default: 20)

    Returns:
        List of article dicts with title, source, url, categories, published_at, pipeline
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_str = since.isoformat()

    # Use FTS5 for search
    if category:
        sql = """
            SELECT DISTINCT a.url, a.title, a.source, a.published_at,
                   a.relevance_score, a.pipeline, a.fetched_at
            FROM articles a
            JOIN articles_fts fts ON a.rowid = fts.rowid
            JOIN article_categories ac ON a.url = ac.article_url
            WHERE articles_fts MATCH ?
              AND a.fetched_at >= ?
              AND ac.category = ?
        """
        params: list = [query, since_str, category]
    else:
        sql = """
            SELECT a.url, a.title, a.source, a.published_at,
                   a.relevance_score, a.pipeline, a.fetched_at
            FROM articles a
            JOIN articles_fts fts ON a.rowid = fts.rowid
            WHERE articles_fts MATCH ?
              AND a.fetched_at >= ?
        """
        params = [query, since_str]

    if pipeline:
        sql += " AND a.pipeline = ?"
        params.append(pipeline)

    sql += " ORDER BY a.fetched_at DESC LIMIT ?"
    params.append(limit)

    cursor = conn.execute(sql, params)
    rows = cursor.fetchall()

    results = []
    for row in rows:
        cat_cursor = conn.execute(
            "SELECT category FROM article_categories WHERE article_url = ?",
            (row["url"],),
        )
        categories = [c["category"] for c in cat_cursor.fetchall()]

        results.append({
            "title": row["title"],
            "source": row["source"],
            "url": row["url"],
            "published_at": row["published_at"],
            "relevance_score": row["relevance_score"],
            "pipeline": row["pipeline"],
            "categories": categories,
        })

    return results
```

- [ ] **Step 5: Run all tests**

Run: `cd ~/news && .venv/bin/python -m pytest -v`
Expected: All tests PASS

- [ ] **Step 6: Backfill existing database**

Run: `cd ~/news && .venv/bin/python -c "from news.storage import get_connection, init_db; conn = get_connection('data/news.db'); init_db(conn); print('FTS backfill complete'); conn.close()"`
Expected: `FTS backfill complete`

- [ ] **Step 7: Commit**

```bash
git add news/storage.py news/query.py tests/test_query.py
git commit -m "feat: add FTS5 full-text search index for articles"
```

---

## Post-Implementation Verification

After all tasks are complete:

1. **Restart Claude Code** to pick up the new MCP server registration
2. **Verify the server appears** in the MCP tools list (should show `news-reader` with 3 tools)
3. **Test a live query**: ask Claude Code "search news for ECB" — it should use the `search_news` tool
4. **Test digest history**: ask "what were the last 3 digest executive briefs?" — should use `digest_history`
5. **Test stats**: ask "how many articles are in the news database?" — should use `news_stats`
