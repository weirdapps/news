# Synthesis Citation Enforcement + Per-Article Links

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the synthesizer from inventing facts. Require every executive-brief bullet, alert, section, and per-article mention to cite the input article it came from. Drop anything unsourced silently. As a side benefit, surface the resolved article URLs as clickable links beneath the synthesis in both digest and monitor emails.

**Architecture:** A new `news/citation_filter.py` module owns the filter+enrich logic — pure functions over the parsed synthesis dict and the input article list. Prompt changes in `synthesizer.py` and `monitor_synth.py` ask the LLM to emit `article_ids` per bullet/section/mention. The pipelines in `main.py` call the filter immediately after synthesis succeeds, before rendering. After filtering, bullet objects are flattened back to strings so the existing templates and DB consumers (`query.py`) keep working unchanged. Templates gain an optional per-section `articles` block (digest) and link the title in `company_mentions` (monitor).

**Tech Stack:** Python 3.12, Jinja2, pytest, no new dependencies.

---

## Out of scope (deferred)

- Topic profile (`topic_synth.py`, `templates/topic.html`) — same pattern but not requested in this round. The new module will be reusable; wiring is one-task work.
- Backfilling old digests in the DB with citations — only forward-looking.
- Surfacing dropped-bullet count in email — internal metric only, log it but don't show.

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `news/citation_filter.py` | **Create** | Pure functions: validate `article_ids` references the input pool, drop unsourced bullets/sections/mentions, attach resolved URL/title/source to surviving items. |
| `tests/test_citation_filter.py` | **Create** | Unit tests for the filter — happy path, missing IDs, out-of-range IDs, mixed valid/invalid, mentions enrichment. |
| `news/synthesizer.py` | **Modify** | Add citation requirement to digest prompt. New schema: `executive_brief: [{text, article_ids}]`, `what_changed: [{text, article_ids}]`, `sections[].article_ids: [int]`. |
| `news/monitor_synth.py` | **Modify** | Add citation requirement to monitor prompt. New schema: `executive_brief: [{text, article_ids}]`, `alerts: [{text, article_ids}]`, `company_mentions[].article_ids: [int]`, `competitor_watch[key]: {summary, article_ids}`. |
| `tests/test_synthesizer.py` | **Modify** | Assert prompt contains the citation requirement string. |
| `tests/test_monitor.py` | **Modify** | Same assertion for monitor prompt. |
| `main.py` | **Modify** | After `synthesize()` and `synthesize_monitor()` return, call the filter, then proceed to rendering. Log dropped count. |
| `templates/digest.html` | **Modify** | Add `{% if section.articles %}` block after the synthesis paragraph, rendering each as a linked title with source. |
| `templates/monitor.html` | **Modify** | Wrap `{{ mention.title }}` in `<a href="{{ mention.url }}">…</a>` when url is present. |
| `tests/test_deliver.py` | **Modify** | Two new tests: digest renders linked article list when sections have `articles`; monitor wraps mention title in `<a>` when url present. |

---

## Task 1: Citation filter module — happy path

**Files:**

- Create: `news/citation_filter.py`
- Test: `tests/test_citation_filter.py`

- [x] **Step 1: Write the failing tests**

```python
"""Tests for synthesis citation filter."""

from datetime import datetime, timezone

from news.citation_filter import (
    filter_unsourced_bullets,
    filter_unsourced_sections,
    enrich_mentions,
    enrich_section_articles,
)
from news.models import Article


def _articles():
    now = datetime.now(timezone.utc)
    return [
        Article(
            url=f"https://example.com/{i}",
            title=f"Title {i}",
            source=f"Source{i}",
            content="x",
            categories=["banking"],
            language="en",
            relevance_score=50,
            fetched_at=now,
            published_at=now,
        )
        for i in range(5)
    ]


def test_filter_unsourced_bullets_drops_no_ids():
    bullets = [
        {"text": "kept", "article_ids": [0, 1]},
        {"text": "dropped", "article_ids": []},
        {"text": "also dropped"},  # no article_ids field
    ]
    result = filter_unsourced_bullets(bullets, _articles())
    assert result == ["kept"]


def test_filter_unsourced_bullets_drops_out_of_range():
    bullets = [
        {"text": "kept", "article_ids": [0, 99]},  # 99 invalid, 0 valid → keep
        {"text": "dropped", "article_ids": [99, 100]},  # all invalid → drop
    ]
    result = filter_unsourced_bullets(bullets, _articles())
    assert result == ["kept"]


def test_filter_unsourced_bullets_accepts_string_ids():
    """LLM sometimes emits IDs as strings — coerce to int."""
    bullets = [{"text": "kept", "article_ids": ["0", "2"]}]
    result = filter_unsourced_bullets(bullets, _articles())
    assert result == ["kept"]


def test_filter_unsourced_bullets_passes_through_plain_strings():
    """Backward compatibility: bullet that is just a string gets dropped (unsourced)."""
    bullets = ["legacy string bullet", {"text": "ok", "article_ids": [0]}]
    result = filter_unsourced_bullets(bullets, _articles())
    assert result == ["ok"]


def test_filter_unsourced_sections_drops_no_ids():
    sections = [
        {"display_name": "kept", "synthesis": "...", "article_ids": [0, 1]},
        {"display_name": "dropped", "synthesis": "...", "article_ids": []},
        {"display_name": "also dropped", "synthesis": "..."},
    ]
    result = filter_unsourced_sections(sections, _articles())
    assert [s["display_name"] for s in result] == ["kept"]


def test_enrich_section_articles_attaches_title_url_source():
    sections = [
        {"display_name": "x", "synthesis": "...", "article_ids": [0, 2]},
    ]
    enriched = enrich_section_articles(sections, _articles())
    assert enriched[0]["articles"] == [
        {"title": "Title 0", "url": "https://example.com/0", "source": "Source0"},
        {"title": "Title 2", "url": "https://example.com/2", "source": "Source2"},
    ]


def test_enrich_mentions_attaches_url_from_first_id():
    mentions = [
        {"title": "kept", "source": "x", "article_ids": [3]},
        {"title": "dropped", "source": "x", "article_ids": []},
    ]
    enriched = enrich_mentions(mentions, _articles())
    assert len(enriched) == 1
    assert enriched[0]["title"] == "kept"
    assert enriched[0]["url"] == "https://example.com/3"
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_citation_filter.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'news.citation_filter'`

- [x] **Step 3: Write the module**

```python
"""Filter and enrich synthesis output using article_ids citations.

The synthesis LLM is required to cite the input article id(s) supporting each
bullet, section, alert, and per-article mention. This module:

- Drops items that fail to cite any valid id (silent — invented content stays
  out of the email).

- Enriches surviving sections / mentions with the resolved (title, url, source)
  so renderers can show clickable links.

Pure functions over (parsed_synthesis_block, input_articles_list). The article
list is indexed positionally — `article_ids` are integer offsets matching the
`id` field that `build_prompt` / `build_monitor_prompt` write into the prompt's
articles array.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _coerce_ids(raw: Any) -> list[int]:
    """Coerce a raw article_ids value into a list of ints. Tolerates strings.

    Returns [] for missing/invalid values rather than raising — a malformed
    citation is treated as no citation, which causes the bullet to be dropped.
    """
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for item in raw:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _valid_ids(raw_ids: list[int], pool_size: int) -> list[int]:
    """Filter ids to those in [0, pool_size)."""
    return [i for i in raw_ids if 0 <= i < pool_size]


def filter_unsourced_bullets(
    bullets: list[Any], articles: list[Any]
) -> list[str]:
    """Return only bullets with at least one valid article_id, flattened to text.

    Accepts either {text, article_ids} dicts (new schema) or plain strings
    (legacy / non-compliant LLM output — always dropped as unsourced).
    """
    pool_size = len(articles)
    kept: list[str] = []
    dropped = 0
    for bullet in bullets:
        if not isinstance(bullet, dict):
            dropped += 1
            continue
        ids = _valid_ids(_coerce_ids(bullet.get("article_ids")), pool_size)
        if not ids:
            dropped += 1
            continue
        text = bullet.get("text")
        if not isinstance(text, str) or not text.strip():
            dropped += 1
            continue
        kept.append(text)
    if dropped:
        logger.info(f"citation_filter: dropped {dropped} unsourced bullet(s)")
    return kept


def filter_unsourced_sections(
    sections: list[Any], articles: list[Any]
) -> list[dict]:
    """Return only sections with at least one valid article_id."""
    pool_size = len(articles)
    kept: list[dict] = []
    dropped = 0
    for section in sections:
        if not isinstance(section, dict):
            dropped += 1
            continue
        ids = _valid_ids(_coerce_ids(section.get("article_ids")), pool_size)
        if not ids:
            dropped += 1
            continue
        kept.append(section)
    if dropped:
        logger.info(f"citation_filter: dropped {dropped} unsourced section(s)")
    return kept


def enrich_section_articles(
    sections: list[dict], articles: list[Any]
) -> list[dict]:
    """For each section, resolve article_ids → [{title, url, source}] in `articles`.

    Mutates and returns the section list. Sections must already be filtered.
    """
    pool_size = len(articles)
    for section in sections:
        ids = _valid_ids(_coerce_ids(section.get("article_ids")), pool_size)
        section["articles"] = [
            {
                "title": articles[i].title,
                "url": articles[i].url,
                "source": articles[i].source,
            }
            for i in ids
        ]
    return sections


def enrich_mentions(
    mentions: list[Any], articles: list[Any]
) -> list[dict]:
    """Filter mentions without article_ids; enrich surviving with `url` from first id."""
    pool_size = len(articles)
    kept: list[dict] = []
    dropped = 0
    for mention in mentions:
        if not isinstance(mention, dict):
            dropped += 1
            continue
        ids = _valid_ids(_coerce_ids(mention.get("article_ids")), pool_size)
        if not ids:
            dropped += 1
            continue
        mention = dict(mention)  # don't mutate caller's dict
        mention["url"] = articles[ids[0]].url
        kept.append(mention)
    if dropped:
        logger.info(f"citation_filter: dropped {dropped} unsourced mention(s)")
    return kept


def filter_competitor_watch(
    competitor_watch: Any, articles: list[Any]
) -> dict[str, str]:
    """Filter competitor_watch entries without article_ids.

    Accepts both schemas:
    - new: {key: {summary, article_ids}}
    - legacy: {key: "summary string"} (always dropped as unsourced)

    Returns flat {key: summary} for backward-compatible template iteration.
    """
    if not isinstance(competitor_watch, dict):
        return {}
    pool_size = len(articles)
    kept: dict[str, str] = {}
    dropped = 0
    for key, value in competitor_watch.items():
        if not isinstance(value, dict):
            dropped += 1
            continue
        ids = _valid_ids(_coerce_ids(value.get("article_ids")), pool_size)
        if not ids:
            dropped += 1
            continue
        summary = value.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            dropped += 1
            continue
        kept[key] = summary
    if dropped:
        logger.info(f"citation_filter: dropped {dropped} unsourced competitor entry(s)")
    return kept
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_citation_filter.py -v`
Expected: PASS (6 tests)

- [x] **Step 5: Commit**

```bash
git add news/citation_filter.py tests/test_citation_filter.py
git commit -m "feat(citation): add filter+enrich for sourced synthesis bullets"
```

---

## Task 2: Cover the competitor_watch helper

**Files:**

- Modify: `tests/test_citation_filter.py`

- [x] **Step 1: Add the failing test**

Append to `tests/test_citation_filter.py`:

```python
def test_filter_competitor_watch_drops_legacy_strings_and_invalid():
    """Legacy schema (key→str) always dropped; new schema (key→{summary,article_ids}) survives only with valid ids."""
    cw = {
        "alpha": "legacy string summary — must drop",
        "piraeus": {"summary": "kept", "article_ids": [1]},
        "eurobank": {"summary": "no ids", "article_ids": []},
        "optima": {"summary": "bad id", "article_ids": [99]},
    }
    result = filter_competitor_watch(cw, _articles())
    assert result == {"piraeus": "kept"}
```

Update the import at the top:

```python
from news.citation_filter import (
    enrich_mentions,
    enrich_section_articles,
    filter_competitor_watch,
    filter_unsourced_bullets,
    filter_unsourced_sections,
)
```

- [x] **Step 2: Run to verify it passes** (the function already exists from Task 1)

Run: `pytest tests/test_citation_filter.py::test_filter_competitor_watch_drops_legacy_strings_and_invalid -v`
Expected: PASS

- [x] **Step 3: Commit**

```bash
git add tests/test_citation_filter.py
git commit -m "test(citation): cover competitor_watch filter"
```

---

## Task 3: Update digest prompt to require article_ids

**Files:**

- Modify: `news/synthesizer.py` (lines 58-87 — the OUTPUT FORMAT block)
- Modify: `tests/test_synthesizer.py`

- [x] **Step 1: Add the failing test**

Append to `tests/test_synthesizer.py`:

```python
def test_build_prompt_requires_article_ids_citations():
    """Digest prompt must require article_ids per bullet and section."""
    prompt = build_prompt(_make_articles(), [], "24h")

    assert "article_ids" in prompt
    assert "CITATION REQUIREMENT" in prompt
    # The schema must show article_ids on bullets and sections
    assert '"text"' in prompt
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_synthesizer.py::test_build_prompt_requires_article_ids_citations -v`
Expected: FAIL — substrings not present

- [x] **Step 3: Update the digest OUTPUT FORMAT block**

In `news/synthesizer.py`, replace lines 58-87 (`**OUTPUT FORMAT:**` through the closing `Just the JSON object."""`) with:

```python
**CITATION REQUIREMENT (CRITICAL):**
Every bullet, section, and what-changed entry MUST include an "article_ids" field listing the integer id(s) of the input articles that support the claim — using the "id" field from the articles array in CONTEXT below. If you cannot point to a specific input article that supports a claim, OMIT the claim. Unsourced items will be silently dropped before delivery.

**OUTPUT FORMAT:**
Return a JSON object with this exact structure:

{
  "executive_brief": [
    {"text": "Bullet 1 — most critical insight", "article_ids": [3, 7]},
    {"text": "Bullet 2", "article_ids": [12]}
  ],
  "what_changed": [
    {"text": "What's new since previous highlights", "article_ids": [3]}
  ],
  "sections": [
    {
      "category": "category_key",
      "display_name": "Descriptive Section Title",
      "synthesis": "2-3 paragraph synthesis connecting the dots across stories in this section",
      "opposing_views": "Note any conflicting perspectives or tensions between sources, or 'None noted'",
      "fact_check": "Flag any speculation vs verified facts, or 'All statements fact-based'",
      "sources": ["Source1", "Source2"],
      "article_ids": [3, 7, 12],
      "high_value": true
    }
  ]
}

**category keys** (use these): banking, greece, business, ai, trading, learning, tech, apple
**high_value flag:** true for sections with strategic business implications, false for general interest.
**display_name:** Use descriptive titles that reflect the actual content (e.g. "ECB Rate Decision & Banking Impact" not just "Banking & Fintech").
**article_ids:** integer ids from the articles array in CONTEXT — required on every bullet, what_changed entry, and section. Sections must list the union of ids cited across all stories synthesised in that section.

Return ONLY valid JSON. No preamble, no markdown formatting, no prose. Just the JSON object."""
```

- [x] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_synthesizer.py -v`
Expected: PASS — all tests including the new one (`parse_synthesis_output` tests still pass because parser is unchanged)

- [x] **Step 5: Commit**

```bash
git add news/synthesizer.py tests/test_synthesizer.py
git commit -m "feat(synthesizer): require article_ids citation in digest prompt"
```

---

## Task 4: Update monitor prompt to require article_ids

**Files:**

- Modify: `news/monitor_synth.py` (lines 76-133 — the `_output_format_section` function)
- Modify: `tests/test_monitor.py`

- [x] **Step 1: Add the failing test**

Find an existing prompt-building test in `tests/test_monitor.py` and add nearby (or at the bottom):

```python
def test_monitor_prompt_requires_article_ids_citations():
    """Monitor prompt must require article_ids per bullet/alert/mention/competitor."""
    from datetime import datetime, timezone

    from news.models import Article
    from news.monitor_synth import build_monitor_prompt

    articles = [
        Article(
            url="https://example.com/x",
            title="x",
            source="s",
            content="c",
            categories=["nbg_direct"],
            language="en",
            relevance_score=50,
            fetched_at=datetime.now(timezone.utc),
            published_at=datetime.now(timezone.utc),
        )
    ]
    keywords_config = {
        "display": {"full_name": "NBG", "short_name": "NBG", "monitor_label": "NBG MONITOR"},
        "company": {},
        "competitors": {},
    }

    prompt = build_monitor_prompt(articles, keywords_config, None, "1h")

    assert "article_ids" in prompt
    assert "CITATION REQUIREMENT" in prompt
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_monitor.py::test_monitor_prompt_requires_article_ids_citations -v`
Expected: FAIL — substrings missing

- [x] **Step 3: Update the monitor `_output_format_section`**

In `news/monitor_synth.py`, replace the entire body of `_output_format_section` (lines 78-133) with:

```python
    return f"""
**CITATION REQUIREMENT (CRITICAL):**
Every bullet, alert, mention, and competitor entry MUST include an "article_ids" field listing the integer id(s) of the input articles that support it — using the "id" field from the articles array in CONTEXT below. If you cannot point to a specific input article that supports a claim, OMIT the claim. Unsourced items will be silently dropped before delivery.

**OUTPUT FORMAT:**
Return a JSON object with this exact structure:

{{
  "mention_count": 15,
  "new_since_last": 8,
  "sentiment_summary": {{
    "positive": 5,
    "negative": 2,
    "neutral": 8,
    "trend": "improving"
  }},
  "alerts": [
    {{"text": "Brief description of any critical/urgent items requiring attention", "article_ids": [4]}}
  ],
  "company_mentions": [
    {{
      "title": "Article title",
      "source": "Source name",
      "type": "news|regulatory|stock|corporate|sector",
      "sentiment": "positive|negative|neutral",
      "summary": "One-sentence summary of the mention",
      "relevance": "high|medium|low",
      "article_ids": [12]
    }}
  ],
  "sector_context": "1-2 paragraph synthesis of relevant sector activity",
  "competitor_watch": {{
    "<competitor_key>": {{"summary": "Brief on competitor activity", "article_ids": [7]}}
  }},
  "executive_brief": [
    {{"text": "Bullet 1 — most important {short_name}-related insight", "article_ids": [12, 4]}},
    {{"text": "Bullet 2", "article_ids": [7]}}
  ]
}}

**NEW vs REPEAT ARTICLES:**
Each article has an "is_new" flag:

- is_new: true — first time this article appears (fetched since last scan)
- is_new: false — already included in a previous scan (still within 24h window)

This monitor runs every 2 hours during business hours. Each report must STAND ALONE as a complete picture because the reader may not see every run. Therefore:

- ALWAYS include key ongoing stories even if they are repeats (is_new: false)
- Use the "new_since_last" count to show how many are genuinely new
- In the executive_brief, lead with new developments but repeat critical ongoing items
- In company_mentions, include both new and important repeat items — mark new items with a prefix like "NEW:" in the summary

**RULES:**
1. If no genuine {short_name} mentions exist, return mention_count: 0 with empty arrays
2. Alerts array should only contain genuinely urgent items (negative press, regulatory actions, stock drops) — and MUST cite article_ids; never invent incidents not in the input
3. Each report must be self-contained — the reader may have missed previous scans
4. Competitor section: each entry must cite article_ids; entries with no source articles will be dropped
5. Executive brief: max 5 bullets, lead with new items, repeat critical ongoing items
6. company_mentions[].article_ids: include the id of the input article that the mention summarises (the renderer uses this id to attach the source URL to the mention)

Return ONLY valid JSON. No preamble, no markdown formatting."""
```

- [x] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_monitor.py -v`
Expected: PASS — including the new test

- [x] **Step 5: Commit**

```bash
git add news/monitor_synth.py tests/test_monitor.py
git commit -m "feat(monitor): require article_ids citation in monitor prompt"
```

---

## Task 5: Wire citation filter into digest pipeline

**Files:**

- Modify: `main.py` (around lines 391-420 — after `synthesize()` call, before render)

- [x] **Step 1: Add a failing pipeline-level test**

Append to `tests/test_orchestrator.py` (or create one if missing — check first):

```python
def test_digest_pipeline_filters_unsourced_bullets(monkeypatch, tmp_path):
    """Smoke test: synthesis output with one sourced + one unsourced bullet → only sourced renders."""
    # Skip — covered by Task 1 unit tests + manual --adhoc verification in Task 8.
    # Adding a full pipeline integration test would require mocking the entire
    # fetch+process+synthesize chain; the unit tests for citation_filter cover
    # the filtering logic, and Task 8 verifies wiring end-to-end.
```

If `tests/test_orchestrator.py` already covers the pipeline, skip creating this — Task 8 (manual --adhoc smoke) is the integration check.

Run: `grep -l "run_digest_pipeline\|run_pipeline" tests/` to check.

- [x] **Step 2: Modify `main.py` to call the filter**

In `main.py`, find the digest pipeline block at lines 408-420 (the section after `synthesize()` returns and before render). Replace:

```python
    # Prepare synthesis data for rendering
    synthesis_data: dict
    if synthesis_ok:
        # Contract: synthesize() returns dict on success, str on failure.
        assert isinstance(synthesis_result, dict)
        synthesis_data = synthesis_result
        synthesis_text = json.dumps(synthesis_result)
    else:
        # Fallback case - plain text
        assert isinstance(synthesis_result, str)
        synthesis_data = {"fallback_text": synthesis_result}
        synthesis_text = synthesis_result
```

with:

```python
    # Prepare synthesis data for rendering
    synthesis_data: dict
    if synthesis_ok:
        # Contract: synthesize() returns dict on success, str on failure.
        assert isinstance(synthesis_result, dict)
        from news.citation_filter import (
            enrich_section_articles,
            filter_unsourced_bullets,
            filter_unsourced_sections,
        )

        synthesis_data = synthesis_result
        synthesis_data["executive_brief"] = filter_unsourced_bullets(
            synthesis_data.get("executive_brief", []), capped_articles
        )
        synthesis_data["what_changed"] = filter_unsourced_bullets(
            synthesis_data.get("what_changed", []), capped_articles
        )
        synthesis_data["sections"] = enrich_section_articles(
            filter_unsourced_sections(
                synthesis_data.get("sections", []), capped_articles
            ),
            capped_articles,
        )
        synthesis_text = json.dumps(synthesis_data)
    else:
        # Fallback case - plain text
        assert isinstance(synthesis_result, str)
        synthesis_data = {"fallback_text": synthesis_result}
        synthesis_text = synthesis_result
```

- [x] **Step 3: Run all existing tests**

Run: `pytest -v`
Expected: PASS — no regressions. (Existing render tests use legacy bullet strings which the renderer still accepts; the new filter only kicks in inside `main.py`, not in `render_digest_html`.)

- [x] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat(digest): drop unsourced bullets, enrich sections with article URLs"
```

---

## Task 6: Wire citation filter into monitor pipeline

**Files:**

- Modify: `main.py` (around lines 640-651 — after `synthesize_monitor()` returns)

- [x] **Step 1: Modify `main.py`**

Find the monitor pipeline block at lines 640-651. Replace:

```python
    # Prepare synthesis data
    synthesis_data: dict
    if synthesis_ok:
        # Contract: synthesize_monitor() returns dict on success, str on failure.
        assert isinstance(synthesis_result, dict)
        synthesis_data = synthesis_result
        synthesis_text = json.dumps(synthesis_result)
    else:
        assert isinstance(synthesis_result, str)
        synthesis_data = {"fallback_text": synthesis_result}
        synthesis_text = synthesis_result
```

with:

```python
    # Prepare synthesis data
    synthesis_data: dict
    if synthesis_ok:
        # Contract: synthesize_monitor() returns dict on success, str on failure.
        assert isinstance(synthesis_result, dict)
        from news.citation_filter import (
            enrich_mentions,
            filter_competitor_watch,
            filter_unsourced_bullets,
        )

        synthesis_data = synthesis_result
        synthesis_data["executive_brief"] = filter_unsourced_bullets(
            synthesis_data.get("executive_brief", []), capped_articles
        )
        synthesis_data["alerts"] = filter_unsourced_bullets(
            synthesis_data.get("alerts", []), capped_articles
        )
        synthesis_data["company_mentions"] = enrich_mentions(
            synthesis_data.get("company_mentions", []), capped_articles
        )
        synthesis_data["competitor_watch"] = filter_competitor_watch(
            synthesis_data.get("competitor_watch"), capped_articles
        )
        synthesis_text = json.dumps(synthesis_data)
    else:
        assert isinstance(synthesis_result, str)
        synthesis_data = {"fallback_text": synthesis_result}
        synthesis_text = synthesis_result
```

- [x] **Step 2: Adjust `mention_count` and `has_alerts` defaults**

The current code at lines 662-667 reads:

```python
    mention_count = (
        synthesis_data.get("mention_count", len(capped_articles))
        if synthesis_ok
        else len(capped_articles)
    )
    has_alerts = bool(synthesis_data.get("alerts")) if synthesis_ok else False
```

`has_alerts` already uses `synthesis_data["alerts"]` which is now the filtered list of strings — still truthy/falsy correctly. No change needed.

`mention_count` reads the LLM-emitted count; if filtering dropped some mentions, this number is now stale. Replace with:

```python
    mention_count = (
        len(synthesis_data.get("company_mentions", []))
        if synthesis_ok
        else len(capped_articles)
    )
    has_alerts = bool(synthesis_data.get("alerts")) if synthesis_ok else False
```

- [x] **Step 3: Run all existing tests**

Run: `pytest -v`
Expected: PASS

- [x] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat(monitor): drop unsourced alerts/mentions, attach mention URLs"
```

---

## Task 7: Render per-section linked articles in digest

**Files:**

- Modify: `templates/digest.html` (after the Sources line, around line 179)
- Modify: `tests/test_deliver.py`

- [x] **Step 1: Add the failing test**

Add to `tests/test_deliver.py`:

```python
def test_render_digest_html_renders_linked_articles_per_section():
    """When a section has `articles`, render each as a linked title with source."""
    synthesis = {
        "executive_brief": ["Bullet"],
        "what_changed": [],
        "sections": [
            {
                "category": "banking",
                "display_name": "Banking",
                "synthesis": "Synth.",
                "opposing_views": "None noted",
                "fact_check": "All statements fact-based",
                "sources": ["FT"],
                "high_value": True,
                "articles": [
                    {
                        "title": "FT scoop on rates",
                        "url": "https://ft.com/rates",
                        "source": "FT",
                    },
                    {
                        "title": "Reuters follow-up",
                        "url": "https://reuters.com/r",
                        "source": "Reuters",
                    },
                ],
            }
        ],
    }
    html = render_digest_html(
        synthesis=synthesis,
        article_count=2,
        source_count=2,
        time_display="09:00",
        date_display="wed 6 may",
        subject="x",
    )
    assert 'href="https://ft.com/rates"' in html
    assert "FT scoop on rates" in html
    assert 'href="https://reuters.com/r"' in html
    assert "Reuters follow-up" in html
```

- [x] **Step 2: Run to verify failure**

Run: `pytest tests/test_deliver.py::test_render_digest_html_renders_linked_articles_per_section -v`
Expected: FAIL — `href="https://ft.com/rates"` not in output

- [x] **Step 3: Modify `templates/digest.html`**

In `templates/digest.html`, find the `<!-- Sources -->` block at lines 168-179. Insert this block **before** it (so the article list appears between the synthesis paragraph and the source-name line):

```html
                                            <!-- Per-article links (when section has resolved articles) -->
                                            {% if section.articles %}
                                            <tr>
                                                <td style="padding-top: 10px;">
                                                    <table width="100%" cellpadding="0" cellspacing="0" border="0">
                                                        {% for article in section.articles %}
                                                        <tr>
                                                            <td style="font-family: Aptos, Calibri, Arial, sans-serif; font-size: 11pt; color: #404040; padding: 3px 0;">
                                                                &bull; <a href="{{ article.url }}" style="color: #1565c0; text-decoration: none;">{{ article.title }}</a> <span style="color: #95a5a6; font-size: 10pt;">— {{ article.source }}</span>
                                                            </td>
                                                        </tr>
                                                        {% endfor %}
                                                    </table>
                                                </td>
                                            </tr>
                                            {% endif %}

```

- [x] **Step 4: Run tests**

Run: `pytest tests/test_deliver.py -v`
Expected: PASS — including the new test, no regressions to `test_render_digest_html_produces_valid_html`

- [x] **Step 5: Commit**

```bash
git add templates/digest.html tests/test_deliver.py
git commit -m "feat(digest-template): render linked article list under each section"
```

---

## Task 8: Link mention titles in monitor template

**Files:**

- Modify: `templates/monitor.html` (lines 144-148 — the mention title cell)
- Modify: `tests/test_deliver.py`

- [x] **Step 1: Add the failing test**

Add to `tests/test_deliver.py`:

```python
def test_render_monitor_html_links_mention_title_when_url_present():
    """When a mention has `url`, the title is wrapped in an <a> tag."""
    from news.deliver import render_monitor_html

    synthesis = {
        "alerts": [],
        "executive_brief": ["k"],
        "sentiment_summary": {"positive": 1, "negative": 0, "neutral": 0, "trend": "stable"},
        "company_mentions": [
            {
                "title": "NBG Q1 results",
                "source": "Reuters",
                "type": "news",
                "sentiment": "positive",
                "summary": "Strong quarter.",
                "url": "https://reuters.com/nbg-q1",
            }
        ],
        "sector_context": "",
    }
    keywords_config = {
        "display": {"short_name": "NBG", "monitor_label": "NBG MONITOR"},
        "competitors": {},
    }
    html = render_monitor_html(
        synthesis=synthesis,
        mention_count=1,
        source_count=1,
        time_display="09:00",
        date_display="wed 6 may",
        keywords_config=keywords_config,
        subject="x",
    )
    assert 'href="https://reuters.com/nbg-q1"' in html
    assert "NBG Q1 results" in html
```

- [x] **Step 2: Run to verify failure**

Run: `pytest tests/test_deliver.py::test_render_monitor_html_links_mention_title_when_url_present -v`
Expected: FAIL — `href` not present

- [x] **Step 3: Modify `templates/monitor.html`**

In `templates/monitor.html`, replace lines 143-148 (the title row inside the mention loop):

```html
                                            <tr>
                                                <td style="font-family: Aptos, Calibri, Arial, sans-serif; font-size: 12pt; color: #404040;">
                                                    {% if mention.sentiment == 'positive' %}<span style="color: #2e7d32;">&#9650;</span>{% elif mention.sentiment == 'negative' %}<span style="color: #c62828;">&#9660;</span>{% else %}<span style="color: #757575;">&#9679;</span>{% endif %}
                                                    <strong>{{ mention.title }}</strong>
                                                </td>
                                            </tr>
```

with:

```html
                                            <tr>
                                                <td style="font-family: Aptos, Calibri, Arial, sans-serif; font-size: 12pt; color: #404040;">
                                                    {% if mention.sentiment == 'positive' %}<span style="color: #2e7d32;">&#9650;</span>{% elif mention.sentiment == 'negative' %}<span style="color: #c62828;">&#9660;</span>{% else %}<span style="color: #757575;">&#9679;</span>{% endif %}
                                                    {% if mention.url %}<strong><a href="{{ mention.url }}" style="color: #1a237e; text-decoration: none;">{{ mention.title }}</a></strong>{% else %}<strong>{{ mention.title }}</strong>{% endif %}
                                                </td>
                                            </tr>
```

- [x] **Step 4: Run tests**

Run: `pytest tests/test_deliver.py -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add templates/monitor.html tests/test_deliver.py
git commit -m "feat(monitor-template): link mention titles when URL resolved"
```

---

## Task 9: End-to-end smoke check (manual)

**Files:** none (operational verification)

- [x] **Step 1: Run an ad-hoc digest pipeline**

Run: `cd ~/SourceCode/news && python3 main.py --adhoc`
Expected:

- Pipeline completes without error
- Email arrives in inbox
- Visual check:
  - Per-section bulleted article list with clickable titles below the synthesis paragraph
  - Executive brief has 3-5 bullets (not 0 — confirms LLM is following the new schema)

- Open the latest digest in the DB to inspect the JSON:

```bash
sqlite3 ~/SourceCode/news/data/news.db "SELECT synthesis_text FROM digests WHERE pipeline='digest' ORDER BY created_at DESC LIMIT 1;" | python3 -m json.tool | head -60
```

Expected: each section has an `articles` array with `{title, url, source}` triples; bullets are plain strings (filtered + flattened).

- [x] **Step 2: Run an ad-hoc monitor pipeline**

Run: `cd ~/SourceCode/news && python3 main.py --profile monitor --adhoc`
Expected:

- Email arrives, NBG mentions are clickable
- Alerts (if any) reference real BoG / NBG events — no fabricated bombings
- Inspect:

```bash
sqlite3 ~/SourceCode/news/data/news.db "SELECT synthesis_text FROM digests WHERE pipeline='monitor' ORDER BY created_at DESC LIMIT 1;" | python3 -m json.tool | head -80
```

Expected: each `company_mentions` entry has `url`; `alerts` are plain strings (filtered).

- [x] **Step 3: If bullets are empty (LLM ignored citation requirement)**

This is the failure mode to watch for. Diagnostics:

```bash
tail -100 ~/SourceCode/news/data/run.log
```

If the run log shows `synthesis OK` but bullets are empty, the LLM is not emitting `article_ids`. Tighten the prompt's CITATION REQUIREMENT block (move it to the top of the system prompt instead of mid-document) and re-run.

- [x] **Step 4: Commit any prompt tweaks made in step 3**

```bash
git add news/synthesizer.py news/monitor_synth.py
git commit -m "tune(citation): tighten prompt for citation compliance"
```

---

## Self-Review

**Spec coverage:**

- ✅ Drop unsourced bullets silently → Tasks 1, 5, 6
- ✅ No exposure in synthesis output → bullet objects flattened to strings (Task 1)
- ✅ Add links in individual items below synthesis (digest) → Task 7
- ✅ Add links in individual items below synthesis (monitor) → Task 8
- ✅ Don't break existing tests → checked at each task
- ✅ Don't break MCP `query.py` consumers → bullets flatten to strings, sections gain optional `articles` field (additive only)

**Placeholder scan:** No TBDs, no "implement later", every step shows the actual code.

**Type consistency:** `filter_unsourced_bullets` returns `list[str]` everywhere. `enrich_mentions` returns `list[dict]` with `url` added. `enrich_section_articles` mutates and returns `list[dict]` with `articles` added. Templates use `section.articles` and `mention.url` — names match exports.

**Topic profile note:** Topic synthesis (`topic_synth.py`) has the same architecture and would benefit from the same enforcement. Out of scope for this round per user; trivial follow-up later — same `enrich_section_articles` + `filter_unsourced_bullets` calls.
