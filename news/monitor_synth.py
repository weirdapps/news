"""AI synthesis layer for brand monitoring — Claude CLI."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from news.roster import build_roster
from news.synthesizer import invoke_claude, parse_synthesis_output

logger = logging.getLogger(__name__)


def _base_prompt(display: dict) -> str:
    """Build the brand-neutral base prompt from a display block."""
    full_name = display.get("full_name", "the company")
    short_name = display.get("short_name", full_name)
    return f"""You are a brand intelligence analyst monitoring {full_name} ({short_name}) for a senior executive.

You will receive a list of articles/mentions. Your job is to:
1. VERIFY which articles genuinely mention {short_name} (filter false positives)
2. CLASSIFY each mention by type and sentiment
3. SYNTHESIZE a concise brand monitoring report

**SENTIMENT SCORING:**
- positive: good earnings, upgrades, product launches, awards, positive analyst coverage
- negative: downgrades, scandals, regulatory penalties, complaints, negative press
- neutral: factual reporting, routine announcements, listings

**MENTION TYPES:**
- news: Media coverage, analysis, opinion pieces
- regulatory: Central-bank, supervisory, or government announcements affecting the company
- stock: Stock price, trading volume, analyst ratings
- corporate: Press releases, IR announcements, leadership changes
- sector: Sector news that includes the company in context
"""


def _disambiguation_section(false_positives: list[str]) -> str:
    """Build the false-positive filter section, or '' when no false positives."""
    if not false_positives:
        return ""
    lines = "\n".join(f'- "{fp}"' for fp in false_positives)
    return f"""
**FALSE POSITIVE FILTERING (CRITICAL):**
Phrases that look like the company name but are NOT — exclude articles where these are the only match:
{lines}
"""


def _competitor_section(competitors: dict) -> str:
    """Build the competitor-context section, or '' when no competitors configured.

    Includes the dict key alongside each competitor name so the LLM uses the
    same key in its JSON output's `competitor_watch` block (which the email
    template iterates over dynamically).
    """
    if not competitors:
        return ""
    lines = []
    for key, comp in competitors.items():
        names = comp.get("names", [])
        if names:
            lines.append(f"  - {key!r}: {names[0]}")
    if not lines:
        return ""
    return f"""
**COMPETITOR CONTEXT:**
Compare the company's mentions alongside these competitors where relevant. In your JSON output, use the exact KEY (left side of `:`) as the key in `competitor_watch`:
{chr(10).join(lines)}
"""


def _output_format_section(short_name: str) -> str:
    """Build the JSON output schema + run-cadence rules section."""
    return f"""
**CITATION REQUIREMENT (CRITICAL):**
Every bullet, alert, mention, and competitor entry MUST include an "article_ids" field listing the integer id(s) of the input articles that support it — using the "id" field from the articles array in CONTEXT below. If you cannot point to a specific input article that supports a claim, OMIT the claim. Unsourced items will be silently dropped before delivery.

**OUTPUT FORMAT:**
Return a JSON object with this exact structure:

{{{{
  "mention_count": 15,
  "new_since_last": 8,
  "sentiment_summary": {{{{
    "positive": 5,
    "negative": 2,
    "neutral": 8,
    "trend": "improving"
  }}}},
  "alerts": [
    {{{{"text": "Brief description of any critical/urgent items requiring attention", "article_ids": [4]}}}}
  ],
  "company_mentions": [
    {{{{
      "title": "Article title",
      "source": "Source name",
      "type": "news|regulatory|stock|corporate|sector",
      "sentiment": "positive|negative|neutral",
      "summary": "One-sentence summary of the mention",
      "relevance": "high|medium|low",
      "article_ids": [12]
    }}}}
  ],
  "sector_context": "1-2 paragraph synthesis of relevant sector activity",
  "competitor_watch": {{{{
    "<competitor_key>": {{{{"summary": "Brief on competitor activity", "article_ids": [7]}}}}
  }}}},
  "executive_brief": [
    {{{{"text": "Bullet 1 — most important {short_name}-related insight", "article_ids": [12, 4]}}}},
    {{{{"text": "Bullet 2", "article_ids": [7]}}}}
  ]
}}}}

**NEW vs REPEAT ARTICLES:**
Each article has an "is_new" flag:
- is_new: true — first time this article appears (fetched since last scan)
- is_new: false — already included in a previous scan (still within 24h window)

This monitor runs every 2 hours during business hours. Each report must STAND ALONE as a complete picture because the reader may not see every run. Therefore:
- ALWAYS include key ongoing stories even if they are repeats (is_new: false)
- Use the "new_since_last" count to show how many are genuinely new
- In the executive_brief, lead with new developments but repeat critical ongoing items
- In company_mentions, include both new and important repeat items — mark new items with a prefix like "NEW:" in the summary

**RULES:**
1. If no genuine {short_name} mentions exist, return mention_count: 0 with empty arrays
2. Alerts array should only contain genuinely urgent items (negative press, regulatory actions, stock drops) — and MUST cite article_ids; never invent incidents not in the input
3. Each report must be self-contained — the reader may have missed previous scans
4. Competitor section: each entry must cite article_ids; entries with no source articles will be dropped
5. Executive brief: max 5 bullets, lead with new items, repeat critical ongoing items
6. company_mentions[].article_ids: include the id of the input article that the mention summarises (the renderer uses this id to attach the source URL to the mention)

Return ONLY valid JSON. No preamble, no markdown formatting."""


def _build_article_entry(article: Any, index: int, last_run_at: datetime | None) -> dict:
    """Build a single article entry for the monitor prompt."""
    is_new = True
    if last_run_at and article.fetched_at:
        is_new = article.fetched_at > last_run_at

    entry = {
        "id": index,
        "title": article.title,
        "source": article.source,
        "category": article.categories[0] if article.categories else "unknown",
        "snippet": article.content[:300] if article.content else "",
        "language": article.language,
        "is_new": is_new,
    }

    if article.published_at and article.fetched_at:
        entry["age_hours"] = round(
            (article.fetched_at - article.published_at).total_seconds() / 3600
        )

    return entry


def build_monitor_prompt(
    articles: list[Any],
    keywords_config: dict,
    previous_summary: dict | None,
    time_window: str,
    last_run_at: datetime | None = None,
) -> str:
    """Build the monitor prompt for Claude.

    Args:
        articles: List of Article objects from the monitor feed
        keywords_config: Brand-monitor keywords config (display, company,
            competitors, ...) — drives the section-builders.
        previous_summary: Previous monitor run's summary (for trend comparison)
        time_window: Description of time window
        last_run_at: Timestamp of the previous monitor run (for new/repeat flagging)

    Returns:
        Complete prompt string
    """
    display = keywords_config.get("display", {})
    company = keywords_config.get("company", {})
    competitors = keywords_config.get("competitors", {})
    short_name = display.get("short_name", display.get("full_name", "the company"))

    sections = [
        _base_prompt(display),
        _disambiguation_section(company.get("false_positives", [])),
        _competitor_section(competitors),
        build_roster(keywords_config),
        _output_format_section(short_name),
    ]
    system_prompt = "".join(s for s in sections if s)

    article_entries = [
        _build_article_entry(article, i, last_run_at) for i, article in enumerate(articles)
    ]
    new_count = sum(1 for entry in article_entries if entry["is_new"])

    context = {
        "time_window": time_window,
        "total_articles": len(article_entries),
        "new_articles": new_count,
        "repeat_articles": len(article_entries) - new_count,
        "articles": article_entries,
    }

    if previous_summary:
        prev_sentiment = previous_summary.get("sentiment_summary", {})
        context["previous_sentiment"] = prev_sentiment

    prompt = f"""{system_prompt}

**CONTEXT:**
{json.dumps(context, ensure_ascii=False)}

**INSTRUCTIONS:**
Review all {len(article_entries)} articles. Filter false positives, classify genuine {short_name} mentions, assess sentiment, and generate the JSON monitoring report."""

    return prompt


def build_monitor_fallback(articles: list[Any]) -> str:
    """Build a plain-text fallback when synthesis fails.

    Args:
        articles: List of Article objects

    Returns:
        Plain text fallback report
    """
    lines = ["MONITOR SYNTHESIS UNAVAILABLE", "", "Raw mentions:", ""]

    by_category: dict[str, list] = {}
    for article in articles:
        cat = article.categories[0] if article.categories else "other"
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(article)

    for cat, cat_articles in sorted(by_category.items()):
        lines.append(f"## {cat}")
        for article in cat_articles[:5]:
            lines.append(f"- {article.title} ({article.source})")
        lines.append("")

    return "\n".join(lines)


def synthesize_monitor(
    articles: list[Any],
    keywords_config: dict,
    previous_summary: dict | None = None,
    time_window: str = "last hour",
    last_run_at: datetime | None = None,
    max_retries: int = 2,
    timeout: int = 300,
    claude_command: str = "claude",
    claude_args: list[str] | None = None,
) -> tuple[dict[str, Any] | str, bool]:
    """Main monitor synthesis function.

    Args:
        articles: List of Article objects from monitor feed
        keywords_config: Brand-monitor keywords config (display, company,
            competitors, ...) — passed through to build_monitor_prompt.
        previous_summary: Previous run's synthesis data for trend comparison
        time_window: Description of time window
        last_run_at: Timestamp of previous run (for new/repeat flagging)
        max_retries: Maximum retry attempts
        timeout: Timeout in seconds
        claude_command: Path to claude command
        claude_args: Additional arguments

    Returns:
        Tuple of (synthesis_data, success_flag)
    """
    prompt = build_monitor_prompt(
        articles, keywords_config, previous_summary, time_window, last_run_at
    )
    logger.info(f"Monitor prompt: {len(prompt)} chars for {len(articles)} articles")

    for attempt in range(max_retries):
        logger.info(f"Monitor synthesis attempt {attempt + 1}/{max_retries}")

        raw_output = invoke_claude(
            prompt,
            timeout=timeout,
            claude_command=claude_command,
            claude_args=claude_args,
        )

        if raw_output is None:
            logger.warning(f"Attempt {attempt + 1} failed: no output")
            continue

        parsed = parse_synthesis_output(raw_output)

        if "error" not in parsed:
            logger.info("Monitor synthesis succeeded")
            return (parsed, True)

        logger.warning(f"Attempt {attempt + 1} failed: parse error")

    logger.error("All monitor synthesis attempts failed, using fallback")
    fallback = build_monitor_fallback(articles)
    return (fallback, False)
