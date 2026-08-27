"""Veracity review: a second pass that checks claims against the articles they cite.

WHY THIS EXISTS

Until now the digest's "FACT CHECK" and "OPPOSING VIEWS" were two string fields in
the same JSON object as the claims, produced by the same model in the same forward
pass. Generation is autoregressive, so those fields are emitted *after* the claims
and conditioned on them: they can hedge, they cannot refute. The 2026-08-27 stack
brief is the worked example. Its executive brief asserted that four companies "all
converged on the same production agent pattern", and its own fact_check noted the
figures were "self-reported by the practitioners on stage" -- without ever drawing
the conclusion that four vendors on one conference track is selection bias. Nothing
reconciled the two, because nothing was ever asked to.

``citation_filter`` is the deterministic half of the same control and it is real,
but ``_valid_ids`` only asks whether an integer is in range. With 847 articles in
the pool every integer under 847 is in range, so on a large digest it is close to a
no-op. It proves a citation EXISTS. It cannot prove the cited article SUPPORTS the
claim. That is the gap this module closes, and it is why the reviewer runs before
citation_filter rather than after: filter_unsourced_bullets flattens bullets to
plain strings, destroying the article_ids this needs.

DESIGN CONSTRAINTS

The reviewer must never make the pipeline more fragile than it was. It runs
unattended on a VPS timer with no one watching, so every failure path returns the
synthesis UNCHANGED rather than raising or emptying it. It is a filter that can
only ever remove individual unsupported claims, never the digest.

The strike ceiling is the important guard. A reviewer that hallucinates, or that
misreads the task and rejects everything, would silently gut a good digest. If it
wants to strike more than STRIKE_CEILING of the claims, the far likelier
explanation is that the reviewer is wrong, not that the synthesis is. In that case
we keep everything and log loudly, which turns a silent evisceration into a visible
anomaly.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from news.synthesizer import invoke_claude, parse_synthesis_output

logger = logging.getLogger(__name__)

# Fields whose entries are {text, article_ids} dicts and are individually strikable.
_BULLET_FIELDS = ("executive_brief", "try_this", "recommendations", "what_changed", "alerts")

# Above this fraction, disbelieve the reviewer rather than the synthesis.
STRIKE_CEILING = 0.5

# The review's OWN output schema. It shares parse_synthesis_output with the synthesis
# profiles, and that function defaults to the digest's required keys, so without this
# the reviewer's {verdicts, contradictions} payload was judged against
# {executive_brief, sections} and logged
#   "Synthesis output unusable: executive_brief is NoneType ... model returned
#    ['contradictions','verdicts','what_changed']"
# at ERROR, twice, on every single review call. The review itself still succeeded --
# the callback only ever inspected `verdicts` -- so this was pure false alarm in the
# logs of the very stage whose job is catching false claims. Observed live on the
# monitor run of 2026-08-27 20:03.
#
# Exactly the same defect as the monitor regression: a validator that does not know
# which schema it is validating. Third instance in this file's short life, which is
# why the default argument is the thing to distrust, not the caller.
REVIEW_REQUIRED_KEYS = ("verdicts",)

# Per cited article. Enough to judge support, small enough that the review prompt
# stays a fraction of the ~62k-char synthesis prompt it follows.
_SNIPPET_CHARS = 600

_SYSTEM_PROMPT = """You are a verification reviewer for a daily intelligence brief. \
You did NOT write the claims below and you have no stake in them.

For each CLAIM you are given the exact articles it cites. Your only question, for \
each one, is narrow and factual:

  Does at least one cited article actually support this claim as stated?

Rules:
- Judge ONLY against the cited articles shown. You have no other evidence. Do not \
use your own knowledge of the world to support OR to reject a claim.
- "Supported" means the article states it, or the claim is a fair summary of what \
the article states. Reasonable paraphrase and aggregation across the cited articles \
are fine.
- Mark supported=false when the cited articles do not contain the claim, when the \
claim materially overstates them (a vendor's own benchmark reported as fact, a \
projection reported as a result, "all X did Y" from a sample of the cited set), or \
when it attributes something to a source that did not say it.
- If you are UNSURE, mark supported=true. A false strike removes correct \
information from the reader; a missed strike leaves a hedge in place. Prefer the \
second error.
- Do not rewrite, improve, shorten or comment on style. You only judge support.

Separately, report CONTRADICTIONS: places where the brief's own fact_check or \
opposing_views text undercuts a claim the brief states flatly, without the brief \
acknowledging it. This is the specific failure this review exists to catch.

Return ONLY valid JSON, no preamble and no markdown fence:

{"verdicts": [{"id": 0, "supported": true, "reason": "one short clause"}], \
"contradictions": ["one sentence naming the claim and what undercuts it"]}

Every claim id you were given must appear exactly once in verdicts."""


def _article_ids(entry: Any) -> list[int]:
    if not isinstance(entry, dict):
        return []
    raw = entry.get("article_ids")
    if not isinstance(raw, list):
        return []
    return [i for i in raw if isinstance(i, int)]


def collect_claims(synthesis: dict[str, Any]) -> list[dict[str, Any]]:
    """Every individually-strikable claim, tagged with where it came from.

    Sections carry their prose in `synthesis` rather than `text`, so they are
    collected separately. An entry with no valid citation is skipped entirely:
    citation_filter already drops those, and asking the reviewer to judge a claim
    with nothing to judge it against wastes tokens and invites a guess.
    """
    claims: list[dict[str, Any]] = []
    for field in _BULLET_FIELDS:
        for index, entry in enumerate(synthesis.get(field) or []):
            ids = _article_ids(entry)
            text = entry.get("text") if isinstance(entry, dict) else None
            if ids and isinstance(text, str) and text.strip():
                claims.append({"field": field, "index": index, "text": text, "ids": ids})

    for index, section in enumerate(synthesis.get("sections") or []):
        if not isinstance(section, dict):
            continue
        ids = _article_ids(section)
        text = section.get("synthesis")
        if ids and isinstance(text, str) and text.strip():
            claims.append({"field": "sections", "index": index, "text": text, "ids": ids})
    return claims


def build_review_prompt(claims: list[dict[str, Any]], articles: list[Any]) -> str:
    """Claims plus ONLY their cited articles.

    Deliberately not the whole corpus. The reviewer's job is to check a claim
    against what it cited, and handing it all 847 articles would both blow the
    prompt budget and let it justify a claim from something the writer never read.
    """
    cited: dict[int, Any] = {}
    payload = []
    for claim_id, claim in enumerate(claims):
        for i in claim["ids"]:
            if 0 <= i < len(articles):
                cited[i] = articles[i]
        payload.append(
            {
                "id": claim_id,
                "claim": claim["text"],
                "cites": [i for i in claim["ids"] if 0 <= i < len(articles)],
            }
        )

    article_payload = [
        {
            "id": i,
            "title": getattr(a, "title", ""),
            "source": getattr(a, "source", ""),
            "text": (
                getattr(a, "transcript_abstract", "")
                or getattr(a, "content", "")
                or getattr(a, "summary", "")
            )[:_SNIPPET_CHARS],
        }
        for i, a in sorted(cited.items())
    ]

    return f"""{_SYSTEM_PROMPT}

**CITED ARTICLES:**
{json.dumps(article_payload, ensure_ascii=False)}

**CLAIMS:**
{json.dumps(payload, ensure_ascii=False)}

Return the JSON verdict object for all {len(payload)} claims."""


def _apply(synthesis: dict[str, Any], claims: list[dict[str, Any]], struck: set[int]) -> int:
    """Remove struck claims. Returns how many were actually removed.

    Removal is by descending index within each field so earlier indices stay valid.
    """
    removed = 0
    by_field: dict[str, list[int]] = {}
    for claim_id in struck:
        claim = claims[claim_id]
        by_field.setdefault(claim["field"], []).append(claim["index"])

    for field, indices in by_field.items():
        target = synthesis.get(field)
        if not isinstance(target, list):
            continue
        for index in sorted(indices, reverse=True):
            if 0 <= index < len(target):
                del target[index]
                removed += 1
    return removed


def review_and_log(
    synthesis: dict[str, Any],
    articles: list[Any],
    *,
    job: str,
    synthesis_config: dict[str, Any],
) -> dict[str, Any]:
    """Run the review for one profile and log the outcome. Returns the synthesis.

    Exists so the three rendering pipelines call one line instead of pasting the
    same eighteen. main.py already carries five near-identical synthesize_* tails
    and four render_*_html copies; adding a fourth-and-fifth copy of this would be
    repeating the mistake, and it would also park the logic in the one module the
    test suite barely reaches.
    """
    synthesis, stats = review_synthesis(
        synthesis,
        articles,
        job=job,
        timeout=synthesis_config.get("review_timeout", 180),
        claude_command=synthesis_config.get("claude_command", "claude"),
        claude_args=synthesis_config.get("claude_args", []),
    )
    logger.info(
        "Veracity review [%s]: reviewed=%s claims=%d struck=%d reason=%s",
        job,
        stats["reviewed"],
        stats["claims"],
        stats["struck"],
        stats["reason"] or "-",
    )
    return synthesis


def review_synthesis(
    synthesis: dict[str, Any],
    articles: list[Any],
    *,
    job: str = "unknown",
    timeout: int = 180,
    claude_command: str = "claude",
    claude_args: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Strike claims their own citations do not support. Returns (synthesis, stats).

    Never raises, never empties the digest. On any failure the synthesis comes back
    exactly as it went in, with stats saying why.
    """
    stats: dict[str, Any] = {
        "reviewed": False,
        "claims": 0,
        "struck": 0,
        "contradictions": [],
        "reason": "",
    }

    claims = collect_claims(synthesis)
    stats["claims"] = len(claims)
    if not claims:
        stats["reason"] = "no cited claims to review"
        return synthesis, stats

    raw = invoke_claude(
        build_review_prompt(claims, articles),
        timeout=timeout,
        claude_command=claude_command,
        claude_args=claude_args,
        validate=lambda text: isinstance(
            parse_synthesis_output(text, required=REVIEW_REQUIRED_KEYS).get("verdicts"), list
        ),
        job=f"{job}-review",
    )
    if raw is None:
        # Degrade to the unreviewed digest. An unreviewed brief is what shipped
        # every day until now; a missing brief is a regression.
        stats["reason"] = "reviewer call failed"
        logger.warning("Veracity review unavailable for %s; shipping unreviewed", job)
        return synthesis, stats

    # required= again: this is the SECOND call on the review payload and the one the
    # log actually came from. Fixing only the validate callback above left this one
    # emitting the same false ERROR, which is what the end-to-end test caught.
    parsed = parse_synthesis_output(raw, required=REVIEW_REQUIRED_KEYS)
    verdicts = parsed.get("verdicts")
    if not isinstance(verdicts, list):
        stats["reason"] = "reviewer returned no verdicts list"
        logger.warning("Veracity review for %s returned no verdicts; shipping unreviewed", job)
        return synthesis, stats

    struck: set[int] = set()
    reasons: list[str] = []
    for verdict in verdicts:
        if not isinstance(verdict, dict) or verdict.get("supported") is not False:
            continue
        claim_id = verdict.get("id")
        # Anything not explicitly and validly struck is kept. Silence is not a strike.
        if isinstance(claim_id, int) and 0 <= claim_id < len(claims):
            struck.add(claim_id)
            reasons.append(f"[{claims[claim_id]['field']}] {str(verdict.get('reason', ''))[:120]}")

    contradictions = parsed.get("contradictions")
    if isinstance(contradictions, list):
        stats["contradictions"] = [str(c) for c in contradictions if isinstance(c, str)][:5]

    if struck and len(struck) / len(claims) > STRIKE_CEILING:
        # Disbelieve the reviewer, not the synthesis. See module docstring.
        stats["reviewed"] = True
        stats["reason"] = (
            f"reviewer struck {len(struck)}/{len(claims)} claims, above the "
            f"{STRIKE_CEILING:.0%} ceiling — distrusting the review, keeping all claims"
        )
        logger.error("Veracity review for %s: %s", job, stats["reason"])
        return synthesis, stats

    stats["struck"] = _apply(synthesis, claims, struck)
    stats["reviewed"] = True
    if stats["struck"]:
        logger.warning(
            "Veracity review struck %d/%d claim(s) for %s: %s",
            stats["struck"],
            len(claims),
            job,
            "; ".join(reasons[:5]),
        )
    else:
        logger.info("Veracity review passed all %d claim(s) for %s", len(claims), job)
    if stats["contradictions"]:
        logger.warning(
            "Veracity review flagged %d brief/fact-check contradiction(s) for %s: %s",
            len(stats["contradictions"]),
            job,
            "; ".join(stats["contradictions"][:3]),
        )
    return synthesis, stats
