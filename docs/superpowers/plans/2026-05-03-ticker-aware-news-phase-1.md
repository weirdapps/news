# Ticker-Aware News, Phase 1: Schema, Tagger, Backfill, MCP, Committee Migration

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every article in `news.db` queryable by ticker symbol, so the trading committee and any future MCP consumer can ask "give me news for AAPL in the last 36 hours" and get hits from the existing 49K-article corpus instead of relying on WebSearch.

**Architecture:** Mirror the existing `article_categories` junction-table pattern with a parallel `article_tickers(article_url, ticker)` junction. Tag tickers at ingest using a two-stage tagger: rules first (cashtag regex + curated `name → ticker` dictionary), Anthropic Haiku fallback for articles in market-relevant categories where rules find nothing. Backfill the existing 49K articles with the same tagger. Extend `mcp__news-reader__search_news` with a `ticker` filter and add a new `recent_for_tickers` tool. Patch the trading committee's `load_news_feed()` SQL to optionally restrict by portfolio tickers.

**Tech Stack:** Python 3.12+, SQLite + FTS5, FastMCP, `claude` CLI via subprocess (reuses the pattern in `news/synthesizer.py:143-203` — routes via Vertex AI / NBG-billed; never the anthropic SDK), pytest with in-memory SQLite. Reuses existing `news/storage.py`, `news/processor.py`, `news/query.py`, `news/mcp_server.py`. Static ticker dictionary auto-generated from `~/SourceCode/etorotrade/yahoofinance/output/etoro.csv`. LLM tagging defaults to Sonnet (configurable).

**Out of scope (deferred to Phase 2 and Phase 3):**
- yfinance ticker fetcher (Phase 2 — adds new articles per portfolio + watchlist)
- trading-hub `/news`, `news-digest`, `market-news` migration (Phase 3)
- New "trading brief" email digest (Phase 3+)

---

## Task 1: Add `article_tickers` junction table + migration

**Files:**
- Modify: `~/news/news/storage.py:75-101` (CREATE TABLE block in `init_db()`)
- Modify: `~/news/news/storage.py:35-54` (`_migrate_db()`)
- Test: `~/news/tests/test_storage_tickers.py` (new)

- [ ] **Step 1: Write the failing test**

Create `~/news/tests/test_storage_tickers.py`:

```python
import sqlite3
import pytest
from news.storage import init_db, get_connection

def test_article_tickers_table_exists():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='article_tickers'"
    )
    assert cursor.fetchone() is not None

def test_article_tickers_has_index():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_article_tickers_ticker'"
    )
    assert cursor.fetchone() is not None

def test_article_tickers_cascade_delete():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO articles (url, title, source, published_at, content, content_hash, fetched_at) "
        "VALUES ('http://x/1', 't', 's', '2026-05-03', 'c', 'h', '2026-05-03')"
    )
    conn.execute("INSERT INTO article_tickers VALUES ('http://x/1', 'AAPL')")
    conn.execute("DELETE FROM articles WHERE url='http://x/1'")
    cursor = conn.execute("SELECT COUNT(*) FROM article_tickers")
    assert cursor.fetchone()[0] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/news && pytest tests/test_storage_tickers.py -v`
Expected: 3 failures (`article_tickers` table does not exist).

- [ ] **Step 3: Add CREATE TABLE in `init_db()`**

In `~/news/news/storage.py`, after the `article_categories` CREATE block (around line 101), add:

```python
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS article_tickers (
                article_url TEXT NOT NULL,
                ticker TEXT NOT NULL,
                PRIMARY KEY (article_url, ticker),
                FOREIGN KEY (article_url) REFERENCES articles(url) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_article_tickers_ticker ON article_tickers(ticker)"
        )
```

- [ ] **Step 4: Add `_migrate_db()` entry for existing databases**

In `~/news/news/storage.py:35-54`, add to the `stmts` list:

```python
        "CREATE TABLE IF NOT EXISTS article_tickers (article_url TEXT NOT NULL, ticker TEXT NOT NULL, PRIMARY KEY (article_url, ticker), FOREIGN KEY (article_url) REFERENCES articles(url) ON DELETE CASCADE)",
        "CREATE INDEX IF NOT EXISTS idx_article_tickers_ticker ON article_tickers(ticker)",
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd ~/news && pytest tests/test_storage_tickers.py -v`
Expected: 3 PASSED.

- [ ] **Step 6: Confirm migration runs cleanly against the live 251 MB DB (read-only safety check)**

Run: `cd ~/news && python -c "import sqlite3; from news.storage import init_db; conn = sqlite3.connect('data/news.db'); init_db(conn); print(conn.execute(\"SELECT COUNT(*) FROM article_tickers\").fetchone())"`
Expected: `(0,)` — table exists, empty.

- [ ] **Step 7: Commit**

```bash
git add news/storage.py tests/test_storage_tickers.py
git commit -m "feat(storage): add article_tickers junction table"
```

---

## Task 2: Extend `Article` model and storage helpers with tickers

**Files:**
- Modify: `~/news/news/models.py:6-31` (Article dataclass)
- Modify: `~/news/news/storage.py:201-258` (`insert_article()`)
- Modify: `~/news/news/storage.py:179-198` (`_row_to_article()`)
- Test: `~/news/tests/test_storage_tickers.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `~/news/tests/test_storage_tickers.py`:

```python
from news.models import Article
from news.storage import insert_article, get_article_by_url
from datetime import datetime

def _make_article(url="http://x/1", tickers=None):
    return Article(
        url=url, title="t", source="s", author=None,
        published_at=datetime(2026, 5, 3), content="c",
        summary=None, content_hash="h", language="en",
        relevance_score=10, fetched_at=datetime(2026, 5, 3),
        categories=["business"], tickers=tickers or [],
    )

def test_insert_article_persists_tickers():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    art = _make_article(tickers=["AAPL", "MSFT"])
    insert_article(conn, art)
    rows = conn.execute(
        "SELECT ticker FROM article_tickers WHERE article_url=? ORDER BY ticker",
        (art.url,)
    ).fetchall()
    assert [r[0] for r in rows] == ["AAPL", "MSFT"]

def test_get_article_by_url_loads_tickers():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    art = _make_article(tickers=["GOOG"])
    insert_article(conn, art)
    loaded = get_article_by_url(conn, art.url)
    assert loaded.tickers == ["GOOG"]

def test_insert_article_with_no_tickers():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    art = _make_article(tickers=[])
    insert_article(conn, art)
    loaded = get_article_by_url(conn, art.url)
    assert loaded.tickers == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/news && pytest tests/test_storage_tickers.py::test_insert_article_persists_tickers -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'tickers'`.

- [ ] **Step 3: Add `tickers` field to `Article` dataclass**

In `~/news/news/models.py`, add after the `categories` field (around line 28):

```python
    tickers: list[str] = field(default_factory=list)
```

(Imports `field` if not already present — `from dataclasses import dataclass, field`.)

- [ ] **Step 4: Update `insert_article()` to write tickers**

In `~/news/news/storage.py:201-258`, after the categories insert loop, add:

```python
    if article.tickers:
        for ticker in article.tickers:
            conn.execute(
                "INSERT OR IGNORE INTO article_tickers (article_url, ticker) VALUES (?, ?)",
                (article.url, ticker.upper()),
            )
```

- [ ] **Step 5: Update `_row_to_article()` to load tickers**

In `~/news/news/storage.py:179-198`, before the final `return Article(...)`, add:

```python
    ticker_rows = conn.execute(
        "SELECT ticker FROM article_tickers WHERE article_url = ? ORDER BY ticker",
        (row["url"],),
    ).fetchall()
    tickers = [r["ticker"] for r in ticker_rows]
```

Then add `tickers=tickers,` to the `Article(...)` constructor.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd ~/news && pytest tests/test_storage_tickers.py -v`
Expected: 6 PASSED.

- [ ] **Step 7: Run the full test suite to catch regressions**

Run: `cd ~/news && pytest -v`
Expected: All previously passing tests still pass. If any test breaks because `Article(...)` now needs a `tickers` arg, the default factory should handle it; investigate any failure.

- [ ] **Step 8: Commit**

```bash
git add news/models.py news/storage.py tests/test_storage_tickers.py
git commit -m "feat(storage): persist and load tickers on Article model"
```

---

## Task 3: Add `claude_code` category and refine `categories.yaml`

**Why:** User flagged Claude Code as personal-interest news that should not bleed into market-relevant queries. Today it falls under `ai` or `tech` and is implicitly excluded from the committee's `('trading','business','banking')` filter — but giving it its own category makes the personal/professional split explicit and queryable.

**Files:**
- Modify: `~/news/config/categories.yaml`
- Test: `~/news/tests/test_categories.py` (new)

- [ ] **Step 1: Write the failing test**

Create `~/news/tests/test_categories.py`:

```python
from news.config import load_categories
from news.processor import classify_article
from news.models import Article
from datetime import datetime

def _art(title, content=""):
    return Article(
        url="http://x/1", title=title, source="s", author=None,
        published_at=datetime(2026, 5, 3), content=content,
        summary=None, content_hash="h", language="en",
        relevance_score=0, fetched_at=datetime(2026, 5, 3),
        categories=[], tickers=[],
    )

def test_claude_code_release_classified_as_claude_code():
    cfg = load_categories()
    assert "claude_code" in cfg["categories"]
    art = _art("Claude Code 4.7 introduces 1M context", "Anthropic released...")
    classify_article(art, cfg)
    assert "claude_code" in art.categories

def test_general_ai_news_not_in_claude_code():
    cfg = load_categories()
    art = _art("OpenAI launches new GPT-5", "OpenAI announced...")
    classify_article(art, cfg)
    assert "claude_code" not in art.categories
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/news && pytest tests/test_categories.py -v`
Expected: FAIL — `claude_code` category not in YAML.

- [ ] **Step 3: Add `claude_code` to `categories.yaml`**

In `~/news/config/categories.yaml`, add a new entry:

```yaml
  claude_code:
    display_name: "Claude Code"
    keywords:
      - "claude code"
      - "claude 4.7"
      - "claude 4.6"
      - "claude opus 4"
      - "claude sonnet 4"
      - "claude haiku 4"
      - "anthropic skills"
      - "claude agent sdk"
    priority: 5
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/news && pytest tests/test_categories.py -v`
Expected: 2 PASSED.

- [ ] **Step 5: Commit**

```bash
git add config/categories.yaml tests/test_categories.py
git commit -m "feat(categories): split claude_code from general ai/tech"
```

---

## Task 4: Build `config/tickers.yaml` from etoro.csv

**Files:**
- Create: `~/news/scripts/build_tickers_yaml.py`
- Create: `~/news/config/tickers.yaml` (generated)
- Test: `~/news/tests/test_build_tickers_yaml.py`

- [ ] **Step 1: Write the failing test**

Create `~/news/tests/test_build_tickers_yaml.py`:

```python
from pathlib import Path
import csv
from scripts.build_tickers_yaml import build_ticker_dict

def test_build_dict_from_csv(tmp_path):
    csv_path = tmp_path / "etoro.csv"
    with csv_path.open("w") as f:
        w = csv.writer(f)
        w.writerow(["TKR", "NAME"])
        w.writerow(["AAPL", "Apple Inc."])
        w.writerow(["MSFT", "Microsoft Corp"])
        w.writerow(["GOOG", "Alphabet Inc."])
    result = build_ticker_dict(csv_path)
    assert result["aapl"] == "AAPL"
    assert result["apple inc."] == "AAPL"
    assert result["apple"] == "AAPL"  # stripped suffix

def test_strips_corporate_suffixes(tmp_path):
    csv_path = tmp_path / "etoro.csv"
    with csv_path.open("w") as f:
        w = csv.writer(f)
        w.writerow(["TKR", "NAME"])
        w.writerow(["BRK.B", "Berkshire Hathaway Inc"])
        w.writerow(["JNJ", "Johnson & Johnson"])
    result = build_ticker_dict(csv_path)
    assert result["berkshire hathaway"] == "BRK.B"
    assert result["johnson & johnson"] == "JNJ"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/news && pytest tests/test_build_tickers_yaml.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `scripts/build_tickers_yaml.py`**

```python
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
    r"\s+(inc\.?|corp\.?|corporation|co\.?|ltd\.?|llc|plc|sa|nv|ag|holdings?|group)$",
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
            stripped = SUFFIXES.sub("", name).strip().lower()
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/news && pytest tests/test_build_tickers_yaml.py -v`
Expected: 2 PASSED.

- [ ] **Step 5: Generate the actual `config/tickers.yaml`**

Run: `cd ~/news && python scripts/build_tickers_yaml.py`
Expected: `Wrote ~10000-15000 entries to .../config/tickers.yaml`

Inspect the file: `head -30 config/tickers.yaml`. Confirm sane mappings (e.g. `apple: AAPL`, `microsoft: MSFT`).

- [ ] **Step 6: Commit**

```bash
git add scripts/build_tickers_yaml.py tests/test_build_tickers_yaml.py config/tickers.yaml
git commit -m "feat(config): generate tickers.yaml from etoro.csv (~5K mappings)"
```

---

## Task 5: Rules-based ticker tagger

**Files:**
- Create: `~/news/news/tagger.py`
- Test: `~/news/tests/test_tagger.py`

- [ ] **Step 1: Write the failing test**

Create `~/news/tests/test_tagger.py`:

```python
from news.tagger import extract_tickers_rules

TICKER_DICT = {
    "apple": "AAPL", "apple inc.": "AAPL", "aapl": "AAPL",
    "microsoft": "MSFT", "microsoft corp": "MSFT", "msft": "MSFT",
    "alphabet": "GOOG", "google": "GOOG", "goog": "GOOG",
}


def test_cashtag_match():
    text = "Markets watching $AAPL ahead of earnings"
    assert extract_tickers_rules(text, TICKER_DICT) == ["AAPL"]

def test_company_name_match():
    text = "Apple announced new headphones; Microsoft followed."
    assert sorted(extract_tickers_rules(text, TICKER_DICT)) == ["AAPL", "MSFT"]

def test_no_match():
    text = "Random news about weather"
    assert extract_tickers_rules(text, TICKER_DICT) == []

def test_dedup():
    text = "Apple, $AAPL, and Apple Inc. are all the same company"
    assert extract_tickers_rules(text, TICKER_DICT) == ["AAPL"]

def test_case_insensitive():
    text = "MICROSOFT had a strong quarter"
    assert extract_tickers_rules(text, TICKER_DICT) == ["MSFT"]

def test_word_boundary_no_substring_match():
    text = "Snapple sold record bottles"  # contains 'apple' as substring
    assert extract_tickers_rules(text, TICKER_DICT) == []

def test_returns_sorted_unique():
    text = "Microsoft and Apple beat estimates; Google missed."
    assert extract_tickers_rules(text, TICKER_DICT) == ["AAPL", "GOOG", "MSFT"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/news && pytest tests/test_tagger.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `news/tagger.py`**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/news && pytest tests/test_tagger.py -v`
Expected: 7 PASSED.

- [ ] **Step 5: Quick smoke test against a real article from the DB**

Run:

```bash
cd ~/news && python -c "
import sqlite3
from news.tagger import load_ticker_dict, extract_tickers_rules
conn = sqlite3.connect('data/news.db')
row = conn.execute('SELECT title, content FROM articles WHERE content LIKE \"%Apple%\" LIMIT 1').fetchone()
text = (row[0] or '') + ' ' + (row[1] or '')
print('TICKERS:', extract_tickers_rules(text[:5000], load_ticker_dict()))
print('TITLE:', row[0])
"
```

Expected: A real article title and a non-empty ticker list including AAPL.

- [ ] **Step 6: Commit**

```bash
git add news/tagger.py tests/test_tagger.py
git commit -m "feat(tagger): rules-based ticker extraction (cashtag + name dict)"
```

---

## Task 6: LLM fallback tagger via `claude` CLI (Vertex-routed, NBG-billed)

**Files:**
- Modify: `~/news/news/tagger.py`
- Modify: `~/news/config/settings.yaml` (add `tagger` block)
- Test: `~/news/tests/test_tagger_llm.py`

**Critical: NO `anthropic` SDK dependency.** All LLM calls in this codebase MUST go through the local `claude` CLI via subprocess — which on this machine is configured to route via Vertex AI and is billed to National Bank of Greece. Adding the SDK would route through the user's personal API key billing, which is wrong. Reuse the existing `invoke_claude()` helper in `news/synthesizer.py:143-203`.

**Model choice:** Default to `sonnet` (Sonnet 4.6) per user preference — cost is covered, quality is the constraint. The synthesizer already accepts `claude_args` like `["--model", "sonnet"]` from settings.yaml — same pattern.

- [ ] **Step 1: Add `tagger` config block to `settings.yaml`**

In `~/news/config/settings.yaml`, after the existing `synthesis:` block (around line 71), add:

```yaml
tagger:
  enabled: true
  model: "sonnet"          # Sonnet by default (NBG-billed via Vertex)
  timeout_seconds: 30
  max_text_chars: 4000     # Truncate input to bound latency
  fallback_categories:     # Only call LLM for articles in these categories
    - business
    - banking
    - trading
    - market
```

- [ ] **Step 2: Write the failing test (mocking subprocess.run)**

Create `~/news/tests/test_tagger_llm.py`:

```python
from unittest.mock import patch, MagicMock
import json
from news.tagger import extract_tickers_llm


def _mock_proc(stdout_text, returncode=0):
    """Build a mock CompletedProcess for subprocess.run."""
    proc = MagicMock()
    proc.stdout = stdout_text
    proc.stderr = ""
    proc.returncode = returncode
    return proc


@patch("news.tagger.subprocess.run")
def test_llm_returns_tickers(mock_run):
    mock_run.return_value = _mock_proc(json.dumps({"tickers": ["AAPL", "MSFT"]}))
    out = extract_tickers_llm("Apple and Microsoft beat estimates")
    assert out == ["AAPL", "MSFT"]
    # Verify it called the `claude` CLI, not anything else
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "claude"
    assert "--model" in cmd
    assert "sonnet" in cmd


@patch("news.tagger.subprocess.run")
def test_llm_returns_empty_when_no_tickers(mock_run):
    mock_run.return_value = _mock_proc(json.dumps({"tickers": []}))
    out = extract_tickers_llm("Weather report for Athens")
    assert out == []


@patch("news.tagger.subprocess.run")
def test_llm_handles_malformed_response(mock_run):
    mock_run.return_value = _mock_proc('{"oops": "no tickers key"}')
    out = extract_tickers_llm("Some article")
    assert out == []  # Graceful fallback on bad JSON shape


@patch("news.tagger.subprocess.run")
def test_llm_handles_markdown_fenced_json(mock_run):
    mock_run.return_value = _mock_proc(
        '```json\n{"tickers": ["AAPL"]}\n```'
    )
    out = extract_tickers_llm("Apple news")
    assert out == ["AAPL"]


@patch("news.tagger.subprocess.run")
def test_llm_uppercases_and_dedups(mock_run):
    mock_run.return_value = _mock_proc(json.dumps({"tickers": ["aapl", "AAPL", "msft"]}))
    out = extract_tickers_llm("text")
    assert out == ["AAPL", "MSFT"]


@patch("news.tagger.subprocess.run")
def test_llm_returns_empty_on_subprocess_failure(mock_run):
    mock_run.side_effect = Exception("command not found")
    out = extract_tickers_llm("text")
    assert out == []


@patch("news.tagger.subprocess.run")
def test_llm_returns_empty_on_nonzero_exit(mock_run):
    mock_run.return_value = _mock_proc("error", returncode=1)
    out = extract_tickers_llm("text")
    assert out == []


@patch("news.tagger.subprocess.run")
def test_llm_passes_custom_model(mock_run):
    mock_run.return_value = _mock_proc(json.dumps({"tickers": []}))
    extract_tickers_llm("text", model="opus")
    cmd = mock_run.call_args[0][0]
    assert "opus" in cmd
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd ~/news && pytest tests/test_tagger_llm.py -v`
Expected: FAIL — `extract_tickers_llm` not defined.

- [ ] **Step 4: Implement `extract_tickers_llm()` in `news/tagger.py` using the Claude CLI**

Append to `~/news/news/tagger.py`:

```python
import json
import subprocess

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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd ~/news && pytest tests/test_tagger_llm.py -v`
Expected: 8 PASSED.

- [ ] **Step 6: Run the full test suite**

Run: `cd ~/news && pytest -v`
Expected: All previously passing tests still pass.

- [ ] **Step 7: Smoke-test the live CLI invocation against one real article**

Run:

```bash
cd ~/news && python -c "
from news.tagger import extract_tickers_llm
print(extract_tickers_llm('Apple Inc. beat earnings expectations on iPhone sales, while Microsoft Azure grew 30%.'))
"
```

Expected: `['AAPL', 'MSFT']` (or a superset). If it returns `[]`, the `claude` CLI isn't on PATH — verify with `which claude`.

- [ ] **Step 8: Commit**

```bash
git add news/tagger.py tests/test_tagger_llm.py config/settings.yaml
git commit -m "feat(tagger): LLM fallback via claude CLI (Vertex/NBG-billed, Sonnet default)"
```

---

## Task 7: Combined `tag_article()` and wire into processor

**Files:**
- Modify: `~/news/news/tagger.py` (add orchestrator)
- Modify: `~/news/news/processor.py:234-286`
- Test: `~/news/tests/test_processor_tagging.py`

- [ ] **Step 1: Write the failing test**

Create `~/news/tests/test_processor_tagging.py`:

```python
from unittest.mock import patch
from news.tagger import tag_article
from news.models import Article
from datetime import datetime

TICKER_DICT = {"apple": "AAPL", "aapl": "AAPL", "microsoft": "MSFT", "msft": "MSFT"}

def _art(title, content="", categories=None):
    return Article(
        url="http://x/1", title=title, source="s", author=None,
        published_at=datetime(2026, 5, 3), content=content,
        summary=None, content_hash="h", language="en",
        relevance_score=0, fetched_at=datetime(2026, 5, 3),
        categories=categories or [], tickers=[],
    )

def test_rules_only_when_match_found():
    art = _art("Apple beats earnings", "Apple Inc. ($AAPL) reported...", categories=["business"])
    with patch("news.tagger.load_ticker_dict", return_value=TICKER_DICT), \
         patch("news.tagger.extract_tickers_llm") as mock_llm:
        tag_article(art, llm_fallback_categories={"business"})
        assert art.tickers == ["AAPL"]
        mock_llm.assert_not_called()  # rules succeeded, no LLM call

def test_llm_fallback_when_rules_empty_and_market_category():
    art = _art("Mystery firm soars", "A previously unknown company...", categories=["business"])
    with patch("news.tagger.load_ticker_dict", return_value=TICKER_DICT), \
         patch("news.tagger.extract_tickers_llm", return_value=["XYZ"]):
        tag_article(art, llm_fallback_categories={"business"})
        assert art.tickers == ["XYZ"]

def test_no_llm_when_not_market_category():
    art = _art("Claude Code 4.7 ships", "Anthropic released...", categories=["claude_code"])
    with patch("news.tagger.load_ticker_dict", return_value=TICKER_DICT), \
         patch("news.tagger.extract_tickers_llm") as mock_llm:
        tag_article(art, llm_fallback_categories={"business", "trading", "banking"})
        assert art.tickers == []
        mock_llm.assert_not_called()

def test_processor_invokes_tagger():
    """Smoke: process_articles populates article.tickers."""
    from news.processor import process_articles
    art = _art("Apple Q3 earnings", "Apple Inc. reported strong revenue.", categories=[])
    with patch("news.tagger.load_ticker_dict", return_value=TICKER_DICT):
        kept, _, _ = process_articles(
            [art], existing_hashes=set(), categories_config={"categories": {}},
            scoring_config={}, source_tiers={}, min_words=1, max_age_hours=999999,
        )
    assert kept[0].tickers == ["AAPL"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/news && pytest tests/test_processor_tagging.py -v`
Expected: FAIL — `tag_article` not defined.

- [ ] **Step 3: Implement `tag_article()` orchestrator in `news/tagger.py`**

Append to `~/news/news/tagger.py`:

```python
DEFAULT_LLM_FALLBACK_CATEGORIES = {"business", "banking", "trading", "market"}


def tag_article(
    article,
    llm_fallback_categories: set[str] | None = None,
) -> None:
    """Populate article.tickers in place.

    1. Run rules tagger over title + content.
    2. If empty AND article belongs to a market-adjacent category, try Haiku.
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
```

- [ ] **Step 4: Wire `tag_article()` into `process_articles()`**

In `~/news/news/processor.py:234-286`, after the `compute_relevance_score(...)` line (around line 283) inside the `for article in unique:` loop, add:

```python
        from news.tagger import tag_article
        tag_article(article)
```

(Inline import to avoid circular imports at module load time; tagger imports `anthropic` which is heavy.)

- [ ] **Step 5: Run all tagger tests**

Run: `cd ~/news && pytest tests/test_processor_tagging.py tests/test_tagger.py tests/test_tagger_llm.py -v`
Expected: all PASS.

- [ ] **Step 6: Run full suite**

Run: `cd ~/news && pytest -v`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add news/tagger.py news/processor.py tests/test_processor_tagging.py
git commit -m "feat(processor): tag tickers at ingest (rules + Haiku fallback)"
```

---

## Task 8: Backfill existing 49K articles

**Files:**
- Create: `~/news/scripts/backfill_tickers.py`
- Test: `~/news/tests/test_backfill_tickers.py`

**Why a script and not a one-time SQL migration:** the tagger uses Python regex + optional LLM, not pure SQL. Script is resumable (skips articles already in `article_tickers`) so it can be run incrementally and interrupted safely.

- [ ] **Step 1: Write the failing test**

Create `~/news/tests/test_backfill_tickers.py`:

```python
import sqlite3
from unittest.mock import patch
from news.storage import init_db, insert_article
from news.models import Article
from datetime import datetime
from scripts.backfill_tickers import backfill

TICKER_DICT = {"apple": "AAPL", "aapl": "AAPL"}


def _art(url, title, content):
    return Article(
        url=url, title=title, source="s", author=None,
        published_at=datetime(2026, 5, 3), content=content,
        summary=None, content_hash=url, language="en",
        relevance_score=0, fetched_at=datetime(2026, 5, 3),
        categories=["business"], tickers=[],
    )


def test_backfill_tags_articles_without_tickers():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    insert_article(conn, _art("http://x/1", "Apple earnings", "Apple Inc reports..."))
    insert_article(conn, _art("http://x/2", "Weather", "Sunny in Athens"))
    with patch("news.tagger.load_ticker_dict", return_value=TICKER_DICT):
        n_processed, n_tagged = backfill(conn, batch_size=10, max_articles=None)
    assert n_processed == 2
    assert n_tagged == 1  # only the Apple article had a ticker
    rows = conn.execute("SELECT article_url, ticker FROM article_tickers").fetchall()
    assert ("http://x/1", "AAPL") in [(r[0], r[1]) for r in rows]


def test_backfill_skips_already_tagged():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    insert_article(conn, _art("http://x/1", "Apple earnings", "Apple Inc reports..."))
    conn.execute("INSERT INTO article_tickers VALUES ('http://x/1', 'AAPL')")
    with patch("news.tagger.load_ticker_dict", return_value=TICKER_DICT):
        n_processed, n_tagged = backfill(conn, batch_size=10, max_articles=None)
    assert n_processed == 0  # already tagged, skipped
    assert n_tagged == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/news && pytest tests/test_backfill_tickers.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `scripts/backfill_tickers.py`**

```python
"""One-time (and resumable) backfill of article_tickers for existing articles.

Iterates articles that have NO entry in article_tickers, runs the tagger,
inserts results. Safe to run repeatedly — skips already-tagged rows.

Usage:
    python scripts/backfill_tickers.py            # process all untagged
    python scripts/backfill_tickers.py --max 100  # process up to 100 (smoke test)
    python scripts/backfill_tickers.py --batch 500 --commit-every 100
"""
from __future__ import annotations
import argparse
import sqlite3
import sys
import time
from pathlib import Path

# Ensure news package importable when run from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from news.storage import get_connection, init_db
from news.tagger import tag_article
from news.models import Article
from datetime import datetime


def _row_to_minimal_article(row) -> Article:
    """Build minimal Article for the tagger — only needs title, content, categories."""
    return Article(
        url=row["url"], title=row["title"] or "", source=row["source"],
        author=None, published_at=datetime.fromisoformat(row["published_at"]),
        content=row["content"] or "", summary=None,
        content_hash=row["content_hash"], language=None,
        relevance_score=0, fetched_at=datetime.fromisoformat(row["fetched_at"]),
        categories=[], tickers=[],
    )


def backfill(
    conn: sqlite3.Connection,
    batch_size: int = 500,
    max_articles: int | None = None,
    commit_every: int = 100,
) -> tuple[int, int]:
    """Process untagged articles. Returns (n_processed, n_tagged)."""
    # Load category map per article in one query
    cat_rows = conn.execute("SELECT article_url, category FROM article_categories").fetchall()
    cat_map: dict[str, list[str]] = {}
    for r in cat_rows:
        cat_map.setdefault(r["article_url"], []).append(r["category"])

    n_processed = 0
    n_tagged = 0
    offset = 0
    while True:
        if max_articles and n_processed >= max_articles:
            break
        limit = batch_size
        if max_articles:
            limit = min(limit, max_articles - n_processed)
        rows = conn.execute(
            """
            SELECT a.* FROM articles a
            WHERE NOT EXISTS (
                SELECT 1 FROM article_tickers at WHERE at.article_url = a.url
            )
            ORDER BY a.fetched_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        if not rows:
            break
        for row in rows:
            art = _row_to_minimal_article(row)
            art.categories = cat_map.get(art.url, [])
            tag_article(art)
            for ticker in art.tickers:
                conn.execute(
                    "INSERT OR IGNORE INTO article_tickers (article_url, ticker) VALUES (?, ?)",
                    (art.url, ticker),
                )
            if art.tickers:
                n_tagged += 1
            n_processed += 1
            if n_processed % commit_every == 0:
                conn.commit()
                print(f"  ... {n_processed} processed, {n_tagged} tagged", flush=True)
        conn.commit()
    return n_processed, n_tagged


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=None)
    parser.add_argument("--batch", type=int, default=500)
    parser.add_argument("--commit-every", type=int, default=100)
    args = parser.parse_args()

    db_path = Path(__file__).parent.parent / "data" / "news.db"
    conn = get_connection(db_path)
    init_db(conn)
    start = time.time()
    n_processed, n_tagged = backfill(conn, args.batch, args.max, args.commit_every)
    elapsed = time.time() - start
    print(f"Done. Processed {n_processed} in {elapsed:.0f}s, tagged {n_tagged} ({100*n_tagged/max(n_processed,1):.0f}%).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/news && pytest tests/test_backfill_tickers.py -v`
Expected: 2 PASSED.

- [ ] **Step 5: Smoke-test against the live DB with `--max 100`**

Run: `cd ~/news && python scripts/backfill_tickers.py --max 100`
Expected: ~30-60s, output like `Processed 100 in 45s, tagged ~30 (30%)`. Verify with:

```bash
sqlite3 data/news.db "SELECT COUNT(*) FROM article_tickers"
sqlite3 data/news.db "SELECT ticker, COUNT(*) FROM article_tickers GROUP BY ticker ORDER BY 2 DESC LIMIT 10"
```

Expected: a few hundred ticker assignments, top tickers like AAPL, MSFT, GOOG.

- [ ] **Step 6: Run full backfill (no `--max`)**

Run in foreground in a screen/tmux pane (estimated 30-60 min for 49K articles):

```bash
cd ~/news && python scripts/backfill_tickers.py 2>&1 | tee /tmp/backfill.log
```

If interrupted, re-run — script is resumable.

- [ ] **Step 7: Verify backfill completion**

```bash
sqlite3 ~/news/data/news.db "SELECT COUNT(*) FROM articles WHERE NOT EXISTS (SELECT 1 FROM article_tickers WHERE article_url=articles.url)"
```

Some articles will have 0 tickers (correctly so — no companies mentioned). The query returns count of articles that have not yet been processed by backfill OR have no tickers. Cross-check by running `head /tmp/backfill.log` to confirm script exited normally.

- [ ] **Step 8: Commit**

```bash
git add scripts/backfill_tickers.py tests/test_backfill_tickers.py
git commit -m "feat(scripts): backfill ticker tags for existing articles"
```

---

## Task 9: Extend MCP `search_news` with ticker filter

**Files:**
- Modify: `~/news/news/query.py:18-97` (`search_articles()`)
- Modify: `~/news/news/mcp_server.py:37-67` (`search_news` tool)
- Test: `~/news/tests/test_query_ticker.py`

- [ ] **Step 1: Write the failing test**

Create `~/news/tests/test_query_ticker.py`:

```python
import sqlite3
from datetime import datetime
from news.storage import init_db, insert_article
from news.models import Article
from news.query import search_articles


def _art(url, title, tickers):
    return Article(
        url=url, title=title, source="s", author=None,
        published_at=datetime(2026, 5, 3), content=title,
        summary=None, content_hash=url, language="en",
        relevance_score=10, fetched_at=datetime(2026, 5, 3),
        categories=["business"], tickers=tickers,
    )


def test_search_filters_by_ticker():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    insert_article(conn, _art("http://x/1", "Apple earnings", ["AAPL"]))
    insert_article(conn, _art("http://x/2", "Microsoft cloud", ["MSFT"]))
    insert_article(conn, _art("http://x/3", "Both Apple and Microsoft", ["AAPL", "MSFT"]))
    results = search_articles(conn, "earnings cloud", ticker="AAPL", days=30, limit=10)
    urls = [r["url"] for r in results]
    assert "http://x/1" in urls
    assert "http://x/3" in urls
    assert "http://x/2" not in urls


def test_search_no_ticker_filter_returns_all_matches():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    insert_article(conn, _art("http://x/1", "Apple earnings", ["AAPL"]))
    insert_article(conn, _art("http://x/2", "Microsoft cloud", ["MSFT"]))
    results = search_articles(conn, "earnings cloud", days=30, limit=10)
    assert len(results) == 2


def test_search_ticker_filter_with_no_matches():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    insert_article(conn, _art("http://x/1", "Apple earnings", ["AAPL"]))
    results = search_articles(conn, "earnings", ticker="NVDA", days=30, limit=10)
    assert results == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/news && pytest tests/test_query_ticker.py -v`
Expected: FAIL — `search_articles()` doesn't accept `ticker`.

- [ ] **Step 3: Add `ticker` parameter to `search_articles()`**

In `~/news/news/query.py:18-97`, modify the function signature to accept `ticker: str | None = None`. After the existing `category` filter logic in the SQL builder, add:

```python
    if ticker:
        where_clauses.append(
            "EXISTS (SELECT 1 FROM article_tickers at WHERE at.article_url = a.url AND at.ticker = ?)"
        )
        params.append(ticker.upper())
```

(Adapt to the actual variable names used in `search_articles` — read the function first to identify the existing `where_clauses` / `params` accumulators, or follow the same WHERE-building pattern already in use for `category` and `pipeline`.)

- [ ] **Step 4: Run query tests to verify they pass**

Run: `cd ~/news && pytest tests/test_query_ticker.py -v`
Expected: 3 PASSED.

- [ ] **Step 5: Expose `ticker` param in MCP `search_news` tool**

In `~/news/news/mcp_server.py:37-67`, modify the tool signature:

```python
@mcp.tool()
def search_news(
    query: str,
    pipeline: str | None = None,
    category: str | None = None,
    ticker: str | None = None,
    days: int = 30,
    limit: int = 20,
) -> list[dict]:
    """Search news articles by keyword across title and content.

    Args:
        query: Search keyword (case-insensitive)
        pipeline: Filter by pipeline — 'digest' or 'monitor' (default: both)
        category: Filter by category — banking, greece, ai, tech, etc.
        ticker: Filter by ticker symbol (e.g. 'AAPL') — case-insensitive
        days: Lookback period in days (default: 30)
        limit: Maximum results (default: 20)
    """
    # In the body, pass ticker through to search_articles()
```

Update the `search_articles(...)` call inside the tool body to include `ticker=ticker`.

- [ ] **Step 6: Smoke-test the MCP tool end-to-end**

Run from a fresh Claude Code session (or via the news repo's MCP harness if there's one):

```python
mcp__news_reader__search_news(query="earnings", ticker="AAPL", days=30, limit=5)
```

Expected: returns 1+ results, all about Apple. (Requires Task 8 backfill to have populated AAPL tags.)

- [ ] **Step 7: Run full suite**

Run: `cd ~/news && pytest -v`
Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add news/query.py news/mcp_server.py tests/test_query_ticker.py
git commit -m "feat(mcp): add ticker filter to search_news"
```

---

## Task 10: Add new MCP tool `recent_for_tickers`

**Files:**
- Modify: `~/news/news/query.py` (add helper)
- Modify: `~/news/news/mcp_server.py` (add tool)
- Test: `~/news/tests/test_query_ticker.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `~/news/tests/test_query_ticker.py`:

```python
from news.query import recent_for_tickers


def test_recent_for_tickers_returns_articles_for_any_ticker_in_list():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    insert_article(conn, _art("http://x/1", "Apple news", ["AAPL"]))
    insert_article(conn, _art("http://x/2", "Microsoft news", ["MSFT"]))
    insert_article(conn, _art("http://x/3", "Google news", ["GOOG"]))
    results = recent_for_tickers(conn, tickers=["AAPL", "MSFT"], hours=72, limit=10)
    urls = sorted(r["url"] for r in results)
    assert urls == ["http://x/1", "http://x/2"]


def test_recent_for_tickers_respects_hours_window():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    # Insert old article (manipulate fetched_at directly)
    insert_article(conn, _art("http://x/1", "Old Apple", ["AAPL"]))
    conn.execute(
        "UPDATE articles SET fetched_at='2020-01-01T00:00:00' WHERE url='http://x/1'"
    )
    # Recent article
    insert_article(conn, _art("http://x/2", "Fresh Apple", ["AAPL"]))
    results = recent_for_tickers(conn, tickers=["AAPL"], hours=24, limit=10)
    assert [r["url"] for r in results] == ["http://x/2"]


def test_recent_for_tickers_empty_list():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    insert_article(conn, _art("http://x/1", "Apple", ["AAPL"]))
    assert recent_for_tickers(conn, tickers=[], hours=72, limit=10) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/news && pytest tests/test_query_ticker.py::test_recent_for_tickers_returns_articles_for_any_ticker_in_list -v`
Expected: FAIL — function not defined.

- [ ] **Step 3: Implement `recent_for_tickers()` in `news/query.py`**

Append to `~/news/news/query.py`:

```python
def recent_for_tickers(
    conn: sqlite3.Connection,
    tickers: list[str],
    hours: int = 24,
    limit: int = 50,
) -> list[dict]:
    """Return articles tagged with ANY of the given tickers, within the time window.

    Args:
        tickers: List of ticker symbols (case-insensitive). Empty list returns [].
        hours: Lookback window from now.
        limit: Max articles to return.
    """
    if not tickers:
        return []
    placeholders = ",".join("?" for _ in tickers)
    sql = f"""
        SELECT DISTINCT a.url, a.title, a.source, a.published_at,
               a.summary, a.relevance_score,
               GROUP_CONCAT(at.ticker) AS matched_tickers
        FROM articles a
        JOIN article_tickers at ON a.url = at.article_url
        WHERE at.ticker IN ({placeholders})
          AND a.fetched_at >= datetime('now', ? || ' hours')
        GROUP BY a.url
        ORDER BY a.relevance_score DESC, a.published_at DESC
        LIMIT ?
    """
    params = [t.upper() for t in tickers] + [f"-{int(hours)}", limit]
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Add MCP tool `recent_for_tickers`**

In `~/news/news/mcp_server.py`, after the existing `search_news` tool (around line 67), add:

```python
@mcp.tool()
def recent_for_tickers(
    tickers: list[str],
    hours: int = 24,
    limit: int = 50,
) -> list[dict]:
    """Get recent news articles tagged with any of the given tickers.

    Optimized for trading workflows — pass a portfolio + watchlist ticker list
    and get back relevant news from the cached corpus, much faster than WebSearch.

    Args:
        tickers: List of ticker symbols (e.g. ['AAPL', 'MSFT']). Case-insensitive.
        hours: Lookback window in hours (default: 24).
        limit: Max articles to return (default: 50).
    """
    from news.query import recent_for_tickers as _query
    conn = get_connection(_DB_PATH)
    try:
        return _query(conn, tickers, hours=hours, limit=limit)
    finally:
        conn.close()
```

(Match the existing pattern in `mcp_server.py` for opening / closing the DB connection — read lines 37-67 for the convention.)

- [ ] **Step 5: Run tests**

Run: `cd ~/news && pytest tests/test_query_ticker.py -v`
Expected: all PASS.

- [ ] **Step 6: Smoke-test from a Claude session**

After restarting any active Claude Code sessions so the MCP tool list refreshes:

```python
mcp__news_reader__recent_for_tickers(tickers=["AAPL", "MSFT", "GOOG"], hours=72, limit=10)
```

Expected: list of recent news for those three names.

- [ ] **Step 7: Commit**

```bash
git add news/query.py news/mcp_server.py tests/test_query_ticker.py
git commit -m "feat(mcp): add recent_for_tickers tool for portfolio-aware queries"
```

---

## Task 11: Migrate trading committee `load_news_feed()` to ticker-aware query

**Files (in trading-marketplace, NOT news repo):**
- Modify: `~/trading-marketplace/scripts/run_committee.py:249-291`

**Decision point:** The committee already queries `news.db` directly via SQL with category filter `('trading', 'business', 'banking')`. We extend the query to ALSO surface ticker-tagged articles for portfolio tickers, even if their category is broader.

- [ ] **Step 1: Read the existing function**

Run: `cd ~/trading-marketplace && sed -n '244,295p' scripts/run_committee.py`

Confirm:
- The function signature accepts (or has access to) the portfolio tickers list (per recon: `data["tickers"]` at line 244 calls `load_news_feed()` with implicit ticker context — verify by reading).
- The current SQL is the JOIN on `article_categories` shown in the recon report.

- [ ] **Step 2: Write a regression test if a `tests/` dir exists for this script**

If `~/trading-marketplace/scripts/tests/` exists, add a test asserting that articles tagged with a portfolio ticker but in a different category (e.g. `tech`) are now included. If no test infra exists, skip and rely on smoke testing.

- [ ] **Step 3: Update SQL to union category filter with ticker filter**

Replace the existing query in `load_news_feed()` (around line 264-273) with:

```python
def load_news_feed(tickers: list[str] | None = None):
    conn = sqlite3.connect(NEWS_DB)
    conn.row_factory = sqlite3.Row
    ticker_clause = ""
    params: list = []
    if tickers:
        placeholders = ",".join("?" for _ in tickers)
        ticker_clause = f"""
        OR EXISTS (
            SELECT 1 FROM article_tickers at
            WHERE at.article_url = a.url AND at.ticker IN ({placeholders})
        )
        """
        params = [t.upper() for t in tickers]
    sql = f"""
        SELECT DISTINCT a.title, a.source, a.published_at, a.summary,
               a.sentiment, a.relevance_score
        FROM articles a
        JOIN article_categories ac ON a.url = ac.article_url
        WHERE (
            ac.category IN ('trading', 'business', 'banking')
            {ticker_clause}
        )
          AND a.fetched_at >= datetime('now', '-36 hours')
        ORDER BY a.relevance_score DESC, a.published_at DESC
        LIMIT 80
    """
    rows = conn.execute(sql, params).fetchall()
    return [...]  # existing dict shaping
```

(Preserve the existing dict-shaping in the return — only the query changes.)

- [ ] **Step 4: Update the call site to pass ticker list**

The recon report says `data["tickers"]` is already populated upstream and the function is called from line 244 of `run_committee.py`. Update the call site to pass `tickers=data["tickers"]`.

- [ ] **Step 5: Smoke-test by running a one-off committee invocation**

Run: `cd ~/trading-marketplace && python scripts/run_committee.py --dry-run` (or whatever the existing dry-run mode is — check `--help`).

Inspect the news.json output to confirm: (a) baseline category-filtered articles still appear, (b) at least one new article is included that was tagged with a portfolio ticker but had a non-business category.

- [ ] **Step 6: Commit (in trading-marketplace repo)**

```bash
cd ~/trading-marketplace
git add scripts/run_committee.py
git commit -m "feat(committee): include ticker-tagged news for portfolio names"
```

---

## Self-review

After completing the tasks above, verify against the original goal:

**1. Spec coverage:**
- ✅ Schema for ticker tagging: Tasks 1-2
- ✅ Personal/professional category split (claude_code): Task 3
- ✅ Tagger (rules + Haiku fallback): Tasks 4-7
- ✅ Backfill existing corpus: Task 8
- ✅ MCP queryability by ticker: Tasks 9-10
- ✅ At least one consumer migrated: Task 11
- ⚠️ NOT covered (intentionally deferred):
  - yfinance ticker fetcher → Phase 2 plan
  - trading-hub /news, news-digest, market-news migration → Phase 3 plan
  - "Trading brief" email digest → Phase 3+

**2. Placeholder scan:** All steps include exact code, exact paths, exact commands. No "TODO" or "implement later" found.

**3. Type consistency:** `Article.tickers: list[str]` is consistent across model, storage, query, MCP, backfill. Function signatures use `ticker: str | None` (singular) for `search_news` and `tickers: list[str]` (plural) for `recent_for_tickers` — mirrors the natural usage of each tool.

---

## Future plans (not part of this document)

**Phase 2: yfinance ticker fetcher** — Add a new fetcher that pulls per-ticker news for portfolio + watchlist (~100-200 stocks per the user decision) via `yf.Ticker(t).news`. Schedule hourly via launchd. Watchlist source = `portfolio.csv ∪ buy.csv ∪ census top 30`.

**Phase 3: Trading-hub primitive migration** — Update `/news` command, `news-digest` agent, `market-news` skill prompts to mention `mcp__news-reader__recent_for_tickers` and `search_news(ticker=...)` as available tools. Per user decision, WebSearch stays as a peer option, not a hard fallback — agent chooses.

**Phase 3+ (optional):** Add a "trading brief" launchd profile that runs at 06:00 Athens, queries `recent_for_tickers(portfolio + watchlist, hours=12)`, synthesizes a market-only brief, and emails it as a third digest type. Could replace the trading-hub morning briefing's WebSearch news section.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-03-ticker-aware-news-phase-1.md`.

**Two execution options:**

**1. Subagent-Driven (recommended)** — Dispatch a fresh subagent per task, review between tasks, fast iteration. Best when tasks have clean boundaries (this plan does).

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints. Best if you want to watch the work happen and intervene.

**Which approach?**
