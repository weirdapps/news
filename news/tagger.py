"""Ticker tagger: rules first, optional Haiku fallback.

The rules layer matches:
  - Cashtags ($AAPL, $MSFT) — high confidence
  - Company names from a curated dict (apple -> AAPL) — word-boundary match

The Haiku fallback (separate function) is invoked only for articles in
market-adjacent categories where the rules layer found nothing — to keep
cost low while improving recall on names not in the dictionary.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from news.auth import refresh_auth
from news.llm_policy import running_on_linux

logger = logging.getLogger(__name__)

CASHTAG_RE = re.compile(r"\$([A-Z][A-Z0-9.\-]{0,5})\b")


@lru_cache(maxsize=1)
def load_ticker_dict() -> dict[str, str]:
    """Load name -> ticker mapping from config/tickers.yaml."""
    path = Path(__file__).parent.parent / "config" / "tickers.yaml"
    if not path.exists():
        return {}
    with path.open() as f:
        return yaml.safe_load(f).get("tickers", {})


@lru_cache(maxsize=4)
def _compile_dict_patterns(dict_id: int, dict_keys_tuple: tuple) -> tuple:
    """Compile two regex alternations: short keys (≤3 chars) and long keys (≥4 chars).

    Sorted longest-first so longer alternatives win in regex matching.
    Returns (short_pattern, long_pattern). Either can be None if no keys in that bucket.

    The dict_id arg is just for cache-key uniqueness; the actual regex is built from keys_tuple.
    """
    short_keys = sorted([k for k in dict_keys_tuple if len(k) <= 3], key=len, reverse=True)
    long_keys = sorted([k for k in dict_keys_tuple if len(k) > 3], key=len, reverse=True)

    short_pattern = None
    if short_keys:
        # Short keys must match in original text in UPPERCASE form
        short_alt = "|".join(re.escape(k.upper()) for k in short_keys)
        short_pattern = re.compile(r"\b(" + short_alt + r")\b")

    long_pattern = None
    if long_keys:
        # Long keys: case-insensitive search; capitalization check happens after match
        long_alt = "|".join(re.escape(k) for k in long_keys)
        long_pattern = re.compile(r"\b(" + long_alt + r")\b", re.IGNORECASE)

    return (short_pattern, long_pattern)


def _extract_cashtags(text: str) -> set[str]:
    """Extract cashtag ($TICKER) mentions from text."""
    return {m.group(1).upper() for m in CASHTAG_RE.finditer(text)}


def _extract_short_keys(text: str, pattern, ticker_dict: dict[str, str]) -> set[str]:
    """Extract tickers from short keys (ALL-CAPS required)."""
    found = set()
    if pattern:
        for m in pattern.finditer(text):
            ticker = ticker_dict.get(m.group(1).lower())
            if ticker:
                found.add(ticker)
    return found


def _extract_long_keys(text: str, pattern, ticker_dict: dict[str, str]) -> set[str]:
    """Extract tickers from long keys (capitalized first letter required)."""
    found = set()
    if pattern:
        for m in pattern.finditer(text):
            if text[m.start()].isupper():
                ticker = ticker_dict.get(m.group(1).lower())
                if ticker:
                    found.add(ticker)
    return found


def extract_tickers_rules(text: str, ticker_dict: dict[str, str]) -> list[str]:
    """Return sorted unique uppercase tickers found in text via rules.

    Optimized: uses TWO pre-compiled alternation regexes (short + long keys)
    for O(text) match time instead of O(text × keys).

    Cashtags ($AAPL) — always captured.
    Long names (≥4 chars): case-insensitive match, requires capital first letter in original.
    Short names (≤3 chars): require ALL-CAPS match (proper-noun heuristic for ticker mentions).
    """
    found = _extract_cashtags(text)

    if not ticker_dict:
        return sorted(found)

    short_pat, long_pat = _compile_dict_patterns(id(ticker_dict), tuple(ticker_dict.keys()))

    found.update(_extract_short_keys(text, short_pat, ticker_dict))
    found.update(_extract_long_keys(text, long_pat, ticker_dict))

    return sorted(found)


# One re-auth attempt per process. A per-article budget would call the login
# script once per article during an outage — same problem as the five nested
# retry loops that Task 3 removed. Resettable for tests.
_reauth_attempted = False


def _reset_reauth_latch() -> None:
    """Reset the per-process reauth latch. For tests only."""
    global _reauth_attempted
    _reauth_attempted = False


def _invoke_once(cmd: list[str], prompt: str, timeout: int) -> dict[str, Any] | None:
    """Run the claude CLI once. Returns the parsed JSON envelope, or None on failure.

    Every None return logs at ERROR, and the four causes are distinguishable in the
    log. A blanket ``except Exception: return None`` used to collapse them, which
    mattered most for TimeoutExpired: spec §15 measured the CLI taking 200.6 s to
    report ``invalid_grant``, because it retries the credential refresh internally.
    At this module's 30 s default the subprocess therefore times out long before the
    auth envelope arrives, so a credential outage reached the caller as a silent []
    and never touched the auth branch below. The timeout IS the observable shape of
    that outage here, and it is the record an operator has to be able to find.

    A timeout is deliberately NOT inferred to be an auth error. A slow model raises
    the identical exception, and acting on the guess would spend the one-shot re-auth
    budget on a hunch.
    """
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.error(
            "tagger: claude CLI timed out after %ss — article tagged empty. "
            "Repeated across articles this is the signature of a credential outage, "
            "which the CLI takes ~200s to report.",
            timeout,
        )
        return None
    except Exception as exc:  # noqa: BLE001 - log and degrade, never raise
        logger.error("tagger: could not invoke claude CLI (%s) — article tagged empty", exc)
        return None
    if proc.returncode != 0:
        logger.error(
            "tagger: claude CLI exited %s — article tagged empty. stderr: %s",
            proc.returncode,
            (proc.stderr or "")[:300] or "(none)",
        )
        return None
    raw = (proc.stdout or "").strip()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.error(
            "tagger: claude CLI output was not JSON (%r) — article tagged empty", raw[:200]
        )
        return None


def _tickers_from_envelope(envelope: dict[str, Any] | None) -> list[str]:
    """Extract tickers from a parsed envelope. Returns [] on any failure."""
    if envelope is None or envelope.get("is_error"):
        return []
    result = str(envelope.get("result", "")).strip()
    # Be lenient: strip markdown fences if the model wraps the JSON
    if result.startswith("```"):
        result = result.strip("`").lstrip("json").strip()
    try:
        data = json.loads(result)
    except (json.JSONDecodeError, ValueError):
        return []
    tickers = data.get("tickers", [])
    if not isinstance(tickers, list):
        return []
    return sorted({t.upper() for t in tickers if isinstance(t, str)})


_AUTH_ERROR_MARKERS = ("invalid_rapt", "invalid_grant", "reauth", "unauthenticated")


def _is_auth_error(env: dict[str, Any] | None) -> bool:
    """True if the envelope is an error AND looks like a gcloud auth-class failure."""
    if not env or not env.get("is_error"):
        return False
    result = str(env.get("result", "")).lower()
    return any(marker in result for marker in _AUTH_ERROR_MARKERS)


_TAGGER_PROMPT = """Extract stock tickers explicitly mentioned in this news article.

Rules:
- Return ONLY tickers for publicly-traded companies that the article is actually about (subject of the story, not passing mentions).
- Use canonical NYSE/NASDAQ ticker format (e.g. AAPL, MSFT, GOOG, BRK.B).
- For non-US listings, use the ticker as it appears in the article.
- If the article mentions no specific company, return an empty list.

Output STRICT JSON only, no prose:
{"tickers": ["AAPL", "MSFT"]}

Article:
"""


def extract_tickers_llm(
    text: str,
    model: str = "sonnet",
    max_chars: int = 4000,
    timeout: int = 30,
) -> list[str]:
    """Call the local `claude` CLI to extract tickers. Returns sorted unique uppercase list.

    Routes via Vertex AI (corporate-billed) — never the anthropic SDK with a personal API key.
    Returns [] on any error. On a credential failure, attempts one re-auth on macOS (using the
    module-level one-shot latch) before giving up. On Linux, the wait for the Mac's token push
    would exceed TimeoutStartSec (600 s) and trigger SIGKILL, so re-auth is skipped and the
    article is tagged empty. Every give-up path logs at ERROR so the run is not silent.

    ``timeout`` stays at 30 s, and that is a ruling rather than a leftover. Spec §15
    measured the CLI taking 200.6 s to report ``invalid_grant``, so at 30 s the auth
    branch below is unreachable for the failure that actually occurs: the subprocess
    times out first. Raising the timeout past ~210 s would make it reachable, and is
    still wrong. All five news pipelines run only on the VPS, where that branch does
    nothing but log — ``running_on_linux()`` fast-fails without re-authing — while the
    cost is paid per article on every host. This function is called once per
    market-adjacent article with no rules hit, measured at 10-81 per day and routinely
    ~20 in a single digest run, so an outage would grow from ~600 s of timeouts to
    ~4000 s, which SIGTERMs even the 2400 s digest before its alert email. Task 6's
    mandate was to stop degrading silently; ``_invoke_once``'s ERROR log delivers that
    for a seventh of the wall clock. Re-auth on a credential outage remains synthesis's
    job, where the policy loop and the per-slot alert live.
    """
    global _reauth_attempted

    prompt = _TAGGER_PROMPT + text[:max_chars]
    cmd = ["claude", "--model", model, "--print", "--output-format", "json"]

    envelope = _invoke_once(cmd, prompt, timeout)

    if not _is_auth_error(envelope):
        return _tickers_from_envelope(envelope)

    # Auth failure path — mirrors check_gcloud_auth's Linux guard and refresh delegation
    if running_on_linux():
        # No remedy on the VPS: waiting for the Mac's 15-minute token push can exceed
        # TimeoutStartSec (600 s) and SIGKILL the service.
        logger.error(
            "tagger: credential error on Linux — no re-auth possible, article tagged empty"
        )
        return []

    if _reauth_attempted:
        logger.error("tagger: credential error; re-auth budget exhausted — article tagged empty")
        return []

    # The latch is burned BEFORE the call, so a SKIPPED re-auth spends it too. That
    # is deliberate, not an oversight: refresh_auth() collapses SKIPPED into False
    # (news/auth.py:51-59), and each of the three SKIPPED causes is fine with a spent
    # latch. Cooldown — every later attempt in this process hits the same cooldown, so
    # the latch correctly prevents futile calls. Credentials already valid, or another
    # process mid-auth — the next article's call simply succeeds and never reaches this
    # path, so the latch is irrelevant. Telling the cases apart would mean calling
    # llm_policy.reauth() directly and reading the enum, which breaks the refresh_auth()
    # abstraction that exists to hide exactly that. Do not "fix" this.
    _reauth_attempted = True
    if not refresh_auth():
        logger.error("tagger: re-auth failed — article tagged empty")
        return []

    envelope = _invoke_once(cmd, prompt, timeout)
    if _is_auth_error(envelope):
        logger.error("tagger: credential error persists after re-auth — article tagged empty")
        return []

    return _tickers_from_envelope(envelope)


DEFAULT_LLM_FALLBACK_CATEGORIES = {"business", "banking", "trading", "market"}


def tag_article(
    article,
    llm_fallback_categories: set[str] | None = None,
) -> None:
    """Populate article.tickers in place.

    1. Run rules tagger over title + content.
    2. If empty AND article belongs to a market-adjacent category, try LLM.
    """
    text = (article.title or "") + " " + (article.content or "")
    rules_hits = extract_tickers_rules(text, load_ticker_dict())
    if rules_hits:
        article.tickers = rules_hits
        return

    fallback = llm_fallback_categories or DEFAULT_LLM_FALLBACK_CATEGORIES
    if any(c in fallback for c in (article.categories or [])):
        article.tickers = extract_tickers_llm(text)
    else:
        article.tickers = []
