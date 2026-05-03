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


def build_ticker_dict(csv_path: Path) -> dict[str, str]:
    """Return lowercase-name -> uppercase-ticker map.

    For each row, register: ticker (lower), full name (lower), name with corporate suffix stripped.
    """
    out: dict[str, str] = {}
    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tkr = (row.get("TKR") or "").strip().upper()
            name = (row.get("NAME") or "").strip()
            if not tkr or not name:
                continue
            out[tkr.lower()] = tkr
            out[name.lower()] = tkr
            stripped = SUFFIXES.sub("", name).strip().rstrip(".,").strip().lower()
            if stripped and stripped != name.lower():
                out[stripped] = tkr
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
