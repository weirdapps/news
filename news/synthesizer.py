"""AI synthesis layer using Claude CLI — AI-curated news selection."""

import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
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
    trace,
)
from news.roster import build_roster

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent


def _trace_path(env: Mapping[str, str]) -> Path:
    """Where the per-call decision record lands. Overridable so a test never writes to data/."""
    override = env.get("NEWS_LLM_TRACE")
    return Path(override) if override else _PROJECT_ROOT / "data" / "llm_trace.jsonl"


def _envelope_tokens(envelope: dict[str, Any] | None) -> tuple[int | None, int | None]:
    """Pull (in_tok, out_tok) from the CLI envelope's usage block.

    Defensive on every hop: the field is absent on every failure path, absent from
    the test doubles, and its shape is the CLI's to change. Telemetry must not be
    the thing that fells a synthesis run, so an unexpected shape yields (None, None)
    rather than raising -- `trace` omits the keys entirely when they are None.
    """
    if not isinstance(envelope, dict):
        return (None, None)
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        return (None, None)

    def _int(value: Any) -> int | None:
        return value if isinstance(value, int) else None

    return (_int(usage.get("input_tokens")), _int(usage.get("output_tokens")))


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


def _validate_or_reject(validate: Callable[[str], bool], text: str) -> bool:
    """Run a caller's content check. A raising validator means "not valid", never a crash.

    All five profiles pass ``lambda text: "error" not in parse_synthesis_output(text)``,
    and that callback is not total: a model returning a top-level JSON array parses
    fine, then makes ``_validate_synthesis`` call ``.get`` on a list. The resulting
    AttributeError used to unwind out of ``invoke_claude`` — which is declared
    ``-> str | None`` — past ``synthesize()`` to main.py's top-level handler and
    ``sys.exit(1)``. Synthesis runs before the alert-email step, so the run died in
    exactly the failure mode the per-slot alert contract exists to cover.

    The catch is deliberately broad. A validator that cannot inspect the text at all
    is making the same statement as one that inspected it and said no, and the loop's
    job is to survive whatever the model returns. Narrowing this to the AttributeError
    observed today would just wait for the next output shape. ``Exception``, not
    ``BaseException``: a KeyboardInterrupt or SystemExit must still end the run.
    """
    try:
        return validate(text)
    except Exception as exc:  # noqa: BLE001 - a broken check means unusable text, not a dead run
        logger.warning(
            "validate callback raised %s: %s — treating as UNPARSEABLE", type(exc).__name__, exc
        )
        return False


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
    validate: Callable[[str], bool] | None = None,
    job: str = "unknown",
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
        validate: Optional content-level check. Called with the result text when
            _classify returns OK. If it returns False, the result is reclassified as
            UNPARSEABLE so the policy retries under that row's cap instead of returning
            text the caller cannot use. Restores the content-level retry that the old
            outer per-profile loops provided when parse_synthesis_output returned
            a dict containing an "error" key.
        job: Profile name recorded in the trace record, so a JSONL line can be
            attributed to the run that produced it. Defaults to "unknown" rather than
            being required, because an unattributed record still beats no record.
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

    # Resolved once: _resolve_tier is a pure function of bare_args, which the loop
    # never mutates. Hoisted so the trace record can name the model/region that was
    # actually used even on the paths where _run_once returns before reaching them.
    traced_model, traced_region = _resolve_tier()
    trace_path = _trace_path(_env)

    while True:
        call_started = now()
        envelope, raw, exc = _run_once()
        latency_ms = int((now() - call_started) * 1000)
        outcome = _classify(envelope, raw, exc)

        # Content-level validation: the transport succeeded and the envelope parsed,
        # but the caller's domain schema rejects the result text. Reclassify as
        # UNPARSEABLE so the policy uses that row's cap instead of returning a value
        # the caller cannot use. This restores the one content-level retry the old
        # outer per-profile loops provided when parse_synthesis_output returned
        # {"error": ...}.
        if outcome is Outcome.OK and validate is not None:
            result_text = str(envelope.get("result")) if envelope is not None else None
            if result_text is not None and not _validate_or_reject(validate, result_text):
                outcome = Outcome.UNPARSEABLE

        attempt = attempt.bump(outcome)
        decision = decide(
            outcome, attempt, now(), deadline, float(timeout), is_linux=running_on_linux()
        )

        # One record per attempt, not per call: a run that retries twice should be
        # legible as three lines with the same job, not one summary that hides the
        # retries. `trace` swallows its own failures by contract.
        in_tok, out_tok = _envelope_tokens(envelope)
        trace(
            trace_path,
            job=job,
            call_site="synthesizer.invoke_claude",
            model=traced_model or "unresolved",
            region=traced_region or "inherited",
            outcome=outcome,
            action=decision.action,
            attempt=attempt,
            latency_ms=latency_ms,
            ts=now(),
            in_tok=in_tok,
            out_tok=out_tok,
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


# Top-level keys whose absence (or wrong type) makes a payload unrenderable. This is
# per-profile, not global: the digest family (digest, market, stack, topic) renders
# `sections`, the brand monitor does not — its schema carries company_mentions,
# alerts and competitor_watch instead. Validating the monitor against this set
# rejected every well-formed monitor payload it ever produced.
DIGEST_REQUIRED_KEYS = ("executive_brief", "sections")


def _validate_synthesis(
    data: dict[str, Any], required: tuple[str, ...] = DIGEST_REQUIRED_KEYS
) -> list[str]:
    """Validate and repair synthesis structure in place. Returns FATAL issues only.

    Three outcomes, deliberately distinguished, because the old code collapsed them
    into one list that the caller logged and then ignored -- so a malformed bullet
    and an unusable payload both shipped:

      - REPAIRED: a coercion this function can make safely (a bare string bullet, a
        missing ``high_value``). Silent, as before.
      - DROPPED: one malformed item among good ones. The item is removed and named
        in the log; the digest still goes out. Rejecting the whole synthesis over a
        single bad section would trade a good digest for a plain-text fallback.
      - FATAL (returned): the payload cannot be rendered at all. The caller turns
        this into an ``error`` key, which makes ``invoke_claude``'s validate callback
        fail, which reclassifies the turn as UNPARSEABLE and spends a retry -- the
        thing that never used to happen.

    ``required`` decides which missing/mistyped top-level keys are FATAL rather than
    ignorable. It exists because FATAL is only meaningful against the schema the
    calling profile actually asked the model for: a monitor payload has no
    ``sections`` by design, and judging it against the digest's set condemned every
    one of them.
    """
    fatal: list[str] = []
    dropped: list[str] = []

    brief = data.get("executive_brief")
    if not isinstance(brief, list):
        if "executive_brief" in required:
            fatal.append(f"executive_brief is {type(brief).__name__}, expected list")
    else:
        kept_brief = []
        for i, item in enumerate(brief):
            if isinstance(item, str):
                kept_brief.append({"text": item, "article_ids": []})
            elif isinstance(item, dict) and "text" in item:
                kept_brief.append(item)
            else:
                dropped.append(f"executive_brief[{i}] ({type(item).__name__})")
        data["executive_brief"] = kept_brief

    sections = data.get("sections")
    if not isinstance(sections, list):
        if "sections" in required:
            fatal.append(f"sections is {type(sections).__name__}, expected list")
    else:
        required_section_keys = {"category", "display_name", "synthesis"}
        kept_sections = []
        for i, section in enumerate(sections):
            if not isinstance(section, dict):
                dropped.append(f"sections[{i}] ({type(section).__name__})")
                continue
            missing = required_section_keys - set(section.keys())
            if missing:
                dropped.append(f"sections[{i}] missing {sorted(missing)}")
                continue
            section.setdefault("high_value", False)
            section.setdefault("article_ids", [])
            kept_sections.append(section)
        data["sections"] = kept_sections

    if "what_changed" not in data:
        data["what_changed"] = []
    elif isinstance(data["what_changed"], str):
        data["what_changed"] = [{"text": data["what_changed"], "article_ids": []}]

    # Deliberately NOT fatal: a well-formed but empty synthesis. The prompt tells the
    # model to skip sections when nothing meaningful happened, so empty is an
    # editorial verdict, not a schema failure. Rejecting it spends a retry and then
    # ships the plain-text "SYNTHESIS UNAVAILABLE" dump, which is worse than the
    # quiet digest it replaced. Whether to email an empty digest is main.py's call.
    if dropped:
        logger.warning(
            "Synthesis: dropped %d malformed item(s): %s", len(dropped), "; ".join(dropped[:5])
        )

    return fatal


def parse_synthesis_output(
    raw: str, required: tuple[str, ...] = DIGEST_REQUIRED_KEYS
) -> dict[str, Any]:
    """Parse Claude's output, extracting JSON from various formats.

    Args:
        raw: Raw string output from Claude
        required: Top-level keys this profile cannot render without. Defaults to
            the digest family's set; the brand monitor passes its own
            (news.monitor_synth.MONITOR_REQUIRED_KEYS).

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

    if parsed is None or not isinstance(parsed, dict):
        preview = raw[:500] if raw else "(empty)"
        # A top-level JSON array parses cleanly and then makes _validate_synthesis
        # call .get on a list. _validate_or_reject catches that AttributeError so a
        # run cannot die of it, but a function annotated `-> dict[str, Any]` should
        # not need catching: reject the shape here, at the only place that knows it.
        logger.error(f"Failed to parse Claude output as JSON object. Preview: {preview}")
        return {
            "executive_brief": ["Failed to parse synthesis output"],
            "what_changed": "Error occurred during synthesis",
            "sections": [],
            "error": "Parse failure",
        }

    # Validate, repair in place, and reject what cannot be rendered. The `error` key
    # is the contract every profile's validate callback checks, so setting it here is
    # what converts a structural failure into a retry instead of a malformed email.
    fatal = _validate_synthesis(parsed, required)
    if fatal:
        # Name the keys the model DID return. Without them a schema rejection is
        # indistinguishable from a model outage in the log, and the run that
        # exposed this validated the wrong profile's schema for two hours before
        # anyone could tell.
        observed = sorted(parsed.keys()) if isinstance(parsed, dict) else type(parsed).__name__
        logger.error(
            "Synthesis output unusable: %s (required=%s, model returned %s)",
            "; ".join(fatal),
            list(required),
            observed,
        )
        parsed["error"] = "Schema failure: " + "; ".join(fatal)

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
        validate=lambda text: "error" not in parse_synthesis_output(text),
        job="digest",
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
