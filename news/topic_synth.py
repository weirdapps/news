"""AI synthesis layer for ad-hoc topical news briefs — Claude CLI.

Brand-neutral by design: the topic profile is driven by a user-supplied
free-text --query string and has no brand context. Reuses the section-builder
pattern from monitor_synth and the Claude invocation from synthesizer.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlencode

from news.roster import build_roster
from news.synthesizer import invoke_claude, parse_synthesis_output

logger = logging.getLogger(__name__)

_GOOGLE_NEWS_RSS_BASE = "https://news.google.com/rss/search"


def build_google_news_url(query: str, hours: int = 24) -> str:
    """Construct a Google News RSS search URL for a topic query.

    Args:
        query: Free-text user query (preserved verbatim, percent-encoded)
        hours: Time window in hours (e.g. 24, 48, 168)

    Returns:
        Fully-formed RSS URL
    """
    # The `when:Nh` operator must be appended to the q= parameter, then the
    # whole thing percent-encoded. urlencode handles the escaping for us.
    q = f"{query} when:{hours}h"
    params = urlencode(
        {
            "q": q,
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        }
    )
    return f"{_GOOGLE_NEWS_RSS_BASE}?{params}"


def _topic_base_prompt(query: str, hours: int) -> str:
    """Build the topic-brief base prompt (intro + analyst framing)."""
    return f"""You are a news intelligence analyst preparing a focused brief on TOPIC: {query} (last {hours}h).

You will receive a list of recent articles related to this topic. Your job is to:
1. SELECT the most relevant and substantive articles (filter noise, duplicates, off-topic results)
2. GROUP them into 2-5 meaningful sections that organize the topic into sub-themes
3. SYNTHESIZE each section into actionable intelligence — connect dots across stories, surface trends, flag tensions

**WHAT TO PRIORITIZE:**
- Articles that directly address the topic with concrete facts, data, or analysis
- Recent developments, decisions, or announcements
- Conflicting perspectives that illuminate the topic
- Authoritative sources (major outlets, primary sources, named experts)

**WHAT TO SKIP:**
- Articles that mention the topic only in passing
- Clickbait, listicles, SEO filler
- Pure opinion with no new information
- Duplicate stories — pick the best source only
- Off-topic results that the search returned but are not actually about the query

"""


def _topic_output_format() -> str:
    """Build the JSON schema + rules section for topic synthesis."""
    return """**OUTPUT FORMAT:**
Return a JSON object with this exact structure:

{
  "executive_brief": [
    "Bullet 1 — most important insight on the topic",
    "Bullet 2",
    "Bullet 3",
    "Bullet 4",
    "Bullet 5"
  ],
  "sections": [
    {
      "display_name": "Descriptive Sub-Theme Title",
      "synthesis": "2-3 paragraph synthesis connecting stories within this sub-theme",
      "opposing_views": "Note any conflicting perspectives between sources, or 'None noted'",
      "fact_check": "Flag any speculation vs verified facts, or 'All statements fact-based'",
      "sources": ["Source1", "Source2"],
      "high_value": true
    }
  ],
  "source_count": 12,
  "time_window": "last 24h"
}

**RULES:**
1. Curate ruthlessly — quality over quantity. 2-5 sections max.
2. Synthesize, don't summarize: connect dots across stories, identify trends.
3. Note tensions: when sources conflict, flag this explicitly.
4. Distinguish verified facts from commentary/speculation.
5. Be concise: executive_brief = 5 bullets max, section synthesis = 2-3 paragraphs.
6. display_name should reflect the actual sub-theme content, not generic labels.
7. high_value: true for sections with strategic implications.

Return ONLY valid JSON. No preamble, no markdown formatting, no prose. Just the JSON object."""


def _build_article_entry(article: Any, index: int) -> dict:
    """Build a single article entry for the topic prompt."""
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


def build_topic_prompt(articles: list[Any], query: str, hours: int) -> str:
    """Build the topic prompt for Claude.

    Args:
        articles: List of Article objects fetched for the topic
        query: User's free-text query
        hours: Time window in hours

    Returns:
        Complete prompt string
    """
    sections = [
        _topic_base_prompt(query, hours),
        # Brand-neutral roster: just the generic NAME_HANDLING_RULES, no leadership.
        build_roster(),
        "\n\n",
        _topic_output_format(),
    ]
    system_prompt = "".join(sections)

    article_entries = [_build_article_entry(article, i) for i, article in enumerate(articles)]

    context = {
        "query": query,
        "time_window": f"last {hours}h",
        "total_articles": len(article_entries),
        "articles": article_entries,
    }

    prompt = f"""{system_prompt}

**CONTEXT:**
{json.dumps(context, ensure_ascii=False)}

**INSTRUCTIONS:**
Review all {len(article_entries)} articles. Select the most relevant ones, group them into 2-5 sub-themes, and generate the JSON synthesis output for the topic: {query}."""

    return prompt


def build_topic_fallback(articles: list[Any]) -> str:
    """Build a plain-text fallback when synthesis fails.

    Args:
        articles: List of Article objects

    Returns:
        Plain text fallback report
    """
    lines = ["TOPIC SYNTHESIS UNAVAILABLE", "", "Recent headlines:", ""]

    by_source: dict[str, list] = {}
    for article in articles:
        source = article.source
        if source not in by_source:
            by_source[source] = []
        by_source[source].append(article)

    for source, source_articles in sorted(by_source.items()):
        lines.append(f"## {source}")
        for article in source_articles[:5]:
            lines.append(f"- {article.title}")
            if article.url:
                lines.append(f"  {article.url}")
        lines.append("")

    return "\n".join(lines)


def synthesize_topic(
    articles: list[Any],
    query: str,
    hours: int,
    max_retries: int = 2,
    timeout: int = 300,
    claude_command: str = "claude",
    claude_args: list[str] | None = None,
) -> tuple[dict[str, Any] | str, bool]:
    """Main topic synthesis function.

    Args:
        articles: List of Article objects fetched for the topic
        query: User's free-text query
        hours: Time window in hours
        max_retries: Maximum retry attempts
        timeout: Timeout in seconds
        claude_command: Path to claude command
        claude_args: Additional arguments

    Returns:
        Tuple of (synthesis_data, success_flag)
    """
    prompt = build_topic_prompt(articles, query, hours)
    logger.info(f"Topic prompt: {len(prompt)} chars for {len(articles)} articles")

    raw_output = invoke_claude(
        prompt,
        timeout=timeout,
        claude_command=claude_command,
        claude_args=claude_args,
        validate=lambda text: "error" not in parse_synthesis_output(text),
    )

    if raw_output is None:
        logger.warning("Topic synthesis failed: no output")
        logger.error("All topic synthesis attempts failed, using fallback")
        fallback = build_topic_fallback(articles)
        return (fallback, False)

    parsed = parse_synthesis_output(raw_output)

    if "error" not in parsed:
        logger.info("Topic synthesis succeeded")
        return (parsed, True)

    logger.warning("Topic synthesis failed: parse error")
    logger.error("All topic synthesis attempts failed, using fallback")
    fallback = build_topic_fallback(articles)
    return (fallback, False)
