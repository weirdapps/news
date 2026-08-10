"""AI synthesis layer using Claude CLI — AI-curated news selection."""

import json
import logging
import os
import re
import subprocess
import time
from typing import Any

from news.llm_policy import (
    Action,
    Attempt,
    Outcome,
    ReauthResult,
    decide,
    reauth,
    resolve_deadline,
    running_on_linux,
)
from news.roster import build_roster

logger = logging.getLogger(__name__)

# Auth-class error markers in the Claude/Vertex JSON envelope's `result`. These
# are credential failures (re-auth fixes them) — distinct from a 429/quota or a
# policy refusal (a model downgrade fixes those). Kept precise to avoid
# misclassifying a quota error as auth.
_AUTH_ERROR_MARKERS = ("invalid_rapt", "invalid_grant", "reauth", "unauthenticated")


def _is_auth_error(env: dict[str, Any] | None) -> bool:
    """True if the envelope is an error AND looks like a gcloud auth-class failure."""
    if not env or not env.get("is_error"):
        return False
    result = str(env.get("result", "")).lower()
    return any(marker in result for marker in _AUTH_ERROR_MARKERS)


_RATE_LIMIT_MARKERS = ("429", "resource_exhausted", "quota", "rate limit")


def _classify(
    envelope: dict | None,
    raw_stdout: str | None,
    exc: BaseException | None,
) -> Outcome:
    """Map one transport result to a policy Outcome.

    Exactly one of the three arguments is meaningful per call: an exception if
    the subprocess raised, raw stdout if it ran but produced nothing usable, or
    a parsed envelope otherwise.

    This function is the whole behavioural risk of the port. Before it, nine
    distinct failure paths collapsed into a bare ``return None`` and the retry
    budget treated them identically. Each mapping below is a deliberate
    decision, not a translation.
    """
    if exc is not None:
        if isinstance(exc, subprocess.TimeoutExpired):
            return Outcome.TIMEOUT
        return Outcome.API_ERROR

    if envelope is None:
        if raw_stdout is None or not raw_stdout.strip():
            return Outcome.EMPTY
        return Outcome.UNPARSEABLE

    if _is_auth_error(envelope):
        return Outcome.AUTH_REAUTH_REQUIRED

    if envelope.get("stop_reason") == "refusal":
        return Outcome.REFUSAL

    if envelope.get("is_error"):
        blob = str(envelope.get("result", "")).lower()
        if any(marker in blob for marker in _RATE_LIMIT_MARKERS):
            return Outcome.RATE_LIMIT
        return Outcome.API_ERROR

    if not str(envelope.get("result", "")).strip():
        return Outcome.EMPTY

    return Outcome.OK


_SYSTEM_PROMPT = (
    """You are a senior news analyst preparing a daily briefing for a senior executive in financial services.

You will receive a large list of article titles and snippets. Your job is to:
1. SELECT the most important articles (typically 20-40 out of hundreds)
2. GROUP them into meaningful sections
3. SYNTHESIZE each section into actionable intelligence

**WHAT TO PRIORITIZE (in order of importance):**
1. Banking-sector news — earnings, regulation, M&A, supervisory actions
2. Financial services: payments, capital markets, retail banking, fintech disruption
3. Greece macro/political: government policy, economy, bonds, ATHEX index, elections, EU relations
4. Market-moving business: US/EU tariffs, trade war escalation, Fed/ECB rate decisions, recession signals, major M&A, oil/energy shocks, S&P/Nasdaq significant moves
5. Claude Code practical content: tutorials, tips, MCP servers, hooks, plugins, Claude CLI usage, agentic AI workflows — the reader is a daily Claude Code user and wants actionable how-to content
6. Anthropic company news: funding, partnerships, product launches, policy positions
7. AI industry: enterprise AI adoption, AI in banking/finance, regulation, significant model releases
8. Learning & Tools: Claude Code release notes, trending GitHub repos (especially AI/Python), interesting Product Hunt launches, Show HN projects, developer tutorials, workflow tips — the reader wants to stay sharp and discover useful tools
9. Investment themes: sector rotation, earnings surprises, new opportunities
10. Payments & fintech: PSD2/3, instant payments, digital wallets, open banking
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
- Note: filter out false-positive matches where the brand name resembles a different entity

"""
    + build_roster()
    + """

**RULES:**
1. Curate ruthlessly — quality over quantity. Skip entire categories if nothing meaningful happened.
2. Synthesize, don't summarize: Connect dots across stories, identify trends, flag strategic implications
3. Note tensions: When sources conflict or present opposing views, explicitly flag this
4. Flag fact vs opinion: Distinguish between verified facts and commentary/speculation
5. Be concise: Executive brief = 5 bullets max, section synthesis = 2-3 paragraphs max
6. For AI section: Focus on what can be practically applied, not just announcements
7. Create section names that reflect the actual content, not generic category labels

**CITATION REQUIREMENT (CRITICAL):**
Every bullet, section, and what-changed entry MUST include an "article_ids" field listing the integer id(s) of the input articles that support the claim — using the "id" field from the articles array in CONTEXT below. If you cannot point to a specific input article that supports a claim, OMIT the claim. Unsourced items will be silently dropped before delivery.

**OUTPUT FORMAT:**
Return a JSON object with this exact structure:

{
  "executive_brief": [
    {"text": "Bullet 1 — most critical insight", "article_ids": [3, 7]},
    {"text": "Bullet 2", "article_ids": [12]}
  ],
  "what_changed": [
    {"text": "What's new since previous highlights", "article_ids": [3]}
  ],
  "sections": [
    {
      "category": "category_key",
      "display_name": "Descriptive Section Title",
      "synthesis": "2-3 paragraph synthesis connecting the dots across stories in this section",
      "opposing_views": "Note any conflicting perspectives or tensions between sources, or 'None noted'",
      "fact_check": "Flag any speculation vs verified facts, or 'All statements fact-based'",
      "sources": ["Source1", "Source2"],
      "article_ids": [3, 7, 12],
      "high_value": true
    }
  ]
}

**category keys** (use these): banking, greece, business, ai, trading, learning, tech, apple
**high_value flag:** true for sections with strategic business implications, false for general interest.
**display_name:** Use descriptive titles that reflect the actual content (e.g. "ECB Rate Decision & Banking Impact" not just "Banking & Fintech").
**article_ids:** integer ids from the articles array in CONTEXT — required on every bullet, what_changed entry, and section. Sections must list the union of ids cited across all stories synthesised in that section.

Return ONLY valid JSON. No preamble, no markdown formatting, no prose. Just the JSON object."""
)


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
                round((article.fetched_at - article.published_at).total_seconds() / 3600)
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
    env: dict | None = None,
) -> str | None:
    """Invoke the model under the shared LLM policy. Returns the text, or None.

    This is the only retry loop in the synthesis path. The five per-profile
    ``for attempt in range(max_retries)`` loops that used to wrap it are gone:
    nested, their caps multiplied instead of capping, and the one-shot re-auth
    latch reset on every outer pass.

    Args:
        prompt: The prompt to send to Claude
        timeout: Timeout in seconds
        claude_command: Path to claude command
        claude_args: Additional arguments to pass to claude
        env: Environment mapping for deadline resolution (defaults to os.environ)
    """
    if claude_args is None:
        claude_args = []

    # --bare (no CLAUDE.md/hooks) + --print + --output-format json so we can read the
    # result envelope's stop_reason (refusal detection) and is_error (e.g. 429).
    bare_args = list(claude_args)
    if "--bare" not in bare_args:
        bare_args.append("--bare")
    if "--print" not in bare_args:
        bare_args.append("--print")
    if "--output-format" not in bare_args:
        bare_args += ["--output-format", "json"]

    def _resolve_tier() -> tuple[str | None, str | None]:
        """Map the opus/sonnet tier alias in bare_args to (exact id, region).

        The bare "opus" alias resolves to an unprovisioned eu quota bucket (429); the
        heavy-tier id claude-opus-5[1m] is the provisioned model. Region must track
        the model: Opus -> eu, Sonnet -> europe-west1 (central ~/.config/nbg-vertex/env).
        """
        if "--model" in bare_args:
            i = bare_args.index("--model") + 1
            if i < len(bare_args):
                tier = bare_args[i].lower()
                if "opus" in tier:
                    return (
                        os.environ.get("VERTEX_MODEL_HEAVY", "claude-opus-5[1m]"),
                        os.environ.get("VERTEX_REGION_HEAVY", "eu"),
                    )
                if "sonnet" in tier:
                    return (
                        os.environ.get("VERTEX_MODEL_LIGHT", "claude-sonnet-4-6"),
                        os.environ.get("VERTEX_REGION_LIGHT", "europe-west1"),
                    )
        return (None, None)

    def _run_once() -> tuple[dict[str, Any] | None, str | None, BaseException | None]:
        """Run claude once. Returns (envelope, raw_stdout, exc).

        Exactly one of the three return slots carries evidence:
          - exc set:      subprocess raised before producing any output (:289, :292)
          - raw set only: subprocess ran but stdout was empty or not JSON (:298, :303)
          - envelope set: subprocess ran and stdout parsed as JSON (success or error envelope)
        """
        model, region = _resolve_tier()
        args = list(bare_args)
        if model is not None:
            if "--model" in args:
                args[args.index("--model") + 1] = model
            else:
                args += ["--model", model]
        run_env = dict(os.environ)
        if region:
            run_env["CLOUD_ML_REGION"] = region
        try:
            proc = subprocess.run(
                [claude_command] + args,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=run_env,
            )
        except subprocess.TimeoutExpired as exc:
            logger.warning(f"Claude CLI timed out after {timeout}s")
            return (None, None, exc)
        except Exception as exc:  # noqa: BLE001 - log and degrade, never raise
            logger.error(f"Failed to invoke Claude CLI: {exc}")
            return (None, None, exc)
        raw = proc.stdout
        if not raw or not raw.strip():
            logger.warning(
                f"Claude CLI returned empty stdout. "
                f"stderr: {proc.stderr[:500] if proc.stderr else '(none)'}"
            )
            return (None, raw, None)
        try:
            return (json.loads(raw), raw, None)
        except json.JSONDecodeError:
            logger.warning(f"Claude CLI output was not a JSON envelope: {raw[:200]!r}")
            return (None, raw, None)

    # WALL CLOCK, not monotonic. PTS_LLM_DEADLINE is an absolute POSIX time
    # (a runner computes it as start + TimeoutStartSec - margin), so comparing it
    # against a monotonic now silently disables the budget check entirely.
    _env = os.environ if env is None else env
    now = time.time
    deadline = resolve_deadline(now(), _env)
    attempt = Attempt()

    while True:
        envelope, raw, exc = _run_once()
        outcome = _classify(envelope, raw, exc)
        attempt = attempt.bump(outcome)
        decision = decide(
            outcome, attempt, now(), deadline, float(timeout), is_linux=running_on_linux()
        )

        if decision.action is Action.RETURN:
            # envelope is guaranteed non-None by _classify: OK requires a dict with a
            # non-empty result field. The guard keeps mypy happy without masking bugs.
            return str(envelope.get("result")) if envelope is not None else None

        if decision.action in (Action.REAUTH_RETRY, Action.WAIT_FOR_PUSH):
            result = reauth()
            if result is not ReauthResult.SKIPPED:
                attempt = attempt.with_reauth_used()
            continue

        if decision.action is Action.PLAIN_RETRY:
            if decision.sleep_s:
                time.sleep(decision.sleep_s)
            continue

        logger.error("giving up: %s (%s)", decision.reason, outcome.value)
        return None


def _validate_synthesis(data: dict[str, Any]) -> list[str]:
    """Validate synthesis output structure. Returns list of issues (empty = valid)."""
    issues: list[str] = []

    if not isinstance(data.get("executive_brief"), list):
        issues.append("executive_brief must be a list")
    else:
        for i, item in enumerate(data["executive_brief"]):
            if isinstance(item, str):
                data["executive_brief"][i] = {"text": item, "article_ids": []}
            elif isinstance(item, dict):
                if "text" not in item:
                    issues.append(f"executive_brief[{i}] missing 'text'")
            else:
                issues.append(f"executive_brief[{i}] is {type(item).__name__}, expected dict")

    if not isinstance(data.get("sections"), list):
        issues.append("sections must be a list")
    else:
        required_section_keys = {"category", "display_name", "synthesis"}
        for i, section in enumerate(data["sections"]):
            if not isinstance(section, dict):
                issues.append(f"sections[{i}] is not a dict")
                continue
            missing = required_section_keys - set(section.keys())
            if missing:
                issues.append(f"sections[{i}] missing keys: {missing}")
            if "high_value" not in section:
                section["high_value"] = False
            if "article_ids" not in section:
                section["article_ids"] = []

    if "what_changed" not in data:
        data["what_changed"] = []
    elif isinstance(data["what_changed"], str):
        data["what_changed"] = [{"text": data["what_changed"], "article_ids": []}]

    return issues


def parse_synthesis_output(raw: str) -> dict[str, Any]:
    """Parse Claude's output, extracting JSON from various formats.

    Args:
        raw: Raw string output from Claude

    Returns:
        Parsed JSON dict, or fallback dict with error message
    """
    parsed = None

    # Try direct JSON parse
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code block
    if parsed is None:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

    # Try finding JSON object in text
    if parsed is None:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

    if parsed is None:
        preview = raw[:500] if raw else "(empty)"
        logger.error(f"Failed to parse Claude output as JSON. Raw output preview: {preview}")
        return {
            "executive_brief": ["Failed to parse synthesis output"],
            "what_changed": "Error occurred during synthesis",
            "sections": [],
            "error": "Parse failure",
        }

    # Validate and coerce structure
    issues = _validate_synthesis(parsed)
    if issues:
        logger.warning(
            f"Synthesis output has {len(issues)} validation issue(s): " + "; ".join(issues[:5])
        )

    return parsed


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

    raw_output = invoke_claude(
        prompt,
        timeout=timeout,
        claude_command=claude_command,
        claude_args=claude_args,
    )

    if raw_output is None:
        logger.warning("Synthesis failed: no output from Claude")
        logger.error("All synthesis attempts failed, using fallback digest")
        fallback = build_fallback_digest(articles)
        return (fallback, False)

    logger.info(f"Raw Claude output: {len(raw_output)} chars, starts with: {raw_output[:200]!r}")
    parsed = parse_synthesis_output(raw_output)

    # Check if parsing succeeded (no error key)
    if "error" not in parsed:
        logger.info("Synthesis succeeded")
        return (parsed, True)

    logger.warning("Synthesis failed: parse error")
    logger.error("All synthesis attempts failed, using fallback digest")
    fallback = build_fallback_digest(articles)
    return (fallback, False)
