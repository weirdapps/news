"""AI synthesis layer using Claude CLI."""

import json
import logging
import re
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a senior news analyst preparing a daily briefing for a C-level banking executive.

Your job is to synthesize news into actionable intelligence, NOT summarize headlines.

**RULES:**
1. Synthesize, don't summarize: Connect dots across stories, identify trends, flag strategic implications
2. Note tensions: When sources conflict or present opposing views, explicitly flag this
3. Flag fact vs opinion: Distinguish between verified facts and commentary/speculation
4. Connect dots for high-value stories: For banking/finance/AI stories with potential business impact, provide deeper context
5. Be concise: Executive brief = 5 bullets max, section synthesis = 2-3 paragraphs max

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
      "display_name": "Category Display Name",
      "synthesis": "2-3 paragraph synthesis connecting the dots across stories in this category",
      "opposing_views": "Note any conflicting perspectives or tensions between sources, or 'None noted'",
      "fact_check": "Flag any speculation vs verified facts, or 'All statements fact-based'",
      "sources": ["Source1", "Source2"],
      "high_value": true
    }
  ]
}

**high_value flag:** Set to true for categories with strategic business implications (banking, finance, AI, regulatory, competitive intelligence). Set to false for general interest categories.

Return ONLY valid JSON. No preamble, no markdown formatting, no prose. Just the JSON object."""


def build_prompt(
    articles_by_category: dict[str, list[Any]],
    previous_highlights: list[str],
    time_window: str,
) -> str:
    """Build the prompt for Claude.

    Args:
        articles_by_category: Dict mapping category keys to lists of Article objects
        previous_highlights: List of highlights from previous briefing
        time_window: Description of time window (e.g. "last 24 hours")

    Returns:
        Complete prompt string combining system prompt and context
    """
    # Build article summaries by category
    article_summaries = {}
    for category, articles in articles_by_category.items():
        summaries = []
        for article in articles:
            summary = {
                "title": article.title,
                "source": article.source,
                "url": article.url,
                "content_preview": article.content[:500] if article.content else "",
                "relevance_score": article.relevance_score,
            }
            summaries.append(summary)
        article_summaries[category] = summaries

    context = {
        "time_window": time_window,
        "previous_highlights": previous_highlights,
        "articles_by_category": article_summaries,
    }

    prompt = f"""{_SYSTEM_PROMPT}

**CONTEXT:**
{json.dumps(context, indent=2)}

**INSTRUCTIONS:**
Analyze the above articles and generate the JSON synthesis output following the format specified in the system prompt."""

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

    cmd = [claude_command] + claude_args

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
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

    # All parsing failed
    logger.error("Failed to parse Claude output as JSON")
    return {
        "executive_brief": ["Failed to parse synthesis output"],
        "what_changed": "Error occurred during synthesis",
        "sections": [],
        "error": "Parse failure",
    }


def build_fallback_digest(
    articles_by_category: dict[str, list[Any]],
    category_display_names: dict[str, str],
) -> str:
    """Build a plain-text fallback digest when synthesis fails.

    Args:
        articles_by_category: Dict mapping category keys to lists of Article objects
        category_display_names: Dict mapping category keys to display names

    Returns:
        Plain text digest with categorized headlines
    """
    lines = ["SYNTHESIS UNAVAILABLE", "", "Categorized headlines:", ""]

    for category, articles in sorted(articles_by_category.items()):
        display_name = category_display_names.get(category, category.title())
        lines.append(f"## {display_name}")
        lines.append("")

        for article in sorted(articles, key=lambda a: a.relevance_score or 0, reverse=True):
            lines.append(f"- {article.title}")
            lines.append(f"  {article.url}")
            lines.append(f"  Source: {article.source}")
            lines.append("")

    return "\n".join(lines)


def synthesize(
    articles_by_category: dict[str, list[Any]],
    category_display_names: dict[str, str],
    previous_highlights: list[str] | None = None,
    time_window: str = "last 24 hours",
    max_retries: int = 2,
    timeout: int = 120,
    claude_command: str = "claude",
    claude_args: list[str] | None = None,
) -> tuple[dict[str, Any] | str, bool]:
    """Main synthesis function with retries and fallback.

    Args:
        articles_by_category: Dict mapping category keys to lists of Article objects
        category_display_names: Dict mapping category keys to display names
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

    prompt = build_prompt(articles_by_category, previous_highlights, time_window)

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

        parsed = parse_synthesis_output(raw_output)

        # Check if parsing succeeded (no error key)
        if "error" not in parsed:
            logger.info("Synthesis succeeded")
            return (parsed, True)

        logger.warning(f"Attempt {attempt + 1} failed: parse error")

    # All attempts failed, return fallback
    logger.error("All synthesis attempts failed, using fallback digest")
    fallback = build_fallback_digest(articles_by_category, category_display_names)
    return (fallback, False)
