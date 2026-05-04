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
import re
import subprocess
from functools import lru_cache
from pathlib import Path
import yaml

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


def extract_tickers_rules(text: str, ticker_dict: dict[str, str]) -> list[str]:
    """Return sorted unique uppercase tickers found in text via rules.

    Optimized: uses TWO pre-compiled alternation regexes (short + long keys)
    for O(text) match time instead of O(text × keys).

    Cashtags ($AAPL) — always captured.
    Long names (≥4 chars): case-insensitive match, requires capital first letter in original.
    Short names (≤3 chars): require ALL-CAPS match (proper-noun heuristic for ticker mentions).
    """
    found: set[str] = set()

    # Cashtag matches — always captured
    for m in CASHTAG_RE.finditer(text):
        found.add(m.group(1).upper())

    if not ticker_dict:
        return sorted(found)

    short_pat, long_pat = _compile_dict_patterns(id(ticker_dict), tuple(ticker_dict.keys()))

    # Short keys: case-sensitive search on original text for ALL-CAPS forms
    if short_pat:
        for m in short_pat.finditer(text):
            matched = m.group(1)
            # Map back to dict key (lowercase) → ticker
            ticker = ticker_dict.get(matched.lower())
            if ticker:
                found.add(ticker)

    # Long keys: case-insensitive search, capitalize-check after
    if long_pat:
        for m in long_pat.finditer(text):
            if text[m.start()].isupper():
                matched = m.group(1)
                ticker = ticker_dict.get(matched.lower())
                if ticker:
                    found.add(ticker)

    return sorted(found)


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

    Routes via Vertex AI (NBG-billed) — never the anthropic SDK with personal API key.
    Returns [] on any error (CLI missing, non-zero exit, malformed JSON, timeout).
    """
    prompt = _TAGGER_PROMPT + text[:max_chars]
    cmd = ["claude", "--model", model, "--print"]
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
        # Be lenient: strip markdown fences if the model wraps the JSON
        if raw.startswith("```"):
            raw = raw.strip("`").lstrip("json").strip()
        data = json.loads(raw)
        tickers = data.get("tickers", [])
        if not isinstance(tickers, list):
            return []
        return sorted({t.upper() for t in tickers if isinstance(t, str)})
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
        article.tickers = extract_tickers_llm(text)
    else:
        article.tickers = []
