"""AI synthesis layer using Claude CLI — AI-curated news selection."""

import json
import logging
import re
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a senior news analyst preparing a daily briefing for a C-level banking executive who leads Cards & Digital Business at National Bank of Greece (NBG).

You will receive a large list of article titles and snippets. Your job is to:
1. SELECT the most important articles (typically 20-40 out of hundreds)
2. GROUP them into meaningful sections
3. SYNTHESIZE each section into actionable intelligence

**WHAT TO PRIORITIZE (in order of importance):**
1. NBG (National Bank of Greece / Εθνική Τράπεζα) specific news — ANY mention is top priority
2. Greek banking sector: Piraeus Bank, Alpha Bank, Eurobank, Bank of Greece, Greek bank earnings, regulation
3. Greece macro/political: government policy, economy, bonds, ATHEX index, elections, EU relations
4. Market-moving business: US/EU tariffs, trade war escalation, Fed/ECB rate decisions, recession signals, major M&A, oil/energy shocks, S&P/Nasdaq significant moves
5. Claude Code practical content: tutorials, tips, MCP servers, hooks, plugins, Claude CLI usage, agentic AI workflows — the reader is a daily Claude Code user and wants actionable how-to content
6. Anthropic company news: funding, partnerships, product launches, policy positions
7. AI industry: enterprise AI adoption, AI in banking/finance, regulation, significant model releases
8. Learning & Tools: Claude Code release notes, trending GitHub repos (especially AI/Python), interesting Product Hunt launches, Show HN projects, developer tutorials, workflow tips — the reader wants to stay sharp and discover useful tools
9. Investment themes: sector rotation, earnings surprises, new opportunities
10. Payments & fintech: PSD2/3, instant payments, digital wallets, open banking — relevant to the reader's Cards & Digital Business role
11. Major Apple/tech only if truly significant

**WHAT TO SKIP:**
- Generic Greek news with no business/banking/economic angle (sports, entertainment, weather, crime)
- Routine corporate press releases
- Clickbait, listicles, SEO-optimized filler articles
- Duplicate stories — pick the best source only
- Medium/Substack articles that are shallow or promotional
- General tech reviews, gadget roundups
- GitHub repos with no clear practical value (star-farming, joke repos)
- Product Hunt launches that are trivial or irrelevant to finance/AI/productivity
- Note: "Εθνική" alone does NOT mean NBG — "Εθνική Οικονομία" (National Economy) or "Εθνική Ομάδα" (National Team) are NOT NBG news

**RULES:**
1. Curate ruthlessly — quality over quantity. Skip entire categories if nothing meaningful happened.
2. Synthesize, don't summarize: Connect dots across stories, identify trends, flag strategic implications
3. Note tensions: When sources conflict or present opposing views, explicitly flag this
4. Flag fact vs opinion: Distinguish between verified facts and commentary/speculation
5. Be concise: Executive brief = 5 bullets max, section synthesis = 2-3 paragraphs max
6. For AI section: Focus on what can be practically applied, not just announcements
7. Create section names that reflect the actual content, not generic category labels

**OUTPUT FORMAT:**
Return a JSON object with this exact structure:

{
  "executive_brief": [
    "Bullet 1 - most critical insight",
    "Bullet 2 - second most critical",
    "Bullet 3",
    "Bullet 4",
    "Bullet 5"
  ],
  "what_changed": "Summary of what's new since previous highlights",
  "sections": [
    {
      "category": "category_key",
      "display_name": "Descriptive Section Title",
      "synthesis": "2-3 paragraph synthesis connecting the dots across stories in this section",
      "opposing_views": "Note any conflicting perspectives or tensions between sources, or 'None noted'",
      "fact_check": "Flag any speculation vs verified facts, or 'All statements fact-based'",
      "sources": ["Source1", "Source2"],
      "high_value": true
    }
  ]
}

**category keys** (use these): banking, greece, business, ai, trading, learning, tech, apple
**high_value flag:** true for sections with strategic business implications, false for general interest.
**display_name:** Use descriptive titles that reflect the actual content (e.g. "ECB Rate Decision & Banking Impact" not just "Banking & Fintech").

Return ONLY valid JSON. No preamble, no markdown formatting, no prose. Just the JSON object."""


def build_prompt(
    articles: list[Any],
    previous_highlights: list[str],
    time_window: str,
) -> str:
    """Build the prompt with all articles for Claude to curate and synthesize.

    Args:
        articles: Flat list of all Article objects from the digest pool
        previous_highlights: List of highlights from previous briefing
        time_window: Description of time window

    Returns:
        Complete prompt string
    """
    # Build compact article list — title + source + short snippet
    article_entries = []
    for i, article in enumerate(articles):
        entry = {
            "id": i,
            "title": article.title,
            "source": article.source,
            "snippet": article.content[:200] if article.content else "",
        }
        if article.published_at:
            entry["age_hours"] = (
                round(
                    (article.fetched_at - article.published_at).total_seconds() / 3600
                )
                if article.fetched_at
                else None
            )
        article_entries.append(entry)

    context = {
        "time_window": time_window,
        "previous_highlights": previous_highlights,
        "total_articles": len(article_entries),
        "articles": article_entries,
    }

    prompt = f"""{_SYSTEM_PROMPT}

**CONTEXT:**
{json.dumps(context, ensure_ascii=False)}

**INSTRUCTIONS:**
Review all {len(article_entries)} articles above. Select the most important ones, group them into sections, and generate the JSON synthesis output. Skip categories with nothing meaningful."""

    return prompt


def invoke_claude(
    prompt: str,
    timeout: int = 120,
    claude_command: str = "claude",
    claude_args: list[str] | None = None,
) -> str | None:
    """Invoke Claude CLI with the given prompt.

    Args:
        prompt: The prompt to send to Claude
        timeout: Timeout in seconds
        claude_command: Path to claude command
        claude_args: Additional arguments to pass to claude

    Returns:
        Claude's response stdout, or None if failed
    """
    if claude_args is None:
        claude_args = []

    # Always use --bare to prevent CLAUDE.md auto-discovery and hooks
    # from injecting conflicting instructions into the synthesis prompt.
    bare_args = list(claude_args)
    if "--bare" not in bare_args:
        bare_args.append("--bare")

    cmd = [claude_command] + bare_args

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

        if result.returncode != 0:
            logger.warning(
                f"Claude CLI exited with code {result.returncode}: "
                f"{result.stderr[:500] if result.stderr else '(no stderr)'}"
            )

        if not result.stdout or not result.stdout.strip():
            logger.warning(
                f"Claude CLI returned empty stdout. "
                f"stderr: {result.stderr[:500] if result.stderr else '(none)'}"
            )
            return None

        return result.stdout

    except subprocess.TimeoutExpired:
        logger.warning(f"Claude CLI timed out after {timeout}s")
        return None

    except Exception as e:
        logger.error(f"Failed to invoke Claude CLI: {e}")
        return None


def parse_synthesis_output(raw: str) -> dict[str, Any]:
    """Parse Claude's output, extracting JSON from various formats.

    Args:
        raw: Raw string output from Claude

    Returns:
        Parsed JSON dict, or fallback dict with error message
    """
    # Try direct JSON parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code block
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try finding JSON object in text
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    # All parsing failed — log raw output for diagnostics
    preview = raw[:500] if raw else "(empty)"
    logger.error(
        f"Failed to parse Claude output as JSON. Raw output preview: {preview}"
    )
    return {
        "executive_brief": ["Failed to parse synthesis output"],
        "what_changed": "Error occurred during synthesis",
        "sections": [],
        "error": "Parse failure",
    }


def build_fallback_digest(
    articles: list[Any],
) -> str:
    """Build a plain-text fallback digest when synthesis fails.

    Args:
        articles: Flat list of Article objects

    Returns:
        Plain text digest with headlines grouped by source category
    """
    lines = ["SYNTHESIS UNAVAILABLE", "", "Recent headlines:", ""]

    # Group by source
    by_source: dict[str, list] = {}
    for article in articles:
        source = article.source
        if source not in by_source:
            by_source[source] = []
        by_source[source].append(article)

    for source, source_articles in sorted(by_source.items()):
        lines.append(f"## {source}")
        lines.append("")
        for article in source_articles[:5]:  # Max 5 per source in fallback
            lines.append(f"- {article.title}")
            lines.append(f"  {article.url}")
            lines.append("")

    return "\n".join(lines)


def synthesize(
    articles: list[Any],
    previous_highlights: list[str] | None = None,
    time_window: str = "last 48 hours",
    max_retries: int = 2,
    timeout: int = 300,
    claude_command: str = "claude",
    claude_args: list[str] | None = None,
) -> tuple[dict[str, Any] | str, bool]:
    """Main synthesis function — Claude curates and synthesizes in one pass.

    Args:
        articles: Flat list of all Article objects from digest pool
        previous_highlights: List of highlights from previous briefing
        time_window: Description of time window
        max_retries: Maximum retry attempts
        timeout: Timeout in seconds for each attempt
        claude_command: Path to claude command
        claude_args: Additional arguments to pass to claude

    Returns:
        Tuple of (synthesis_data, used_claude)
        - synthesis_data: Either parsed JSON dict or fallback plain text string
        - used_claude: True if synthesis succeeded, False if fallback used
    """
    if previous_highlights is None:
        previous_highlights = []

    prompt = build_prompt(articles, previous_highlights, time_window)
    logger.info(f"Prompt size: {len(prompt)} chars for {len(articles)} articles")

    for attempt in range(max_retries):
        logger.info(f"Synthesis attempt {attempt + 1}/{max_retries}")

        raw_output = invoke_claude(
            prompt,
            timeout=timeout,
            claude_command=claude_command,
            claude_args=claude_args,
        )

        if raw_output is None:
            logger.warning(f"Attempt {attempt + 1} failed: no output from Claude")
            continue

        logger.info(
            f"Raw Claude output: {len(raw_output)} chars, starts with: {raw_output[:200]!r}"
        )
        parsed = parse_synthesis_output(raw_output)

        # Check if parsing succeeded (no error key)
        if "error" not in parsed:
            logger.info("Synthesis succeeded")
            return (parsed, True)

        logger.warning(f"Attempt {attempt + 1} failed: parse error")

    # All attempts failed, return fallback
    logger.error("All synthesis attempts failed, using fallback digest")
    fallback = build_fallback_digest(articles)
    return (fallback, False)
