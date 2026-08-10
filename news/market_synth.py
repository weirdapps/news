"""AI synthesis layer for the market profile — market-moving news.

Produces a broad market-moving synthesis (executive brief + per-category
sections) that feeds the trading Investment Brief. Portfolio-specific movers
are surfaced separately at consumption time via ``recent_for_tickers`` (ticker
tags), so this layer focuses on the BROAD market read.

Reuses invoke_claude + parse_synthesis_output from synthesizer; output schema
(executive_brief[], sections[] with article_ids) stays compatible with the
citation filter and the news-reader MCP.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from news.synthesizer import invoke_claude, parse_synthesis_output

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a senior markets strategist writing the market-moving-news section of a daily investment brief for a professional investor.

The reader runs a global, long-biased equity book (~$1.1M, ~44 holdings): US mega-cap tech (NVDA, MSFT, GOOG, AMZN, AVGO), Greater-China & HK (Alibaba, Tencent, Geely, Great Wall, ICBC), Japan (Itochu, Kawasaki, Suzuki), Europe (Deutsche Telekom, VW, Santander, UniCredit), gold (GLD), a Greek ETF (ATHEX), plus selective EM and small crypto exposure. Home currency EUR. They want ONE thing: **what is moving markets, and why it matters to a book like theirs.**

You will receive market/macro/commodity/crypto/Greece news articles. Your job:

1. SELECT the genuinely market-moving items (typically 12-25 out of the pool). Ignore filler, promotional content, and stale rehashes.
2. RANK by market impact — what actually moves prices: rate decisions, inflation/jobs prints, earnings surprises, guidance cuts, M&A, geopolitics, commodity shocks, regulatory actions.
3. GROUP into the sections below and SYNTHESIZE each into a tight read.

**SECTIONS (in this order, include only those with real news):**
1. **macro_rates** — Central banks (Fed/ECB), rates, inflation, jobs, GDP, bonds, the dollar/EUR.
2. **equities** — Index moves, big single-stock earnings/guidance/M&A/analyst actions, IPOs.
3. **sectors_themes** — Sector rotations and themes (semis/AI, EV, banks, healthcare, luxury, defense).
4. **commodities_energy** — Oil, gas, gold, metals, OPEC.
5. **crypto** — Bitcoin/ether, ETF flows, regulation — only if genuinely market-moving.
6. **greece_athex** — Greek market / ATHEX / Greek banks (reader is NBG + holds a Greek ETF).

**RULES:**
1. Curate ruthlessly — quality over quantity. Skip entire sections if nothing meaningful happened.
2. Synthesize, don't summarize: connect dots, name the market implication, flag what it means for a global long book.
3. Prefer primary/tier-1 sources; when sources conflict, note it in opposing_views.
4. Distinguish confirmed facts from speculation/rumor in fact_check.
5. No price predictions or advice — describe what happened and the transmission to markets.
6. Be concise: executive_brief = 5 bullets max, each section synthesis = 1-2 tight paragraphs.

**CITATION REQUIREMENT (CRITICAL):**
Every executive_brief bullet and every section MUST include an "article_ids" field listing the integer id(s) of the supporting input articles — using the "id" field from the articles array in CONTEXT. If you cannot point to a specific input article, OMIT the claim. Never invent news.

**OUTPUT FORMAT:**
Return ONLY a JSON object with this exact structure:

{
  "executive_brief": [
    {"text": "Most market-moving development right now + why it matters", "article_ids": [3, 7]},
    {"text": "Next", "article_ids": [12]}
  ],
  "sections": [
    {
      "category": "macro_rates",
      "display_name": "Descriptive title reflecting the actual news",
      "synthesis": "1-2 paragraph synthesis of what moved and the market implication",
      "opposing_views": "Tensions between sources, or 'None noted'",
      "fact_check": "Speculation vs facts, or 'All statements fact-based'",
      "sources": ["Source1", "Source2"],
      "article_ids": [3, 7, 12],
      "high_value": true
    }
  ]
}

**category keys:** macro_rates, equities, sectors_themes, commodities_energy, crypto, greece_athex
**high_value:** true for the highest-impact sections.
**display_name:** Reflect actual content (e.g. "Fed Holds, Signals One Cut in 2026" not "Macro & Rates").

Return ONLY valid JSON. No preamble, no markdown formatting, no prose. Just the JSON object."""


def _build_article_entry(article: Any, index: int) -> dict:
    entry = {
        "id": index,
        "title": article.title,
        "source": article.source,
        "snippet": article.content[:300] if article.content else "",
        "language": article.language,
    }
    if article.published_at and article.fetched_at:
        entry["age_hours"] = round(
            (article.fetched_at - article.published_at).total_seconds() / 3600
        )
    return entry


def build_market_prompt(
    articles: list[Any],
    previous_highlights: list[str] | None = None,
    time_window: str = "last 18 hours",
) -> str:
    article_entries = [_build_article_entry(article, i) for i, article in enumerate(articles)]

    context: dict[str, Any] = {
        "time_window": time_window,
        "total_articles": len(article_entries),
        "articles": article_entries,
    }
    if previous_highlights:
        context["previous_highlights"] = previous_highlights

    return f"""{_SYSTEM_PROMPT}

**CONTEXT:**
{json.dumps(context, ensure_ascii=False)}

**INSTRUCTIONS:**
Review all {len(article_entries)} articles. Select the market-moving ones, rank by impact, group into the sections, and generate the JSON synthesis. Skip sections with nothing meaningful."""


def build_market_fallback(articles: list[Any]) -> str:
    lines = ["MARKET SYNTHESIS UNAVAILABLE", "", "Recent headlines:", ""]
    by_source: dict[str, list] = {}
    for article in articles:
        by_source.setdefault(article.source, []).append(article)

    for source, source_articles in sorted(by_source.items()):
        lines.append(f"## {source}")
        for article in source_articles[:5]:
            lines.append(f"- {article.title}")
            if article.url:
                lines.append(f"  {article.url}")
        lines.append("")

    return "\n".join(lines)


def synthesize_market(
    articles: list[Any],
    previous_highlights: list[str] | None = None,
    time_window: str = "last 18 hours",
    max_retries: int = 2,
    timeout: int = 300,
    claude_command: str = "claude",
    claude_args: list[str] | None = None,
) -> tuple[dict[str, Any] | str, bool]:
    prompt = build_market_prompt(articles, previous_highlights, time_window)
    logger.info(f"Market prompt: {len(prompt)} chars for {len(articles)} articles")

    raw_output = invoke_claude(
        prompt,
        timeout=timeout,
        claude_command=claude_command,
        claude_args=claude_args,
    )

    if raw_output is None:
        logger.warning("Market synthesis failed: no output")
        logger.error("All market synthesis attempts failed, using fallback")
        fallback = build_market_fallback(articles)
        return (fallback, False)

    parsed = parse_synthesis_output(raw_output)

    if "error" not in parsed:
        logger.info("Market synthesis succeeded")
        return (parsed, True)

    logger.warning("Market synthesis failed: parse error")
    logger.error("All market synthesis attempts failed, using fallback")
    fallback = build_market_fallback(articles)
    return (fallback, False)
