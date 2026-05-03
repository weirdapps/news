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


def extract_tickers_rules(text: str, ticker_dict: dict[str, str]) -> list[str]:
    """Return sorted unique uppercase tickers found in text via rules.

    Cashtags ($AAPL) — always captured.
    Long names (≥4 chars): case-insensitive word-boundary match.
    Short names (≤3 chars): require ALL-CAPS match to avoid common-word false
    positives (e.g., "on" as preposition vs "ON" as ticker for ON Semiconductor).
    """
    found: set[str] = set()

    # Cashtag matches — always captured
    for m in CASHTAG_RE.finditer(text):
        found.add(m.group(1).upper())

    # Name matches — sort longest-first so longer names win
    text_lower = text.lower()
    for name in sorted(ticker_dict.keys(), key=len, reverse=True):
        if len(name) <= 3:
            # Short keys: require ALL-CAPS in ORIGINAL text (case-sensitive)
            pattern = r"\b" + re.escape(name.upper()) + r"\b"
            if re.search(pattern, text):
                found.add(ticker_dict[name])
        else:
            # Long keys: case-insensitive
            pattern = r"\b" + re.escape(name) + r"\b"
            if re.search(pattern, text_lower):
                found.add(ticker_dict[name])

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
