"""MCP server for the news intelligence platform.

Exposes the news article database and digest synthesis history as MCP tools
for querying from Claude Code sessions.

Run: python -m news.mcp_server
"""

import sqlite3
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from news import mcp_result
from news.storage import get_connection, init_db

# Default database path — same as pipeline uses
_DB_PATH = str(Path(__file__).parent.parent / "data" / "news.db")

# mcp 2.x renamed FastMCP -> MCPServer and moved mcp.server.fastmcp ->
# mcp.server.mcpserver. Keep `instructions` a keyword: v2 inserted `title` and
# `description` as the 2nd/3rd positional parameters.
mcp = MCPServer(
    "news-reader",
    instructions=(
        "News intelligence platform — search articles from digest and brand monitor "
        "pipelines, retrieve AI-curated synthesis history, and query article database. "
        "Digest runs 5x daily (00:00, 09:00, 13:00, 17:00, 21:00 Athens). "
        "Monitor runs bi-hourly during business hours (08:00–22:00 Athens) "
        "plus a 00:00 catch-up for brand mentions."
    ),
)


def _get_conn():
    """Get a database connection with row factory."""
    try:
        conn = get_connection(_DB_PATH)
        init_db(conn)
        return conn
    except sqlite3.Error as exc:
        raise mcp_result.Unavailable(
            f"news database unavailable at {_DB_PATH}: {exc}",
            remediation="Run the news pipeline to create data/news.db",
        ) from exc


@mcp.tool()
@mcp_result.tool
def search_news(
    query: str,
    pipeline: str | None = None,
    category: str | None = None,
    ticker: str | None = None,
    days: int = 30,
    limit: int = 20,
) -> list[dict]:
    """Search news articles by keyword across title and content.

    Args:
        query: Search keyword (case-insensitive)
        pipeline: Filter by pipeline — 'digest' or 'monitor' (default: both)
        category: Filter by category — banking, greece, ai, tech, etc.
        ticker: Filter by stock ticker symbol (e.g., 'AAPL', 'MSFT')
        days: Lookback period in days (default: 30)
        limit: Maximum results (default: 20)
    """
    from news.query import search_articles

    conn = _get_conn()
    try:
        return search_articles(
            conn,
            query=query,
            pipeline=pipeline,
            category=category,
            ticker=ticker,
            days=days,
            limit=limit,
        )
    finally:
        conn.close()


@mcp.tool()
@mcp_result.tool
def digest_history(
    pipeline: str = "digest",
    days: int = 7,
    limit: int = 10,
) -> list[dict]:
    """Get recent digest or monitor synthesis history.

    Returns AI-curated executive briefs and section syntheses from past runs.
    Use this to track how news narratives evolve over days.

    Args:
        pipeline: 'digest' for news digests, 'monitor' for brand monitoring (default: digest)
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
@mcp_result.tool
def news_stats() -> dict:
    """Get news database statistics: article counts, category distribution, source distribution, date range."""
    from news.query import get_news_stats

    conn = _get_conn()
    try:
        return get_news_stats(conn)
    finally:
        conn.close()


@mcp.tool()
@mcp_result.tool
def recent_for_tickers(
    tickers: list[str],
    hours: int = 24,
    limit: int = 50,
) -> list[dict]:
    """Get recent news articles tagged with any of the given tickers.

    Optimized for trading workflows — pass a portfolio + watchlist ticker list
    and get back relevant news from the cached corpus, much faster than WebSearch.

    Args:
        tickers: List of ticker symbols (e.g. ['AAPL', 'MSFT']). Case-insensitive.
        hours: Lookback window in hours (default: 24).
        limit: Max articles to return (default: 50).
    """
    from news.query import recent_for_tickers as _query

    conn = _get_conn()
    try:
        return _query(conn, tickers, hours=hours, limit=limit)
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run()
