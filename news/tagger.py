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


class TaggerAuthError(Exception):
    """Raised when the LLM CLI reports a credential failure.

    Distinct from a parse error or empty result: an auth failure cannot be
    retried without re-authentication, and returning [] would make it
    indistinguishable from an article that genuinely mentions no tickers.
    """


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
    Returns [] on any error (CLI missing, non-zero exit, malformed JSON, timeout).
    Raises TaggerAuthError on a credential failure so the caller can surface it
    rather than silently returning [] which is indistinguishable from a genuine
    empty result.
    """
    prompt = _TAGGER_PROMPT + text[:max_chars]
    cmd = ["claude", "--model", model, "--print", "--output-format", "json"]
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            return []
        raw = (proc.stdout or "").strip()
        try:
            envelope = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return []
        if _is_auth_error(envelope):
            raise TaggerAuthError(str(envelope.get("result", "")))
        if envelope.get("is_error"):
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
    except TaggerAuthError:
        raise
    except Exception:
        return []


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
        try:
            article.tickers = extract_tickers_llm(text)
        except TaggerAuthError:
            logger.error("LLM ticker extraction failed: credential error")
            raise
    else:
        article.tickers = []
