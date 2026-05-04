"""Generate config/tickers.yaml from etoro.csv (TKR + NAME columns).

Usage: python scripts/build_tickers_yaml.py
Reads:  ~/SourceCode/etorotrade/yahoofinance/output/etoro.csv
Writes: config/tickers.yaml
"""
from __future__ import annotations
import csv
import re
import sys
from pathlib import Path
import yaml

SUFFIXES = re.compile(
    r"[\s.]+(inc\.?|corp\.?|corporation|co\.?|ltd\.?|llc|plc|sa|nv|ag|holdings?|group)$",
    re.IGNORECASE,
)

STOPLIST = {
    # Common English words also used as company names (false positive risk)
    "a", "ai", "an", "and", "as", "at", "be", "by", "do", "for", "go", "if",
    "in", "is", "it", "no", "of", "on", "or", "so", "to", "up", "us", "we",
    "the", "this", "that", "with", "from", "into", "have", "has", "had",
    "are", "was", "were", "been", "being", "all", "one", "two", "three",
    "any", "many", "more", "most", "some", "few", "new", "old", "now",
    # Common business/finance words
    "news", "target", "api", "fast", "next", "open", "real", "tech",
    "well", "group", "holdings", "growth", "value", "market", "stock",
    "price", "share", "fund", "cash", "gold", "silver", "copper", "oil",
    "gas", "water", "food", "home", "life", "time", "work", "save",
    "code", "light", "heavy", "smart", "simple", "easy", "hard", "slow",
    "key", "lock", "play", "buy", "sell", "trade", "deal", "team",
    "form", "core", "peak", "edge", "flex", "pure", "free", "live",
    "best", "good", "great", "high", "low", "max", "min", "top",
    # Articles/prepositions in other languages
    "el", "la", "le", "il", "der", "die", "das",
}


def build_ticker_dict(csv_path: Path) -> dict[str, str]:
    """Return lowercase-name -> uppercase-ticker map.

    For each row, register: ticker (lower), full name (lower), name with corporate suffix stripped.
    """
    out: dict[str, str] = {}

    def _register(key: str, ticker: str) -> None:
        """Register key -> ticker mapping if key not in stoplist."""
        if key and key not in STOPLIST:
            out[key] = ticker

    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tkr = (row.get("TKR") or "").strip().upper()
            name = (row.get("NAME") or "").strip()
            if not tkr or not name:
                continue
            _register(tkr.lower(), tkr)
            _register(name.lower(), tkr)
            stripped = SUFFIXES.sub("", name).strip().rstrip(".,").strip().lower()
            if stripped and stripped != name.lower():
                _register(stripped, tkr)
    return out


def main() -> int:
    src = Path.home() / "SourceCode/etorotrade/yahoofinance/output/etoro.csv"
    if not src.exists():
        print(f"ERROR: {src} not found", file=sys.stderr)
        return 1
    repo_root = Path(__file__).parent.parent
    out_path = repo_root / "config" / "tickers.yaml"
    mapping = build_ticker_dict(src)
    payload = {"tickers": dict(sorted(mapping.items()))}
    with out_path.open("w") as f:
        yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)
    print(f"Wrote {len(mapping)} entries to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
