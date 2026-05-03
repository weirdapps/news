"""Ticker tagger: rules first, optional Haiku fallback.

The rules layer matches:
  - Cashtags ($AAPL, $MSFT) — high confidence
  - Company names from a curated dict (apple -> AAPL) — word-boundary match

The Haiku fallback (separate function) is invoked only for articles in
market-adjacent categories where the rules layer found nothing — to keep
cost low while improving recall on names not in the dictionary.
"""
from __future__ import annotations
import re
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


def extract_tickers_rules(text: str, ticker_dict: dict[str, str]) -> list[str]:
    """Return sorted unique uppercase tickers found in text via rules.

    Word-boundary regex per name to avoid substring matches (e.g. 'snapple' must
    not match 'apple'). For the dict, longer keys are matched first to prefer
    'apple inc.' over 'apple' when both could match.
    """
    found: set[str] = set()

    # Cashtag matches
    for m in CASHTAG_RE.finditer(text):
        found.add(m.group(1).upper())

    # Name matches (case-insensitive, word boundary)
    text_lower = text.lower()
    # Sort keys longest-first so longer names win and we don't double-count
    for name in sorted(ticker_dict.keys(), key=len, reverse=True):
        # Build word-boundary regex; escape regex specials in name
        pattern = r"\b" + re.escape(name) + r"\b"
        if re.search(pattern, text_lower):
            found.add(ticker_dict[name])

    return sorted(found)
