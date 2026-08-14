# YouTube Transcript Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace YouTube marketing descriptions with fact-extracted abstracts distilled from actual video transcripts, for the `stack` profile.

**Architecture:** A Mac-side harvester fetches transcripts (YouTube blocks the VPS by IP), distils each once via the `claude` CLI, and stores them in a `transcripts.db` it rsyncs to the VPS. The stack pipeline reads that store read-only and attaches abstracts to articles before scoring. The abstract lives in its own field and column, never in `content`, so `compute_hash()` is unaffected and dedup is unchanged.

**Tech Stack:** Python 3.12+, `youtube-transcript-api`, feedparser, SQLite, the `claude` CLI subprocess (Vertex), rsync, launchd.

**Spec:** `docs/superpowers/specs/2026-08-14-youtube-transcript-enrichment-design.md`

## Global Constraints

- **LLM calls MUST go through the local `claude` CLI** via `subprocess.run(["claude", "--model", "sonnet", "--print"], ...)`. Never add the `anthropic` SDK, never call `api.anthropic.com`. Mock `subprocess.run` in tests.
- **All tests mock HTTP and the `claude` subprocess.** No network calls, no real LLM calls, in any test.
- **Run tests with `PYTHONPATH=. uv run pytest`.** The project has no `[build-system]`, so `uv sync` does not install `news` into `.venv` and a bare `uv run pytest` fails with `ModuleNotFoundError: No module named 'news'`.
- **Lint with `uv run ruff check .` and `uv run ruff format .`** (line-length 100, target py312).
- **Profile scope is `stack` only.** No `digest`, `monitor` or `market` code path changes.
- **Transcript abstracts get an 800-character synthesis allowance**; all other articles keep their existing 300.
- **Retry ceiling is 3 attempts** for transient transcript-fetch failures. `no_captions` is terminal and never retried.
- **The live VPS database is `~/SourceCode/news/data/news.db`.** `/mnt/data/news-data/` is a stale June snapshot; ignore it.

---

## File Structure

| File | Responsibility |
|---|---|
| `news/models.py` | Add `Article.transcript_abstract` |
| `news/storage.py` | Persist the new column: `init_db`, `_migrate_db`, `insert_article`, `_row_to_article` |
| `news/transcripts.py` | **New.** Everything about the transcript store: schema, upsert, status rules, video-ID extraction, read path, article enrichment. Shared by the Mac harvester (writer) and the VPS pipeline (reader) |
| `news/processor.py` | Relevance scoring reads the abstract |
| `news/stack_synth.py` | 800-char snippet for abstract-bearing articles |
| `main.py` | Hook enrichment into `run_stack_pipeline`, thread coverage counts to delivery |
| `news/deliver.py` + `templates/stack.html` | Coverage line in the footer |
| `scripts/youtube_harvest.py` | **New.** Mac-side orchestration: config parse, feed walk, transcript fetch, distillation, rsync push |
| `config/sources.yaml` | Remove `YouTube: Bloomberg TV` |
| `config/stack/sources.yaml` | Expanded channel roster |
| `config/stack/settings.yaml` | `storage.transcripts_db_path` |
| `.gitignore` | `data/transcripts.db` |

---

### Task 1: Article field and storage round-trip

**Files:**
- Modify: `news/models.py:26`
- Modify: `news/storage.py` (`_migrate_db` list ~line 42, `init_db` schema ~line 92, `insert_article` ~line 229, `_row_to_article` ~line 197)
- Test: `tests/test_storage.py`, `tests/test_models.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Article.transcript_abstract: str` — every later task depends on this field existing and persisting.

- [ ] **Step 1: Write the failing tests**

In `tests/test_storage.py`:

```python
def test_insert_article_round_trips_the_transcript_abstract(conn):
    article = Article(
        url="https://www.youtube.com/watch?v=abc12345678",
        title="A Video",
        source="YouTube: Fireship",
        content="Short marketing blurb.",
        categories=["ai"],
        language="en",
        published_at=datetime.now(UTC),
        transcript_abstract="Meta released Muse Glimmer, a 30B agentic model under Apache 2.0.",
    )
    article.compute_hash()
    insert_article(conn, article)

    row = conn.execute("SELECT * FROM articles WHERE url = ?", (article.url,)).fetchone()
    restored = _row_to_article(conn, row)
    assert restored.transcript_abstract == (
        "Meta released Muse Glimmer, a 30B agentic model under Apache 2.0."
    )


def test_migrate_db_is_idempotent_on_an_already_migrated_database(conn):
    """_migrate_db runs on every init_db; a second pass must not raise."""
    _migrate_db(conn)
    _migrate_db(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(articles)")}
    assert "transcript_abstract" in cols
```

In `tests/test_models.py`:

```python
def test_transcript_abstract_does_not_affect_the_content_hash():
    """The abstract must stay out of the hash input or dedup breaks."""
    base = Article(
        url="https://www.youtube.com/watch?v=abc12345678",
        title="A Video",
        source="YouTube: Fireship",
        content="Short marketing blurb.",
        categories=["ai"],
        language="en",
    )
    enriched = Article(
        url="https://www.youtube.com/watch?v=abc12345678",
        title="A Video",
        source="YouTube: Fireship",
        content="Short marketing blurb.",
        categories=["ai"],
        language="en",
        transcript_abstract="A completely different 800 character abstract.",
    )
    base.compute_hash()
    enriched.compute_hash()
    assert base.content_hash == enriched.content_hash
```

Add `_migrate_db` and `_row_to_article` to the imports in `tests/test_storage.py`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. uv run pytest tests/test_storage.py tests/test_models.py -q`
Expected: FAIL — `TypeError: Article.__init__() got an unexpected keyword argument 'transcript_abstract'`

- [ ] **Step 3: Add the field**

In `news/models.py`, after `urgency: str = ""`:

```python
    transcript_abstract: str = ""
```

- [ ] **Step 4: Add the column to the schema and the migration**

In `news/storage.py`, in the `init_db` `CREATE TABLE articles` block, after `urgency TEXT,`:

```sql
            transcript_abstract TEXT,
```

In `_migrate_db`, append to the statement list:

```python
        "ALTER TABLE articles ADD COLUMN transcript_abstract TEXT",
```

- [ ] **Step 5: Persist and restore it**

In `insert_article`, add `transcript_abstract` to the column list and one more `?` to the VALUES tuple, then `article.transcript_abstract` to the parameters.

In `_row_to_article`, add to the `Article(...)` construction:

```python
        transcript_abstract=_row_get(row, "transcript_abstract") or "",
```

There is no `_row_get` helper in `storage.py`; `_row_to_article` indexes `row` directly. Use this exact form, which survives a row read before the migration has run:

```python
        transcript_abstract=(
            row["transcript_abstract"] if "transcript_abstract" in row.keys() else ""
        )
        or "",
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `PYTHONPATH=. uv run pytest tests/test_storage.py tests/test_models.py -q`
Expected: PASS

- [ ] **Step 7: Run the full suite**

Run: `PYTHONPATH=. uv run pytest -q`
Expected: all pass — this change touches every article write path.

- [ ] **Step 8: Commit**

```bash
git add news/models.py news/storage.py tests/test_storage.py tests/test_models.py
git commit -m "feat(storage): add transcript_abstract column, kept out of the content hash"
```

---

### Task 2: Transcript store, schema and write path

**Files:**
- Create: `news/transcripts.py`
- Test: `tests/test_transcripts.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `MAX_ATTEMPTS: int = 3`
  - `init_transcript_db(conn: sqlite3.Connection) -> None`
  - `upsert_transcript(conn, video_id: str, channel: str, title: str, published_at: str | None, transcript: str, abstract: str, status: str) -> None`
  - `pending_video_ids(conn, candidates: list[str]) -> list[str]` — the subset still worth attempting

- [ ] **Step 1: Write the failing tests**

Create `tests/test_transcripts.py`:

```python
import sqlite3

import pytest

from news.transcripts import (
    MAX_ATTEMPTS,
    init_transcript_db,
    pending_video_ids,
    upsert_transcript,
)


@pytest.fixture
def tconn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_transcript_db(conn)
    return conn


def test_upsert_stores_a_successful_transcript(tconn):
    upsert_transcript(
        tconn, "abc12345678", "Fireship", "A Video", "2026-08-11T00:00:00Z",
        transcript="the full spoken words", abstract="the distilled facts", status="ok",
    )
    row = tconn.execute("SELECT * FROM transcripts WHERE video_id='abc12345678'").fetchone()
    assert row["abstract"] == "the distilled facts"
    assert row["status"] == "ok"
    assert row["attempts"] == 1


def test_repeated_failures_increment_attempts(tconn):
    for _ in range(3):
        upsert_transcript(
            tconn, "vid00000001", "Fireship", "A Video", None,
            transcript="", abstract="", status="fetch_failed",
        )
    row = tconn.execute("SELECT * FROM transcripts WHERE video_id='vid00000001'").fetchone()
    assert row["attempts"] == 3


def test_pending_excludes_successful_videos(tconn):
    upsert_transcript(tconn, "done0000001", "Fireship", "Done", None, "t", "a", "ok")
    assert pending_video_ids(tconn, ["done0000001", "new00000001"]) == ["new00000001"]


def test_pending_excludes_videos_with_no_captions_permanently(tconn):
    """no_captions is terminal; that video will never gain captions."""
    upsert_transcript(tconn, "nocap000001", "Fireship", "No Caps", None, "", "", "no_captions")
    assert pending_video_ids(tconn, ["nocap000001"]) == []


def test_pending_retries_a_transient_failure_below_the_ceiling(tconn):
    upsert_transcript(tconn, "flaky000001", "Fireship", "Flaky", None, "", "", "fetch_failed")
    assert pending_video_ids(tconn, ["flaky000001"]) == ["flaky000001"]


def test_pending_gives_up_at_the_attempt_ceiling(tconn):
    for _ in range(MAX_ATTEMPTS):
        upsert_transcript(tconn, "dead0000001", "Fireship", "Dead", None, "", "", "fetch_failed")
    assert pending_video_ids(tconn, ["dead0000001"]) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. uv run pytest tests/test_transcripts.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'news.transcripts'`

- [ ] **Step 3: Write the module**

Create `news/transcripts.py`:

```python
"""Transcript store: the bridge between the Mac harvester and the VPS pipeline.

YouTube blocks datacenter IPs, so transcripts are fetched on a residential
connection and rsynced to the VPS as a standalone SQLite file. The Mac is the
only writer; the pipeline opens it read-only. Keeping it separate from news.db
means neither file ever has two writers.
"""

import sqlite3
from datetime import UTC, datetime

# A transient fetch failure is retried on later harvester runs up to this many
# times, then left alone rather than retried forever.
MAX_ATTEMPTS = 3

# Statuses that will never change on a later run: the work is done, or the
# video simply has no captions and never will.
_TERMINAL_STATUSES = frozenset({"ok", "no_captions"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcripts (
    video_id        TEXT PRIMARY KEY,
    channel         TEXT NOT NULL,
    title           TEXT NOT NULL,
    published_at    TEXT,
    transcript      TEXT,
    abstract        TEXT,
    status          TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    updated_at      TEXT NOT NULL
);
"""


def init_transcript_db(conn: sqlite3.Connection) -> None:
    """Create the transcript table if it does not exist."""
    conn.executescript(_SCHEMA)
    conn.commit()


def upsert_transcript(
    conn: sqlite3.Connection,
    video_id: str,
    channel: str,
    title: str,
    published_at: str | None,
    transcript: str,
    abstract: str,
    status: str,
) -> None:
    """Insert or update one video's record, incrementing the attempt counter."""
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO transcripts (
            video_id, channel, title, published_at, transcript, abstract,
            status, attempts, last_attempt_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(video_id) DO UPDATE SET
            channel         = excluded.channel,
            title           = excluded.title,
            published_at    = excluded.published_at,
            transcript      = excluded.transcript,
            abstract        = excluded.abstract,
            status          = excluded.status,
            attempts        = transcripts.attempts + 1,
            last_attempt_at = excluded.last_attempt_at,
            updated_at      = excluded.updated_at
        """,
        (video_id, channel, title, published_at, transcript, abstract, status, now, now),
    )
    conn.commit()


def pending_video_ids(conn: sqlite3.Connection, candidates: list[str]) -> list[str]:
    """Filter candidates down to those still worth attempting."""
    if not candidates:
        return []
    placeholders = ",".join("?" * len(candidates))
    rows = conn.execute(
        f"SELECT video_id, status, attempts FROM transcripts WHERE video_id IN ({placeholders})",
        candidates,
    ).fetchall()
    settled = {
        row["video_id"]
        for row in rows
        if row["status"] in _TERMINAL_STATUSES or row["attempts"] >= MAX_ATTEMPTS
    }
    return [vid for vid in candidates if vid not in settled]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=. uv run pytest tests/test_transcripts.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add news/transcripts.py tests/test_transcripts.py
git commit -m "feat(transcripts): transcript store schema, upsert and retry ceiling"
```

---

### Task 3: Transcript store, read path and article enrichment

**Files:**
- Modify: `news/transcripts.py`
- Test: `tests/test_transcripts.py`

**Interfaces:**
- Consumes: `Article.transcript_abstract` (Task 1), `init_transcript_db` / `upsert_transcript` (Task 2)
- Produces:
  - `extract_video_id(url: str) -> str | None`
  - `load_abstracts(db_path: str | Path, video_ids: list[str]) -> dict[str, str]`
  - `enrich_articles(articles: list[Article], db_path: str | Path) -> tuple[int, int]` returning `(enriched, total_video_items)`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_transcripts.py`:

```python
from pathlib import Path

from news.models import Article
from news.transcripts import enrich_articles, extract_video_id, load_abstracts


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=G55HSGpuh1M", "G55HSGpuh1M"),
        ("https://youtube.com/watch?list=PL1&v=G55HSGpuh1M", "G55HSGpuh1M"),
        ("https://youtu.be/G55HSGpuh1M", "G55HSGpuh1M"),
        ("https://techcrunch.com/2026/08/14/story", None),
        ("", None),
    ],
)
def test_extract_video_id(url, expected):
    assert extract_video_id(url) == expected


def _seed_store(tmp_path: Path) -> Path:
    db_path = tmp_path / "transcripts.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_transcript_db(conn)
    upsert_transcript(conn, "G55HSGpuh1M", "Fireship", "Muse Glimmer", None,
                      "full words", "Meta released Muse Glimmer under Apache 2.0.", "ok")
    upsert_transcript(conn, "nocap000001", "Fireship", "No Caps", None, "", "", "no_captions")
    conn.close()
    return db_path


def test_load_abstracts_returns_only_successful_records(tmp_path):
    db_path = _seed_store(tmp_path)
    got = load_abstracts(db_path, ["G55HSGpuh1M", "nocap000001"])
    assert got == {"G55HSGpuh1M": "Meta released Muse Glimmer under Apache 2.0."}


def test_load_abstracts_returns_empty_when_the_database_is_absent(tmp_path):
    """VPS first run, or a Mac that has never harvested. Must not raise."""
    assert load_abstracts(tmp_path / "nope.db", ["G55HSGpuh1M"]) == {}


def _article(url: str) -> Article:
    return Article(url=url, title="T", source="S", content="c", categories=[], language="en")


def test_enrich_articles_attaches_abstracts_to_video_items(tmp_path):
    db_path = _seed_store(tmp_path)
    articles = [_article("https://www.youtube.com/watch?v=G55HSGpuh1M")]

    enriched, total = enrich_articles(articles, db_path)

    assert (enriched, total) == (1, 1)
    assert articles[0].transcript_abstract == "Meta released Muse Glimmer under Apache 2.0."


def test_enrich_articles_leaves_non_youtube_articles_untouched(tmp_path):
    db_path = _seed_store(tmp_path)
    articles = [_article("https://techcrunch.com/story")]

    enriched, total = enrich_articles(articles, db_path)

    assert (enriched, total) == (0, 0)
    assert articles[0].transcript_abstract == ""


def test_enrich_articles_reports_partial_coverage(tmp_path):
    """A video the harvester has not reached yet counts toward the total, not the enriched."""
    db_path = _seed_store(tmp_path)
    articles = [
        _article("https://www.youtube.com/watch?v=G55HSGpuh1M"),
        _article("https://www.youtube.com/watch?v=unseen00001"),
    ]

    enriched, total = enrich_articles(articles, db_path)

    assert (enriched, total) == (1, 2)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. uv run pytest tests/test_transcripts.py -q`
Expected: FAIL — `ImportError: cannot import name 'enrich_articles'`

- [ ] **Step 3: Write the read path**

Append to `news/transcripts.py` (add `import re` and `from pathlib import Path` and `from news.models import Article` at the top):

```python
# Matches both the long watch URL and the youtu.be short form. YouTube ids are
# always 11 characters from the URL-safe alphabet.
_VIDEO_ID_RE = re.compile(r"(?:youtube\.com/watch\?(?:[^ ]*&)?v=|youtu\.be/)([A-Za-z0-9_-]{11})")


def extract_video_id(url: str) -> str | None:
    """Return the YouTube video id in a URL, or None if it is not a video URL."""
    if not url:
        return None
    match = _VIDEO_ID_RE.search(url)
    return match.group(1) if match else None


def load_abstracts(db_path: str | Path, video_ids: list[str]) -> dict[str, str]:
    """Read abstracts for the given video ids. Returns {} if the store is unusable.

    Opened read-only: the VPS is a consumer of this file, never a writer. A
    missing or corrupt store degrades to no enrichment rather than an error,
    which leaves the pipeline exactly where it was before this feature.
    """
    path = Path(db_path)
    if not path.exists() or not video_ids:
        return {}

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}

    try:
        placeholders = ",".join("?" * len(video_ids))
        rows = conn.execute(
            f"SELECT video_id, abstract FROM transcripts "
            f"WHERE status = 'ok' AND abstract IS NOT NULL AND abstract != '' "
            f"AND video_id IN ({placeholders})",
            video_ids,
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()

    return {video_id: abstract for video_id, abstract in rows}


def enrich_articles(articles: list[Article], db_path: str | Path) -> tuple[int, int]:
    """Attach transcript abstracts to YouTube articles in place.

    Returns (enriched, total_video_items) so the caller can report coverage. A
    gap between the two means the harvester has not caught up, which is the
    expected state when the Mac was asleep.
    """
    video_items = []
    for article in articles:
        video_id = extract_video_id(article.url)
        if video_id:
            video_items.append((article, video_id))

    if not video_items:
        return 0, 0

    abstracts = load_abstracts(db_path, [video_id for _, video_id in video_items])

    enriched = 0
    for article, video_id in video_items:
        abstract = abstracts.get(video_id)
        if abstract:
            article.transcript_abstract = abstract
            enriched += 1

    return enriched, len(video_items)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=. uv run pytest tests/test_transcripts.py -q`
Expected: PASS (16 tests)

- [ ] **Step 5: Commit**

```bash
git add news/transcripts.py tests/test_transcripts.py
git commit -m "feat(transcripts): video-id extraction, read-only lookup and article enrichment"
```

---

### Task 4: Relevance scoring reads the abstract

**Files:**
- Modify: `news/processor.py:105` (inside `compute_relevance_score`)
- Test: `tests/test_processor.py`

**Interfaces:**
- Consumes: `Article.transcript_abstract` (Task 1)
- Produces: no new symbols; changes the behaviour of `compute_relevance_score`

**Why this task exists:** the stack pool routinely holds 400-900 articles against a `max_digest_articles: 150` cap, so selection genuinely drops content. A video whose description never mentions Claude but whose transcript does would otherwise be cut before its abstract was ever read.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_processor.py`:

```python
def test_relevance_score_counts_a_claude_mention_found_only_in_the_abstract():
    """The description is marketing copy; the substance is in the transcript."""
    article = _make_article(content="Subscribe for more videos every week!")
    article.transcript_abstract = (
        "A walkthrough of building an MCP server and wiring it into Claude Code."
    )

    score = compute_relevance_score(article, {"claude_mention": 30}, source_tier=2)

    assert score == 30
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. uv run pytest tests/test_processor.py::test_relevance_score_counts_a_claude_mention_found_only_in_the_abstract -q`
Expected: FAIL — `assert 0 == 30`

- [ ] **Step 3: Include the abstract in the match text**

In `news/processor.py`, in `compute_relevance_score`, change:

```python
    text = (article.title + " " + article.content).lower()
```

to:

```python
    # The abstract carries what was actually said in a video, while content is
    # only the uploader's blurb. Scoring reads both, or transcript-only signal
    # never survives selection.
    text = (article.title + " " + article.content + " " + article.transcript_abstract).lower()
```

Leave `classify_article` unchanged: category assignment should reflect an item's headline topic, while scoring decides whether it survives at all.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=. uv run pytest tests/test_processor.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add news/processor.py tests/test_processor.py
git commit -m "feat(processor): relevance scoring reads the transcript abstract"
```

---

### Task 5: Stack synthesis gives abstracts an 800-char allowance

**Files:**
- Modify: `news/stack_synth.py:123`
- Test: `tests/test_stack_synth.py` (create if absent)

**Interfaces:**
- Consumes: `Article.transcript_abstract` (Task 1)
- Produces: no new symbols

- [ ] **Step 1: Write the failing tests**

Create `tests/test_stack_synth.py`. The prompt builder is `build_stack_prompt` (`news/stack_synth.py:133`); import it and `Article` from `news.models`:

```python
def test_stack_prompt_gives_an_abstract_the_larger_allowance():
    article = Article(
        url="https://www.youtube.com/watch?v=G55HSGpuh1M",
        title="A Video", source="YouTube: Fireship",
        content="blurb", categories=["ai"], language="en",
    )
    article.transcript_abstract = "x" * 1000

    prompt = build_stack_prompt([article], time_window="13:00")

    assert "x" * 800 in prompt
    assert "x" * 801 not in prompt


def test_stack_prompt_keeps_the_300_char_cap_for_plain_articles():
    article = Article(
        url="https://techcrunch.com/story", title="A Story", source="TechCrunch",
        content="y" * 500, categories=["tech"], language="en",
    )

    prompt = build_stack_prompt([article], time_window="13:00")

    assert "y" * 300 in prompt
    assert "y" * 301 not in prompt
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. uv run pytest tests/test_stack_synth.py -q`
Expected: FAIL — the 1000-char abstract is not in the prompt at all

- [ ] **Step 3: Prefer the abstract, with its own cap**

In `news/stack_synth.py`, replace:

```python
        "snippet": article.content[:300] if article.content else "",
```

with:

```python
        # A transcript abstract is distilled fact rather than marketing copy, so
        # it earns a larger allowance. ~20 video items at 800 chars is roughly
        # +2.5k tokens, noise against the 150s synthesis timeout.
        "snippet": (
            article.transcript_abstract[:800]
            if article.transcript_abstract
            else (article.content[:300] if article.content else "")
        ),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=. uv run pytest tests/test_stack_synth.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add news/stack_synth.py tests/test_stack_synth.py
git commit -m "feat(stack): give transcript abstracts an 800-char synthesis allowance"
```

---

### Task 6: Hook enrichment into the stack pipeline

**Files:**
- Modify: `config/stack/settings.yaml` (storage block)
- Modify: `main.py:1131-1146` (`run_stack_pipeline`), `main.py:538-575` (`_setup_digest_pipeline`)
- Test: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `enrich_articles` (Task 3)
- Produces: `config["transcripts_db_path"]: Path`

- [ ] **Step 1: Add the config key**

In `config/stack/settings.yaml`, in the `storage:` block:

```yaml
  transcripts_db_path: ~/SourceCode/news/data/transcripts.db
```

- [ ] **Step 2: Write the failing test**

Append to `tests/test_orchestrator.py`:

```python
def test_stack_pipeline_attaches_transcript_abstracts_before_storing(tmp_path):
    """Enrichment must land before compute_hash/process_articles so scoring sees it."""
    transcripts_db = tmp_path / "transcripts.db"
    tconn = sqlite3.connect(transcripts_db)
    tconn.row_factory = sqlite3.Row
    init_transcript_db(tconn)
    upsert_transcript(tconn, "G55HSGpuh1M", "Fireship", "Muse Glimmer", None,
                      "full words", "Meta released Muse Glimmer under Apache 2.0.", "ok")
    tconn.close()

    settings = {
        "pipeline": {
            "max_digest_articles": 100, "max_articles_per_source": 10,
            "min_article_length_words": 3, "max_article_age_hours": 36,
            "digest_window_hours": 36,
        },
        "email": {"recipient": "test@example.com"},
        "storage": {
            "db_path": str(tmp_path / "news.db"),
            "run_log_path": str(tmp_path / "runs.log"),
            "transcripts_db_path": str(transcripts_db),
        },
        "schedule": {"timezone": "Europe/Athens", "runs": ["13:00"]},
        "synthesis": {"max_retries": 1, "timeout": 60, "claude_command": "claude"},
        "scoring": {},
    }
    video = Article(
        url="https://www.youtube.com/watch?v=G55HSGpuh1M",
        title="Muse Glimmer", source="YouTube: Fireship",
        content="Subscribe for more!", categories=["ai"], language="en",
        published_at=datetime.now(UTC),
    )

    mem_conn = sqlite3.connect(":memory:")
    mem_conn.row_factory = sqlite3.Row
    init_db(mem_conn)
    stored: list[Article] = []

    with (
        patch("main.get_settings", return_value=settings),
        patch("main.get_sources", return_value={"rss_feeds": []}),
        patch("main.get_categories", return_value={}),
        patch("main.get_connection", return_value=mem_conn),
        patch("main.init_db"),
        patch("main.get_last_digest", return_value=None),
        patch("main.fetch_all_sources", new_callable=AsyncMock, return_value=([video], [])),
        patch("main.insert_article", side_effect=lambda conn, a: stored.append(a)),
        patch("main.get_articles_since", return_value=[]),
        patch("main.check_gcloud_auth", return_value=True),
        # NOT "main.synthesize_stack": main.py imports it locally inside the
        # function (main.py:1182), so only the source module patch takes effect.
        patch("news.stack_synth.synthesize_stack", return_value=("fallback", False)),
        patch("main.send_email", return_value=True),
        patch("main.insert_digest", return_value=1),
        patch("main.update_digest_sent"),
    ):
        asyncio.run(run_stack_pipeline())

    assert len(stored) == 1
    assert stored[0].transcript_abstract == "Meta released Muse Glimmer under Apache 2.0."
```

Add the needed imports: `run_stack_pipeline` from `main`, and `init_transcript_db`, `upsert_transcript` from `news.transcripts`.

- [ ] **Step 3: Run the test to verify it fails**

Run: `PYTHONPATH=. uv run pytest tests/test_orchestrator.py::test_stack_pipeline_attaches_transcript_abstracts_before_storing -q`
Expected: FAIL — `assert '' == 'Meta released Muse Glimmer under Apache 2.0.'`

- [ ] **Step 4: Expose the path in config**

In `main.py`, in `_setup_digest_pipeline`, after the `run_log_path` line:

```python
    transcripts_db_path = Path(
        storage_config.get("transcripts_db_path", db_path.parent / "transcripts.db")
    ).expanduser()
```

and add to the returned dict:

```python
        "transcripts_db_path": transcripts_db_path,
```

- [ ] **Step 5: Enrich before hashing**

In `main.py`, in `run_stack_pipeline`, between the fetch-error logging block and the `# PROCESS` comment (around line 1139):

```python
    # ENRICH: attach transcript abstracts before hashing and scoring. The
    # abstract is a separate field, so the hash input is untouched; doing it
    # here is what lets process_articles score on what was actually said.
    enriched, video_total = enrich_articles(raw_articles, config["transcripts_db_path"])
    if video_total:
        logger.info(f"Transcript enrichment: {enriched}/{video_total} YouTube items enriched")
```

Add the import at the top of `main.py`:

```python
from news.transcripts import enrich_articles
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `PYTHONPATH=. uv run pytest tests/test_orchestrator.py -q`
Expected: PASS

- [ ] **Step 7: Run the full suite**

Run: `PYTHONPATH=. uv run pytest -q`
Expected: all pass. `_setup_digest_pipeline` is shared by four pipelines, so a `KeyError` here breaks all of them.

- [ ] **Step 8: Commit**

```bash
git add main.py config/stack/settings.yaml tests/test_orchestrator.py
git commit -m "feat(stack): enrich YouTube articles with transcript abstracts before scoring"
```

---

### Task 7: Coverage line in the email footer

**Files:**
- Modify: `news/deliver.py:514-522` (`render_stack_html`), `templates/stack.html:243`
- Modify: `main.py` (`run_stack_pipeline` render call)
- Test: `tests/test_deliver.py`

**Interfaces:**
- Consumes: the `(enriched, video_total)` counts from Task 6
- Produces: `render_stack_html(..., transcript_coverage: str = "")`

**Why this task exists:** the owner accepted the Mac-asleep failure mode on the explicit condition that it not be silent. A VPS log line nobody opens does not satisfy that; the footer of the email he actually reads does.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_deliver.py`:

```python
def test_stack_html_shows_transcript_coverage_when_provided():
    html = render_stack_html(
        synthesis={"executive_brief": [], "sections": []},
        article_count=10, source_count=3,
        time_display="13:00", date_display="Fri 14 AUG",
        transcript_coverage="11/14 videos transcribed",
    )
    assert "11/14 videos transcribed" in html


def test_stack_html_omits_the_coverage_line_when_there_are_no_videos():
    html = render_stack_html(
        synthesis={"executive_brief": [], "sections": []},
        article_count=10, source_count=3,
        time_display="13:00", date_display="Fri 14 AUG",
    )
    assert "videos transcribed" not in html
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. uv run pytest tests/test_deliver.py -q`
Expected: FAIL — `TypeError: render_stack_html() got an unexpected keyword argument 'transcript_coverage'`

- [ ] **Step 3: Thread the parameter through**

In `news/deliver.py`, add `transcript_coverage: str = ""` to the `render_stack_html` signature, and pass it into `template.render(...)`.

In `templates/stack.html`, line 243, extend the footer:

```html
Stack | daily AI/dev intelligence<br>{{ source_count }} sources | {{ article_count }} articles{% if transcript_coverage %} | {{ transcript_coverage }}{% endif %}{% if next_run %} | next: {{ next_run }}{% endif %}
```

- [ ] **Step 4: Pass the real counts from the pipeline**

In `main.py`, in `run_stack_pipeline`, keep the enrichment counts in scope and pass them to the render call:

```python
    coverage = f"{enriched}/{video_total} videos transcribed" if video_total else ""
```

then add `transcript_coverage=coverage,` to the `render_stack_html(...)` call.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHONPATH=. uv run pytest tests/test_deliver.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add news/deliver.py templates/stack.html main.py tests/test_deliver.py
git commit -m "feat(stack): report transcript coverage in the email footer"
```

---

### Task 8: Harvester, channel discovery and work queue

**Files:**
- Create: `scripts/youtube_harvest.py`
- Test: `tests/test_youtube_harvest.py`

**Interfaces:**
- Consumes: `pending_video_ids` (Task 2)
- Produces:
  - `youtube_channels(sources: dict) -> list[tuple[str, str]]` — `(feed_name, channel_id)`
  - `videos_in_feed(xml: str) -> list[dict]` — dicts with `video_id`, `title`, `published`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_youtube_harvest.py`:

```python
from scripts.youtube_harvest import videos_in_feed, youtube_channels

SAMPLE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns="http://www.w3.org/2005/Atom">
  <title>Fireship</title>
  <entry>
    <yt:videoId>G55HSGpuh1M</yt:videoId>
    <title>Meta's new model</title>
    <published>2026-08-11T18:00:00+00:00</published>
  </entry>
  <entry>
    <yt:videoId>abc12345678</yt:videoId>
    <title>Another video</title>
    <published>2026-08-10T18:00:00+00:00</published>
  </entry>
</feed>"""


def test_youtube_channels_extracts_ids_from_feed_urls():
    sources = {
        "rss_feeds": [
            {"name": "YouTube: Fireship",
             "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCsBjURrPoezykLs9EqgamOA"},
            {"name": "TechCrunch", "url": "https://techcrunch.com/feed/"},
        ]
    }
    assert youtube_channels(sources) == [
        ("YouTube: Fireship", "UCsBjURrPoezykLs9EqgamOA")
    ]


def test_youtube_channels_ignores_a_youtube_url_without_a_channel_id():
    sources = {"rss_feeds": [{"name": "Broken", "url": "https://www.youtube.com/feeds/videos.xml"}]}
    assert youtube_channels(sources) == []


def test_videos_in_feed_extracts_id_title_and_date():
    videos = videos_in_feed(SAMPLE_ATOM)
    assert [v["video_id"] for v in videos] == ["G55HSGpuh1M", "abc12345678"]
    assert videos[0]["title"] == "Meta's new model"
    assert videos[0]["published"].startswith("2026-08-11")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. uv run pytest tests/test_youtube_harvest.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.youtube_harvest'`

If `scripts/` has no `__init__.py` and the import fails for that reason, add an empty `scripts/__init__.py`.

- [ ] **Step 3: Write the discovery half**

Create `scripts/youtube_harvest.py`:

```python
"""Mac-side YouTube transcript harvester.

YouTube serves caption data to residential IPs and refuses datacenter ones, so
this runs on the Mac rather than the VPS. It fetches transcripts for new videos
on the stack profile's channels, distils each one into a factual abstract, and
rsyncs the resulting store to the VPS for the pipeline to read.

Run hourly via launchd. Idempotent: a video already recorded is never refetched.
"""

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import feedparser

_YT_FEED_HOST = "youtube.com/feeds/videos.xml"


def youtube_channels(sources: dict[str, Any]) -> list[tuple[str, str]]:
    """Extract (feed name, channel id) for every YouTube feed in a sources config.

    Reading the roster from the same config the pipeline fetches means adding a
    channel stays a one-place edit.
    """
    channels: list[tuple[str, str]] = []
    for source in sources.get("rss_feeds", []):
        url = source.get("url", "")
        if _YT_FEED_HOST not in url:
            continue
        channel_id = parse_qs(urlparse(url).query).get("channel_id", [""])[0]
        if channel_id:
            channels.append((source["name"], channel_id))
    return channels


def videos_in_feed(xml: str) -> list[dict[str, str]]:
    """Parse a YouTube Atom feed into video records."""
    feed = feedparser.parse(xml)
    videos = []
    for entry in feed.entries:
        video_id = entry.get("yt_videoid")
        if not video_id:
            continue
        videos.append({
            "video_id": video_id,
            "title": entry.get("title", ""),
            "published": entry.get("published", ""),
        })
    return videos
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=. uv run pytest tests/test_youtube_harvest.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/youtube_harvest.py tests/test_youtube_harvest.py
git commit -m "feat(harvest): discover YouTube channels and parse their Atom feeds"
```

---

### Task 9: Harvester, transcript fetch and distillation

**Files:**
- Modify: `scripts/youtube_harvest.py`
- Modify: `pyproject.toml` (dependencies)
- Test: `tests/test_youtube_harvest.py`

**Interfaces:**
- Consumes: `upsert_transcript` (Task 2)
- Produces:
  - `fetch_transcript(video_id: str) -> tuple[str, str]` returning `(text, status)` where status is `ok`, `no_captions` or `fetch_failed`
  - `distil(transcript: str, title: str) -> str` — the abstract, or `""` on failure

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, add to `dependencies`:

```python
    "youtube-transcript-api",
```

Then run `uv lock` and commit the lockfile, or Dependabot alerts will drift from the manifest.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_youtube_harvest.py`:

```python
from unittest.mock import Mock, patch

from scripts.youtube_harvest import distil, fetch_transcript


def test_fetch_transcript_joins_snippet_text():
    snippets = [Mock(text="On Monday,"), Mock(text="Meta released a model.")]
    with patch("scripts.youtube_harvest.YouTubeTranscriptApi") as api:
        api.return_value.fetch.return_value = snippets
        text, status = fetch_transcript("G55HSGpuh1M")

    assert status == "ok"
    assert text == "On Monday, Meta released a model."


def test_fetch_transcript_marks_a_captionless_video_terminal():
    from youtube_transcript_api import TranscriptsDisabled

    with patch("scripts.youtube_harvest.YouTubeTranscriptApi") as api:
        api.return_value.fetch.side_effect = TranscriptsDisabled("G55HSGpuh1M")
        text, status = fetch_transcript("G55HSGpuh1M")

    assert (text, status) == ("", "no_captions")


def test_fetch_transcript_marks_a_blocked_request_retryable():
    with patch("scripts.youtube_harvest.YouTubeTranscriptApi") as api:
        api.return_value.fetch.side_effect = RuntimeError("RequestBlocked")
        text, status = fetch_transcript("G55HSGpuh1M")

    assert (text, status) == ("", "fetch_failed")


def test_distil_calls_the_claude_cli_and_returns_the_abstract():
    completed = Mock(returncode=0, stdout="  Meta released Muse Glimmer under Apache 2.0.  ")
    with patch("scripts.youtube_harvest.subprocess.run", return_value=completed) as run:
        abstract = distil("the full spoken transcript", "Meta's new model")

    assert abstract == "Meta released Muse Glimmer under Apache 2.0."
    argv = run.call_args[0][0]
    assert argv[:1] == ["claude"]
    assert "--model" in argv and "sonnet" in argv


def test_distil_returns_empty_when_the_cli_fails():
    completed = Mock(returncode=1, stdout="", stderr="boom")
    with patch("scripts.youtube_harvest.subprocess.run", return_value=completed):
        assert distil("transcript", "title") == ""
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `PYTHONPATH=. uv run pytest tests/test_youtube_harvest.py -q`
Expected: FAIL — `ImportError: cannot import name 'distil'`

- [ ] **Step 4: Write fetch and distillation**

Append to `scripts/youtube_harvest.py` (add `import subprocess` and `import logging` at the top):

```python
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)

logger = logging.getLogger(__name__)

_CLI_TIMEOUT = 120

# The whole point of the feature: the uploader's description is written to sell
# the click, so we ask for the substance and name the things to throw away.
_DISTIL_PROMPT = """\
Below is the transcript of a technical video titled "{title}".

Write a dense factual abstract of it in 600-800 characters.

Rules:
- Lead with the single most consequential concrete fact.
- Keep specifics: names, versions, numbers, licences, benchmarks, tool names.
- Drop every sponsor read, discount code, subscribe request and channel plug.
- Drop hype framing ("insane", "game-changer", "you won't believe").
- Write plain declarative prose. No preamble, no markdown, no bullet points.
- Output the abstract only.

TRANSCRIPT:
{transcript}
"""


def fetch_transcript(video_id: str) -> tuple[str, str]:
    """Fetch a video's captions. Returns (text, status).

    Status is 'ok', 'no_captions' when the video will never have captions, or
    'fetch_failed' for anything transient such as an IP block or a timeout.
    """
    try:
        snippets = YouTubeTranscriptApi().fetch(video_id)
        return " ".join(snippet.text for snippet in snippets).strip(), "ok"
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable):
        return "", "no_captions"
    except Exception as e:
        logger.warning(f"{video_id}: transcript fetch failed: {type(e).__name__}: {e}")
        return "", "fetch_failed"


def distil(transcript: str, title: str) -> str:
    """Distil a transcript into a factual abstract via the local claude CLI.

    Routed through the CLI (Vertex) rather than any SDK, per project policy.
    Returns "" on any failure; the caller records summary_failed and keeps the
    raw transcript so only the cheap half is retried.
    """
    prompt = _DISTIL_PROMPT.format(title=title, transcript=transcript)
    try:
        result = subprocess.run(
            ["claude", "--model", "sonnet", "--print"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning(f"distillation call failed: {type(e).__name__}: {e}")
        return ""

    if result.returncode != 0:
        logger.warning(f"claude CLI exited {result.returncode}: {(result.stderr or '')[:200]}")
        return ""

    return (result.stdout or "").strip()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHONPATH=. uv run pytest tests/test_youtube_harvest.py -q`
Expected: PASS (8 tests)

- [ ] **Step 6: Commit**

```bash
git add scripts/youtube_harvest.py tests/test_youtube_harvest.py pyproject.toml uv.lock
git commit -m "feat(harvest): fetch transcripts and distil them via the claude CLI"
```

---

### Task 10: Harvester entry point, rsync push and scheduling

**Files:**
- Modify: `scripts/youtube_harvest.py`
- Modify: `.gitignore`
- Create: `~/Library/LaunchAgents/com.plessas.youtube-harvest.plist` (outside the repo)
- Test: `tests/test_youtube_harvest.py`

**Interfaces:**
- Consumes: everything from Tasks 2, 8, 9
- Produces: `harvest(sources, db_path, limit) -> dict` with counts, and a `main()` CLI entry

- [ ] **Step 1: Write the failing test**

Append to `tests/test_youtube_harvest.py`:

```python
def test_harvest_skips_videos_already_in_the_store(tmp_path):
    """Idempotence: the hourly run must not refetch or re-distil settled videos."""
    import sqlite3

    from news.transcripts import init_transcript_db, upsert_transcript
    from scripts.youtube_harvest import harvest

    db_path = tmp_path / "transcripts.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_transcript_db(conn)
    upsert_transcript(conn, "G55HSGpuh1M", "Fireship", "Done", None, "t", "a", "ok")
    conn.close()

    sources = {"rss_feeds": [{
        "name": "YouTube: Fireship",
        "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCsBjURrPoezykLs9EqgamOA",
    }]}

    resp = Mock(text=SAMPLE_ATOM, status_code=200)
    resp.raise_for_status = Mock()

    with (
        patch("scripts.youtube_harvest.httpx.get", return_value=resp),
        patch("scripts.youtube_harvest.fetch_transcript",
              return_value=("spoken words", "ok")) as fetch,
        patch("scripts.youtube_harvest.distil", return_value="the abstract"),
    ):
        stats = harvest(sources, db_path, limit=10)

    # SAMPLE_ATOM has two videos; one is already settled, so only one is fetched.
    assert fetch.call_count == 1
    assert stats["attempted"] == 1
    assert stats["ok"] == 1


def test_harvest_records_summary_failed_but_keeps_the_transcript(tmp_path):
    import sqlite3

    from scripts.youtube_harvest import harvest

    db_path = tmp_path / "transcripts.db"
    sources = {"rss_feeds": [{
        "name": "YouTube: Fireship",
        "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCsBjURrPoezykLs9EqgamOA",
    }]}
    resp = Mock(text=SAMPLE_ATOM, status_code=200)
    resp.raise_for_status = Mock()

    with (
        patch("scripts.youtube_harvest.httpx.get", return_value=resp),
        patch("scripts.youtube_harvest.fetch_transcript", return_value=("spoken words", "ok")),
        patch("scripts.youtube_harvest.distil", return_value=""),
    ):
        harvest(sources, db_path, limit=10)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM transcripts WHERE video_id='G55HSGpuh1M'").fetchone()
    assert row["status"] == "summary_failed"
    assert row["transcript"] == "spoken words"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. uv run pytest tests/test_youtube_harvest.py -q`
Expected: FAIL — `ImportError: cannot import name 'harvest'`

- [ ] **Step 3: Write the orchestration**

Append to `scripts/youtube_harvest.py` (add `import argparse`, `import sqlite3`, `import sys`, `from pathlib import Path`, `import httpx`, and the `news.config` / `news.transcripts` imports):

```python
_FEED_TIMEOUT = 30
# Bounds the work and the spend in any single hourly run. A backlog drains over
# successive runs rather than firing hundreds of CLI calls at once.
_DEFAULT_LIMIT = 25


def harvest(sources: dict[str, Any], db_path: Path, limit: int = _DEFAULT_LIMIT) -> dict[str, int]:
    """Fetch and distil transcripts for new videos. Returns run counts."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_transcript_db(conn)

    stats = {"channels": 0, "attempted": 0, "ok": 0, "no_captions": 0, "failed": 0}

    try:
        for name, channel_id in youtube_channels(sources):
            stats["channels"] += 1
            url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
            try:
                response = httpx.get(url, timeout=_FEED_TIMEOUT, follow_redirects=True)
                response.raise_for_status()
            except Exception as e:
                logger.warning(f"{name}: feed fetch failed: {type(e).__name__}: {e}")
                continue

            videos = videos_in_feed(response.text)
            by_id = {v["video_id"]: v for v in videos}
            todo = pending_video_ids(conn, list(by_id))

            for video_id in todo:
                if stats["attempted"] >= limit:
                    logger.info(f"Run limit {limit} reached; remaining videos wait for next run")
                    return stats

                video = by_id[video_id]
                stats["attempted"] += 1
                text, status = fetch_transcript(video_id)

                abstract = ""
                if status == "ok":
                    abstract = distil(text, video["title"])
                    if not abstract:
                        status = "summary_failed"

                upsert_transcript(
                    conn, video_id, name, video["title"], video["published"],
                    text, abstract, status,
                )

                if status == "ok":
                    stats["ok"] += 1
                elif status == "no_captions":
                    stats["no_captions"] += 1
                else:
                    stats["failed"] += 1
    finally:
        conn.close()

    return stats


def push_to_vps(db_path: Path, remote: str) -> bool:
    """rsync the store to the VPS. Returns True on success."""
    try:
        result = subprocess.run(
            ["rsync", "-az", "--timeout=30", str(db_path), remote],
            capture_output=True, text=True, timeout=180,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.error(f"rsync failed: {type(e).__name__}: {e}")
        return False
    if result.returncode != 0:
        logger.error(f"rsync exited {result.returncode}: {(result.stderr or '')[:200]}")
        return False
    return True


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="Harvest YouTube transcripts for the stack profile")
    parser.add_argument("--profile", default="stack")
    parser.add_argument("--limit", type=int, default=_DEFAULT_LIMIT)
    parser.add_argument("--db", default=str(Path.home() / "SourceCode/news/data/transcripts.db"))
    parser.add_argument("--remote", default="vps:~/SourceCode/news/data/transcripts.db")
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args()

    sources = get_sources(profile=args.profile)
    stats = harvest(sources, Path(args.db), limit=args.limit)
    logger.info(
        f"Harvest complete: {stats['channels']} channels, {stats['attempted']} attempted, "
        f"{stats['ok']} ok, {stats['no_captions']} no captions, {stats['failed']} failed"
    )

    if args.no_push:
        return 0
    return 0 if push_to_vps(Path(args.db), args.remote) else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=. uv run pytest tests/test_youtube_harvest.py -q`
Expected: PASS (10 tests)

- [ ] **Step 5: Ignore the store**

In `.gitignore`, under the `# Runtime data` block:

```text
data/transcripts.db
data/transcripts.db-wal
data/transcripts.db-shm
```

- [ ] **Step 6: Verify against the real world once, by hand**

```bash
PYTHONPATH=. uv run python scripts/youtube_harvest.py --limit 2 --no-push
PYTHONPATH=. uv run python -c "
import sqlite3
c = sqlite3.connect('data/transcripts.db'); c.row_factory = sqlite3.Row
for r in c.execute('SELECT video_id, status, length(transcript) t, length(abstract) a, title FROM transcripts'):
    print(dict(r))
"
```

Expected: at least one row with `status='ok'`, a transcript of a few thousand characters and an abstract of roughly 600-800. Read the abstract and confirm it reads as fact rather than marketing. This is the one step that proves the feature actually does what it was built for.

- [ ] **Step 7: Install the launchd job**

Write `~/Library/LaunchAgents/com.plessas.youtube-harvest.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.plessas.youtube-harvest</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-lc</string>
        <string>cd ~/SourceCode/news &amp;&amp; PYTHONPATH=. uv run python scripts/youtube_harvest.py</string>
    </array>
    <key>StartInterval</key>
    <integer>3600</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/plessas/Library/Logs/youtube-harvest.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/plessas/Library/Logs/youtube-harvest.err</string>
</dict>
</plist>
```

Then: `launchctl load ~/Library/LaunchAgents/com.plessas.youtube-harvest.plist`

- [ ] **Step 8: Commit**

```bash
git add scripts/youtube_harvest.py tests/test_youtube_harvest.py .gitignore
git commit -m "feat(harvest): harvest orchestration, rsync push and hourly launchd job"
```

---

### Task 11: Channel roster and Bloomberg TV removal

**Files:**
- Modify: `config/sources.yaml` (remove `YouTube: Bloomberg TV`)
- Modify: `config/stack/sources.yaml` (expanded roster)

> **NOTE:** the verified channel roster is produced by the `youtube-channel-research`
> workflow and inserted here before execution begins. Every entry in it has had its
> `channel_id` resolved and its Atom feed confirmed to return at least one entry.
> Do not add a channel to this file that has not been through that verification.

- [ ] **Step 1: Remove Bloomberg TV from digest**

In `config/sources.yaml`, delete the five-line `YouTube: Bloomberg TV` entry (around line 161). Digest already carries Reuters Business, FT, FT International, WSJ Markets, Bloomberg via Google and CNBC as text feeds covering the same beat with real prose.

- [ ] **Step 2: Add the verified roster to the stack profile**

Append the curated entries to `rss_feeds` in `config/stack/sources.yaml`, following the existing format exactly.

- [ ] **Step 3: Prove every configured feed is alive**

```bash
PYTHONPATH=. uv run python -c "
import asyncio, yaml
from news.fetcher import fetch_all_sources
s = yaml.safe_load(open('config/stack/sources.yaml'))
yt = [f for f in s['rss_feeds'] if 'youtube.com/feeds' in f['url']]
arts, errs = asyncio.run(fetch_all_sources({'rss_feeds': yt}))
by = {}
for a in arts: by[a.source] = by.get(a.source, 0) + 1
for f in yt:
    print(('OK  ' if by.get(f['name']) else 'DEAD'), by.get(f['name'], 0), f['name'])
print('errors:', errs)
"
```

Expected: every channel prints `OK` with a non-zero count. Any `DEAD` line is a wrong channel id and must be fixed or the entry removed before commit. The dead-source warning added earlier this session will also flag these at runtime, but catching them here is cheaper.

- [ ] **Step 4: Run the full suite and lint**

```bash
PYTHONPATH=. uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

- [ ] **Step 5: Commit**

```bash
git add config/sources.yaml config/stack/sources.yaml
git commit -m "feat(sources): expand the YouTube roster, drop Bloomberg TV clip feed"
```

---

## Deployment

After merge, on the VPS:

```bash
ssh vps 'cd ~/SourceCode/news && git pull && ~/.venvs/news/bin/python -m pip install -q -e . 2>/dev/null; true'
ssh vps 'systemctl --user list-timers --all | grep news-stack'
```

The `transcript_abstract` column is added by `_migrate_db()` on the next pipeline
run, so no manual migration step is required. The first stack run after the Mac
harvester has populated and pushed `transcripts.db` will show a non-zero coverage
figure in the email footer.
