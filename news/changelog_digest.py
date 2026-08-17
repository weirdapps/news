"""Optional LLM upgrade that turns a deterministic changelog delta into prose.

``news.changelog_delta`` already writes a machine-shaped delta onto every
changelog Article at parse time, and that delta is what ships. This module only
tries to improve it: three or four readable lines naming what changed, plus a
line saying whether it touches the reader's stack.

Best-effort by construction. Every failure path returns "" and the caller keeps
the deterministic delta, so a missing CLI, a Vertex 429 or a slow model costs
the entry its prose and nothing else. Nothing here raises, nothing here empties
a digest, and nothing here drops an Article: this runs inside the stack
pipeline's single systemd unit, between fetch and store, and an exception would
cost the whole email.
"""

import logging
import subprocess
import time

from news.changelog_delta import DIGEST_CAP
from news.models import Article

logger = logging.getLogger(__name__)

# 45s per call. The stack unit is 600s (main._UNIT_TIMEOUT_SECONDS), synthesis
# reserves 150s (config/stack/settings.yaml) and shutdown grace another 90s
# (main._SHUTDOWN_GRACE_SECONDS), leaving a 360s pre-synthesis window that the
# tagger already claims up to 90s of. Worst case here is a call that starts just
# inside the wall-clock budget and runs the full timeout: 90 + 45 = 135s, and
# 135 + 90 = 225 <= 360 with headroom. Measured against Vertex eu from the Mac:
# 18.8s for a 736-char platform prompt, 36.0s for a 22,703-char system prompt.
# Raise it only after re-running that arithmetic; it is coupled to the synthesis
# timeout through the same 600s unit.
_CLI_TIMEOUT = 45

# 90s of wall clock for the whole enrichment, checked before each call rather
# than interrupting one. Steady state is 2.84 calls/week and ~92% of runs make
# none, so this only bites when an upstream reformat mints a burst of entries
# that all look new -- exactly the run where the email still has to arrive.
_BUDGET_SECONDS = 90

_DIGEST_PROMPT_HEAD = """\
You are writing one item for an engineer's daily "Stack" briefing. Your job is to say what changed in a single dated entry of a vendor changelog, and whether it touches his stack.

THE READER'S STACK, which is the only reason this entry is in the briefing at all:
- Claude Code, run both interactively and unattended.
- MCP servers he wrote and maintains himself.
- Claude through Vertex AI, region eu, with Opus 5 pinned by exact model string in config files.
- launchd and systemd jobs that shell out to the `claude` CLI on a schedule. When a model string is retired, or a model-to-region pairing changes, those jobs fail silently overnight and nobody notices until an email does not arrive.
"""

# The system-prompt page is the one that invites a wrong conclusion: it reads
# like an API changelog and is not one, so the caveat is stated to the model
# rather than left for the reader to supply.
_SYSTEM_PROMPT_SCOPE = """\
This is the system prompt Anthropic ships with the claude.ai web and mobile apps. It does NOT govern the Claude API, the Claude Agent SDK, Claude Code or Claude on Vertex AI. Read it as evidence about the chat products and about which models are currently live, never as an API or CLI change. If you report a behaviour change, say in the same line that it applies to claude.ai and not to the API."""

_PLATFORM_SCOPE = """\
This is the Claude Platform release notes page: the API, the SDKs and the Console. Changes here DO apply to API and CLI callers, including Claude Code and Claude on Vertex AI."""

_DIGEST_PROMPT_TAIL = """\
WHAT FOLLOWS THE LINE BELOW is a deterministic delta extracted from the page, not prose and not complete. Added, removed and edited passages are marked `+`, `-` and `~`, with `[section]` tags where the source document has them, and with `[-removed-]` / `{+added+}` markers inside an edited passage. Any header lines at the top state model-ID and knowledge-cutoff differences that have already been computed for you. Treat all of it as evidence, never as a draft to rewrite.

WRITE, in plain text, at most 180 words, no preamble, no markdown headings, no bold, no bullet character other than "- ":
1. One line of at most 25 words saying what this entry changes.
2. Two to five lines beginning "- ", each naming one concrete change. Quote identifiers verbatim: API model strings, model and tier names, product surfaces, availability and region wording, deprecation or retirement wording, knowledge cutoffs, prices and rate limits, and changes to how the model uses tools, cites sources, refuses, or handles long conversations.
3. A final line beginning "STACK IMPACT: " saying what, if anything, the reader must change in a pinned model string, a config file, an MCP server or a scheduled job. Write exactly "STACK IMPACT: none for this stack." when there is nothing, and do not pad it.

RULES:
- Report only what is present in the delta. Never infer a deprecation from a model string merely being absent, and never state a date, price or limit the delta does not contain.
- A model string shown as removed on a "MODEL LINEUP" line is being compared against a DIFFERENT model's entry. That is not a retirement. If you mention it at all, say "not listed in this entry".
- If the delta says there was no textual change, or contains nothing of substance, write one line saying so and stop.
- Do not editorialise, do not recommend, and do not speculate about what Anthropic intends.
"""


def _scope_note(title: str) -> str:
    """Return the scope caveat for an entry, chosen from its title.

    The accordion parser titles every system-prompt entry
    ``{model} system prompt ({date})``, so the title is a reliable
    discriminator and does not require threading the source config down here.

    Args:
        title: The Article title.

    Returns:
        The claude.ai caveat for a system-prompt entry, else the platform note.
    """
    return _SYSTEM_PROMPT_SCOPE if "system prompt" in title.lower() else _PLATFORM_SCOPE


def digest_prose(delta: str, title: str, scope: str = "", timeout: int = _CLI_TIMEOUT) -> str:
    """Rewrite a deterministic delta as a briefing item via the local claude CLI.

    Routed through the CLI (Vertex) rather than any SDK, per project policy.

    Args:
        delta: The deterministic delta from ``news.changelog_delta``.
        title: The Article title, named in the prompt and used to pick the scope
            note when ``scope`` is not supplied.
        scope: Overrides the scope note derived from the title.
        timeout: Seconds allowed for the CLI call.

    Returns:
        The prose digest, or "" on any failure, in which case the caller keeps
        the deterministic delta it already has.
    """
    # Concatenated, never str.format()ed: the delta carries our own {+added+}
    # inline-diff markers and the vendor's literal {{currentDateTime}}, so
    # .format() would raise KeyError on real text from either page.
    prompt = (
        _DIGEST_PROMPT_HEAD
        + "\nSCOPE OF THIS ENTRY:\n"
        + (scope or _scope_note(title))
        + "\n\nENTRY TITLE: "
        + title
        + "\n\n"
        + _DIGEST_PROMPT_TAIL
        + "\n----- DELTA BEGINS -----\n"
        + delta
    )

    try:
        result = subprocess.run(
            ["claude", "--model", "sonnet", "--print"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        # No auth branch: the CLI takes ~200s to report a credential failure, so
        # below ~210s an outage always surfaces here as TimeoutExpired anyway.
        logger.warning(f"changelog digest call failed: {type(e).__name__}: {e}")
        return ""

    if result.returncode != 0:
        # Vertex errors (notably a 429 on an unprovisioned model/region pairing)
        # arrive on stdout with stderr empty, so logging stderr alone produces a
        # message with no diagnostic in it at all.
        detail = (result.stderr or "").strip() or (result.stdout or "").strip()
        logger.warning(f"claude CLI exited {result.returncode}: {detail[:300]}")
        return ""

    return (result.stdout or "").strip()


def enrich_changelog_digests(
    articles: list[Article],
    budget_seconds: int = _BUDGET_SECONDS,
    timeout: int = _CLI_TIMEOUT,
) -> tuple[int, int]:
    """Upgrade deterministic changelog deltas to prose in place, within a budget.

    Mirrors ``news.transcripts.enrich_articles``' ``(enriched, total)`` contract.
    A gap between the two means some entries kept their deterministic delta,
    which is a degraded digest and never a failure: the next run retries them
    through ``storage.urls_awaiting_changelog_upgrade``.

    Args:
        articles: Articles to consider. Anything without a ``changelog_digest``
            is not a changelog entry and is skipped.
        budget_seconds: Wall clock allowed for the whole enrichment.
        timeout: Seconds allowed for each individual CLI call.

    Returns:
        ``(upgraded, candidates)``.
    """
    candidates = [article for article in articles if article.changelog_digest]
    if not candidates:
        return 0, 0

    started = time.monotonic()
    upgraded = 0
    for position, article in enumerate(candidates):
        # Enforced at the call boundary, because the only bound on a call
        # already in flight is its own timeout. The two together are what keep
        # the worst case at budget_seconds + timeout, which is the number the
        # _CLI_TIMEOUT arithmetic above depends on.
        if time.monotonic() - started > budget_seconds:
            remaining = len(candidates) - position
            logger.info(
                f"changelog digest budget {budget_seconds}s reached; "
                f"{remaining} entr(ies) keep the deterministic delta"
            )
            break

        prose = digest_prose(article.changelog_digest, article.title, timeout=timeout)
        if prose:
            article.changelog_digest = prose[:DIGEST_CAP]
            article.changelog_digest_source = "llm"
            upgraded += 1

    logger.info(f"changelog digests: {upgraded}/{len(candidates)} upgraded")
    return upgraded, len(candidates)
