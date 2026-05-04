# Brand Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-05-04-brand-extraction-design.md`

**Goal:** Strip every NBG-specific literal from tracked source files; brand identity reads from the gitignored `config/monitor/keywords.yaml` via threaded `keywords_config` dict.

**Architecture:** Single source of brand truth = `keywords.yaml` extended with a new `display:` block. Prompt becomes section-conditional (sections returning `""` when their data is empty). JSON contract rename `nbg_mentions` → `company_mentions`. No new modules, no `Brand` class — explicit dict threading through 4 call sites.

**Tech Stack:** Python 3.12+, pytest, Jinja2 (templates only), PyYAML.

**Branch policy:** This plan can run on master since email is already flowing on the previous commit. If you want isolation for safer review, `EnterWorktree` first — both are acceptable.

---

## Task 1: Migrate local keywords.yaml + extend keywords.example.yaml schema

**Files:**
- Modify: `config/monitor/keywords.yaml` (gitignored — manual hand-edit)
- Modify: `config/monitor/keywords.example.yaml` (tracked template)

- [ ] **Step 1: Hand-edit local `config/monitor/keywords.yaml`**

The file currently has top-level `nbg:` and no `display:` block. Make two edits:

(a) Add a new `display:` block at the very top (above the existing `nbg:`):

```yaml
display:
  full_name: "National Bank of Greece"
  short_name: "NBG"
  monitor_label: "NBG MONITOR"
  ticker_primary: "ETE.AT"
```

(b) Rename the top-level key `nbg:` → `company:` (the nested fields stay identical):

```yaml
# Before:
nbg:
  names: [...]
  ...

# After:
company:
  names: [...]
  ...
```

- [ ] **Step 2: Update `config/monitor/keywords.example.yaml` to match the new schema**

Add the same `display:` block at the top (with placeholder values). The existing `company:` key already matches.

```yaml
# Add at the top of the file, above 'company:':
display:
  full_name: "Your Company Full Name"
  short_name: "YourCo"
  monitor_label: "BRAND MONITOR"
  ticker_primary: "TICKER"
```

- [ ] **Step 3: Verify both load correctly**

Run:
```bash
.venv/bin/python -c "
from news.config import get_keywords
kw = get_keywords(profile='monitor')
print('display:', kw.get('display'))
print('company keys:', list(kw.get('company', {}).keys()))
print('company.names[0]:', kw['company']['names'][0])
"
```

Expected output (from your local file):
```
display: {'full_name': 'National Bank of Greece', 'short_name': 'NBG', ...}
company keys: ['names', 'stock_symbols', 'false_positives', 'leadership', 'products']
company.names[0]: National Bank of Greece
```

- [ ] **Step 4: Commit (only the .example file — local .yaml is gitignored)**

```bash
git add config/monitor/keywords.example.yaml
git commit -m "feat(config): add display schema to keywords template

Adds display.{full_name,short_name,monitor_label,ticker_primary} to the
public keywords.example.yaml template. Local keywords.yaml has been
hand-migrated to match (rename nbg→company, add display block)."
```

---

## Task 2: Refactor monitor_synth.py to section-builders + rename JSON contract

**Files:**
- Modify: `news/monitor_synth.py`
- Test: `tests/test_monitor.py`

- [ ] **Step 1: Read current monitor_synth.py end to end**

```bash
wc -l news/monitor_synth.py && head -5 news/monitor_synth.py
```

Note the current module shape: a long `MONITOR_SYSTEM_PROMPT` constant, a `build_monitor_prompt()` function, and helpers for fallback/subject/HTML rendering. Plan: extract the prompt-string assembly into 5 small builder functions; pass `keywords_config` into `build_monitor_prompt`.

- [ ] **Step 2: Write failing tests for the new section builders**

Add to `tests/test_monitor.py` (after the existing `test_build_monitor_prompt_*` tests):

```python
from news.monitor_synth import (
    _base_prompt,
    _disambiguation_section,
    _name_anchoring_section,
    _competitor_section,
    _output_format_section,
)


def test_base_prompt_uses_display_full_name():
    out = _base_prompt({"full_name": "Acme Bank", "short_name": "ACME"})
    assert "Acme Bank" in out
    assert "ACME" in out


def test_base_prompt_falls_back_when_display_missing():
    out = _base_prompt({})
    assert "the company" in out  # generic fallback


def test_disambiguation_section_empty_returns_empty_string():
    assert _disambiguation_section([]) == ""


def test_disambiguation_section_lists_false_positives():
    out = _disambiguation_section(["National Team", "National Economy"])
    assert "National Team" in out
    assert "National Economy" in out


def test_name_anchoring_section_empty_returns_empty_string():
    assert _name_anchoring_section([]) == ""


def test_name_anchoring_section_lists_leadership():
    out = _name_anchoring_section([
        {"name_en": "Pavlos Mylonas", "role": "CEO"},
    ])
    assert "Pavlos Mylonas" in out
    assert "CEO" in out


def test_competitor_section_empty_returns_empty_string():
    assert _competitor_section({}) == ""


def test_competitor_section_lists_competitor_names():
    out = _competitor_section({
        "piraeus": {"names": ["Piraeus Bank"]},
        "alpha": {"names": ["Alpha Bank"]},
    })
    assert "Piraeus Bank" in out
    assert "Alpha Bank" in out


def test_output_format_section_uses_short_name():
    out = _output_format_section("ACME")
    assert "ACME" in out
    assert "company_mentions" in out  # JSON key rename
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_monitor.py -v -k "section or base_prompt or output_format" 2>&1 | tail -20
```

Expected: ImportError or AttributeError for the new functions.

- [ ] **Step 4: Replace `news/monitor_synth.py` with the refactored module**

Replace the entire file content with the structure below. Preserve the existing public functions (`build_monitor_prompt`, `build_monitor_fallback`, `build_monitor_subject`, `render_monitor_html`) — only their internals change.

Key changes:
- Delete the `MONITOR_SYSTEM_PROMPT` string constant
- Add 5 underscore-prefixed builders
- `build_monitor_prompt` becomes a composer
- ALL hardcoded NBG/Greek prose deleted
- JSON output schema in the prompt uses `company_mentions` not `nbg_mentions`

```python
"""AI synthesis layer for brand monitoring — Claude CLI."""
# (existing imports preserved)


def _base_prompt(display: dict) -> str:
    full_name = display.get("full_name", "the company")
    short_name = display.get("short_name", full_name)
    return f"""You are a brand intelligence analyst monitoring {full_name} ({short_name}) for a senior executive.

Your job:
1. VERIFY which articles genuinely mention {short_name} (filter false positives)
2. CLASSIFY each genuine mention by category and sentiment
3. FLAG urgent items
"""


def _disambiguation_section(false_positives: list[str]) -> str:
    if not false_positives:
        return ""
    lines = "\n".join(f'  - "{fp}"' for fp in false_positives)
    return f"""
EXCLUDE these false-positive matches (phrases that look like the brand but are not):
{lines}
"""


def _name_anchoring_section(leadership: list[dict]) -> str:
    if not leadership:
        return ""
    lines = []
    for person in leadership:
        name = person.get("name_en") or person.get("name", "")
        role = person.get("role", "")
        if name:
            lines.append(f"  - {name} ({role})")
    if not lines:
        return ""
    return f"""
EXECUTIVE ROSTER (anchor names mentioned in articles to these people):
{chr(10).join(lines)}
"""


def _competitor_section(competitors: dict) -> str:
    lines = []
    for _, comp in competitors.items():
        names = comp.get("names", [])
        if names:
            lines.append(f"  - {names[0]}")
    if not lines:
        return ""
    return f"""
COMPETITORS — track relative positioning where mentioned:
{chr(10).join(lines)}
"""


def _output_format_section(short_name: str) -> str:
    return f"""
Return JSON with this exact shape:

{{
  "company_mentions": [
    {{"summary": "Brief description", "sentiment": "positive|negative|neutral", "urgency": "high|medium|low", "url": "...", "category": "..."}}
  ],
  "competitor_mentions": {{
    "<competitor_key>": "Brief on competitor activity, or null if nothing"
  }},
  "executive_brief": [
    "Bullet 1 — most important {short_name}-related insight",
    "Bullet 2",
    "Bullet 3"
  ],
  "mention_count": <int>
}}

Rules:
- In company_mentions, include both new and important repeat items — mark new items with a "NEW:" prefix in the summary
- If no genuine {short_name} mentions exist, return mention_count: 0 with empty arrays
"""


def build_monitor_prompt(articles, keywords_config: dict, previous_summary=None) -> str:
    display = keywords_config.get("display", {})
    company = keywords_config.get("company", {})
    competitors = keywords_config.get("competitors", {})
    short_name = display.get("short_name", display.get("full_name", "the company"))

    sections = [
        _base_prompt(display),
        _disambiguation_section(company.get("false_positives", [])),
        _name_anchoring_section(company.get("leadership", [])),
        _competitor_section(competitors),
        _output_format_section(short_name),
    ]
    system_prompt = "".join(s for s in sections if s)

    # ... existing logic for assembling article entries + previous_summary context
    # (preserve the existing article-rendering code; only the system_prompt changes)
```

NOTE: Preserve the existing functions `build_monitor_fallback`, `build_monitor_subject`, `render_monitor_html` — they're touched in later tasks but stay structurally similar in this task. If the article-assembly code at the bottom of `build_monitor_prompt` references `MONITOR_SYSTEM_PROMPT`, replace that reference with `system_prompt` from the composition above.

- [ ] **Step 5: Update existing `test_build_monitor_prompt_*` tests to pass `keywords_config`**

Find tests in `tests/test_monitor.py` that call `build_monitor_prompt(...)`. Each needs a `keywords_config` arg now. Use a minimal fixture dict for tests:

```python
_TEST_KEYWORDS = {
    "display": {"full_name": "Test Bank", "short_name": "TST"},
    "company": {"false_positives": [], "leadership": []},
    "competitors": {},
}
```

Update each call site: `build_monitor_prompt(articles)` → `build_monitor_prompt(articles, _TEST_KEYWORDS)`.

For the test `test_build_monitor_prompt_anchors_executive_names` — add the leadership data to a local fixture:
```python
keywords = {**_TEST_KEYWORDS, "company": {"leadership": [{"name_en": "Alice", "role": "CEO"}]}}
prompt = build_monitor_prompt(articles, keywords)
assert "Alice" in prompt
```

- [ ] **Step 6: Run all monitor tests**

```bash
.venv/bin/pytest tests/test_monitor.py -v 2>&1 | tail -40
```

Expected: ALL tests pass.

- [ ] **Step 7: Commit**

```bash
git add news/monitor_synth.py tests/test_monitor.py
git commit -m "refactor(monitor_synth): section-builder prompt + JSON contract rename

- Decompose 200-line prompt constant into 5 underscore-prefixed
  builders; each returns '' when its source data is empty.
- All hardcoded NBG/Greek prose removed.
- Rename JSON output key nbg_mentions → company_mentions.
- build_monitor_prompt() now requires keywords_config dict."
```

---

## Task 3: Refactor roster.py — function-ize from constant

**Files:**
- Modify: `news/roster.py`
- Test: `tests/test_monitor.py`

- [ ] **Step 1: Read current `news/roster.py`**

```bash
cat news/roster.py
```

Note: it's a single string constant `EXECUTIVE_ROSTER` plus transliteration rules in prose. The constant is referenced from `news/monitor_synth.py` (find the exact location).

- [ ] **Step 2: Write failing test for `build_roster`**

Add to `tests/test_monitor.py`:

```python
from news.roster import build_roster


def test_build_roster_empty_returns_empty_string():
    assert build_roster({"company": {"leadership": []}, "competitors": {}}) == ""


def test_build_roster_includes_leadership():
    keywords = {
        "company": {
            "leadership": [
                {"name_en": "Alice Smith", "role": "CEO"},
                {"name_en": "Bob Jones", "role": "COO"},
            ]
        },
        "competitors": {},
    }
    out = build_roster(keywords)
    assert "Alice Smith" in out
    assert "CEO" in out
    assert "Bob Jones" in out


def test_build_roster_includes_competitor_names():
    keywords = {
        "company": {"leadership": []},
        "competitors": {"x": {"names": ["XCorp Bank"]}, "y": {"names": ["YBank"]}},
    }
    out = build_roster(keywords)
    assert "XCorp Bank" in out
    assert "YBank" in out


def test_build_roster_no_specific_examples_in_module():
    """The roster module itself contains no brand-specific examples."""
    import news.roster as roster_mod
    src = open(roster_mod.__file__).read()
    for forbidden in ["Mylonas", "Theofilidi", "Plessas", "Megalou", "Psaltis", "Karavias", "Ethniki"]:
        assert forbidden not in src, f"Found brand-specific literal: {forbidden}"
```

- [ ] **Step 3: Run to verify it fails**

```bash
.venv/bin/pytest tests/test_monitor.py -v -k "build_roster" 2>&1 | tail -10
```

Expected: ImportError on `build_roster`.

- [ ] **Step 4: Replace `news/roster.py`**

```python
"""Executive roster builder for synthesis prompts.

Greek transliteration rules are kept generic (no language-specific examples).
Brand-specific names + roles come from keywords.yaml.company.leadership.
"""


def build_roster(keywords_config: dict) -> str:
    """Build executive roster section for inclusion in synthesis prompts.

    Returns "" when both leadership and competitors are empty (forker with
    minimal config).
    """
    leadership = keywords_config.get("company", {}).get("leadership", [])
    competitors = keywords_config.get("competitors", {})

    if not leadership and not competitors:
        return ""

    parts = []

    if leadership:
        rows = []
        for person in leadership:
            name = person.get("name_en") or person.get("name", "")
            role = person.get("role", "")
            if name:
                rows.append(f"- {name}, {role}")
        if rows:
            parts.append("EXECUTIVE ROSTER:\n" + "\n".join(rows))

    if competitors:
        rows = []
        for _, comp in competitors.items():
            names = comp.get("names", [])
            if names:
                rows.append(f"- {names[0]}")
        if rows:
            parts.append("KEY COMPETITORS:\n" + "\n".join(rows))

    parts.append(_TRANSLITERATION_RULES)
    return "\n\n".join(parts)


_TRANSLITERATION_RULES = """\
TRANSLITERATION + ATTRIBUTION RULES:
1. Use the roster above to disambiguate which person a surname refers to.
2. NEVER invent first names. If an article references only a surname, write only the surname. Use the roster to verify which person the surname refers to, but only add the first name if the article itself includes it.
3. Preserve original-language surname suffixes when transliterating to English.
4. NEVER attribute a quote or position to a person not actually named in the article. If unsure who said something, attribute to the institution.
"""
```

- [ ] **Step 5: Update consumer in `news/monitor_synth.py`**

The previous `EXECUTIVE_ROSTER` import and inline reference must change. Find the import (likely at the top: `from news.roster import EXECUTIVE_ROSTER`) and replace with:

```python
from news.roster import build_roster
```

In `build_monitor_prompt`, append the roster to the assembled sections:

```python
# After the existing section composition:
roster = build_roster(keywords_config)
if roster:
    system_prompt = system_prompt + "\n\n" + roster
```

- [ ] **Step 6: Run roster + monitor tests**

```bash
.venv/bin/pytest tests/test_monitor.py -v 2>&1 | tail -20
```

Expected: ALL pass, including the new `test_build_roster_no_specific_examples_in_module`.

- [ ] **Step 7: Commit**

```bash
git add news/roster.py news/monitor_synth.py tests/test_monitor.py
git commit -m "refactor(roster): function-ize from constant; strip brand examples

EXECUTIVE_ROSTER constant → build_roster(keywords_config) function.
Greek transliteration rules retained as generic English guidance;
specific surnames + Greek examples removed from source."
```

---

## Task 4: Update processor.py — read patterns from keywords_config

**Files:**
- Modify: `news/processor.py`
- Test: `tests/test_processor.py`

- [ ] **Step 1: Write a failing test that the patterns come from config**

Add to `tests/test_processor.py`:

```python
def test_compute_relevance_score_uses_keywords_config_for_company_match():
    """Company-mention bonus is awarded based on keywords_config patterns, not a hardcoded list."""
    article = _make_article(
        title="AcmeCorp Q1 results",
        content="AcmeCorp posted strong results. " * 20,
    )
    scoring = {"company_mention": 50, "category_match": 0, "tier_1_bonus": 0,
               "tier_2_bonus": 0, "tier_3_bonus": 0,
               "recency_1h": 0, "recency_4h": 0, "recency_12h": 0, "recency_24h": 0}
    keywords = {"company": {"names": ["AcmeCorp"]}}

    score = compute_relevance_score(article, scoring, source_tier=2, keywords_config=keywords)
    assert score >= 50  # got the company_mention bonus


def test_compute_relevance_score_no_keywords_config_skips_company_bonus():
    """Without keywords_config (digest profile), the company_mention bonus does not apply."""
    article = _make_article(
        title="National Bank of Greece Q1",
        content="NBG news. " * 20,
    )
    scoring = {"company_mention": 50, "category_match": 0, "tier_1_bonus": 0,
               "tier_2_bonus": 0, "tier_3_bonus": 0,
               "recency_1h": 0, "recency_4h": 0, "recency_12h": 0, "recency_24h": 0}

    score = compute_relevance_score(article, scoring, source_tier=2)  # no keywords_config
    assert score == 0  # no company bonus, no other bonuses applied
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_processor.py -v -k "keywords_config" 2>&1 | tail -10
```

Expected: FAIL — current code has hardcoded patterns that match "national bank of greece" regardless of config.

- [ ] **Step 3: Edit `news/processor.py`**

Find the function signature (around line 85-95):

```python
def compute_relevance_score(article, scoring, source_tier):
```

Change to:

```python
def compute_relevance_score(article, scoring, source_tier, keywords_config: dict | None = None):
```

Find the hardcoded patterns block (around lines 99-102):

```python
# Check for company mentions (NBG patterns are local to the personal config;
# the YAML scoring key uses the generic name `company_mention`).
company_patterns = ["national bank of greece", "nbg", "ethniki trapeza"]
if any(pattern in text for pattern in company_patterns):
    score += scoring.get("company_mention", 0)
```

Replace with:

```python
# Check for company mentions — patterns come from keywords_config.
# When keywords_config is None (digest profile), no company bonus is applied.
if keywords_config:
    company_names = [n.lower() for n in keywords_config.get("company", {}).get("names", [])]
    if any(name in text for name in company_names):
        score += scoring.get("company_mention", 0)
```

- [ ] **Step 4: Update the existing `test_compute_relevance_score` test**

The existing test (line ~115 in tests/test_processor.py) uses an article titled "NBG Quarterly Results" and expects the company bonus. Pass a `keywords_config` to make it work:

Find:
```python
score = compute_relevance_score(article, scoring_config, source_tier=1)
```

Change to:
```python
keywords = {"company": {"names": ["NBG", "National Bank of Greece"]}}
score = compute_relevance_score(article, scoring_config, source_tier=1, keywords_config=keywords)
```

Update the comment one line down to mention the config dependency.

- [ ] **Step 5: Run all processor tests**

```bash
.venv/bin/pytest tests/test_processor.py -v 2>&1 | tail -15
```

Expected: ALL pass.

- [ ] **Step 6: Commit**

```bash
git add news/processor.py tests/test_processor.py
git commit -m "refactor(processor): read company patterns from keywords_config

Hardcoded ['national bank of greece', 'nbg', 'ethniki trapeza'] list
removed. compute_relevance_score now accepts an optional keywords_config
(default None preserves digest-profile behavior — no company bonus
when keywords aren't loaded)."
```

---

## Task 5: Update deliver.py — display label + JSON key + template var renames

**Files:**
- Modify: `news/deliver.py`
- Test: `tests/test_deliver.py` (or `tests/test_monitor.py` — check where deliver tests live)

- [ ] **Step 1: Locate the touchpoints**

```bash
grep -n "nbg\|NBG" news/deliver.py
```

Expected matches at lines 257, 291, 330 (per the spec):
- 257: `nbg_mentions = synthesis.get("nbg_mentions", [])`
- 291: `nbg_mentions=nbg_mentions,` (Jinja context kwarg)
- 330: `label = "NBG Monitor"`

- [ ] **Step 2: Write failing test for the display label coming from config**

Add to `tests/test_deliver.py` (or wherever the deliver tests live — `grep -l "deliver" tests/`):

```python
def test_render_monitor_html_uses_display_label_from_config():
    synthesis = {"company_mentions": [], "executive_brief": [], "mention_count": 0}
    keywords = {"display": {"monitor_label": "ACME WATCH", "short_name": "ACME"}}

    html = render_monitor_html(synthesis, schedule_text="00:00, 09:00", keywords_config=keywords)
    assert "ACME WATCH" in html
    assert "NBG MONITOR" not in html  # confirms no leak


def test_render_monitor_html_falls_back_when_display_missing():
    synthesis = {"company_mentions": [], "executive_brief": [], "mention_count": 0}
    keywords = {}  # forker with no display block

    html = render_monitor_html(synthesis, schedule_text="00:00", keywords_config=keywords)
    assert "BRAND MONITOR" in html  # generic fallback
```

- [ ] **Step 3: Run to verify failure**

```bash
.venv/bin/pytest tests/test_deliver.py -v -k "display_label or render_monitor" 2>&1 | tail -10
```

Expected: TypeError (extra kwarg) or KeyError.

- [ ] **Step 4: Edit `news/deliver.py`**

Three localized edits:

(a) Around line 257 — rename the synthesis getter:
```python
# Before:
nbg_mentions = synthesis.get("nbg_mentions", [])
# After:
company_mentions = synthesis.get("company_mentions", [])
```

(b) Around line 291 — update the Jinja context kwarg name:
```python
# Before:
nbg_mentions=nbg_mentions,
# After:
company_mentions=company_mentions,
```

Add to the same context kwargs:
```python
display=keywords_config.get("display", {}),
```

(c) Around line 330 — replace the hardcoded label:
```python
# Before:
label = "NBG Monitor"
# After:
display = keywords_config.get("display", {})
label = display.get("monitor_label", "BRAND MONITOR")
```

(d) Update the `render_monitor_html` signature to accept `keywords_config`:
```python
# Before:
def render_monitor_html(synthesis, schedule_text):
# After:
def render_monitor_html(synthesis, schedule_text, keywords_config: dict):
```

- [ ] **Step 5: Run deliver tests**

```bash
.venv/bin/pytest tests/test_deliver.py -v 2>&1 | tail -20
```

Expected: ALL pass. If old tests fail because they don't pass `keywords_config`, update them to pass an empty dict `{}` (which exercises the fallback path).

- [ ] **Step 6: Commit**

```bash
git add news/deliver.py tests/test_deliver.py
git commit -m "refactor(deliver): brand label + JSON key from keywords_config

- 'NBG Monitor' label literal → keywords.display.monitor_label
- nbg_mentions JSON key → company_mentions
- render_monitor_html signature gains keywords_config parameter
- display block passed to template context for use in monitor.html"
```

---

## Task 6: Update templates/monitor.html — template var renames

**Files:**
- Modify: `templates/monitor.html`

- [ ] **Step 1: Locate the brand-bound strings**

```bash
grep -n "NBG\|nbg" templates/monitor.html
```

Expected at lines ~28 (banner), ~129 (comment), ~130 (`{% if nbg_mentions %}`), ~136 (header), ~139 (`{% for mention in nbg_mentions %}`).

- [ ] **Step 2: Apply the renames in `templates/monitor.html`**

| Line | Old | New |
|------|-----|-----|
| ~28 (banner) | `NBG MONITOR` | `{{ display.monitor_label }}` |
| ~129 (comment) | `<!-- NBG Mentions -->` | `<!-- Company Mentions -->` |
| ~130 | `{% if nbg_mentions %}` | `{% if company_mentions %}` |
| ~136 (header) | `NBG MENTIONS` | `{{ display.short_name }} MENTIONS` |
| ~139 | `{% for mention in nbg_mentions %}` | `{% for mention in company_mentions %}` |

- [ ] **Step 3: Verify no NBG strings remain**

```bash
grep -in "nbg" templates/monitor.html
```

Expected: NO output.

- [ ] **Step 4: Run a render integration test**

```bash
.venv/bin/pytest tests/test_deliver.py -v -k "render_monitor" 2>&1 | tail -10
```

Expected: ALL pass.

- [ ] **Step 5: Commit**

```bash
git add templates/monitor.html
git commit -m "refactor(template): brand-aware monitor.html

- 'NBG MONITOR' / 'NBG MENTIONS' literals → display.monitor_label / display.short_name
- nbg_mentions Jinja loop → company_mentions"
```

---

## Task 7: Thread keywords_config through main.py

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Read the monitor pipeline section of main.py**

```bash
sed -n '475,560p' main.py
```

Note: `keywords_config = get_keywords(profile="monitor")` is already loaded around line 486. Find every call site downstream that needs it.

- [ ] **Step 2: Update each call site**

Search for the call sites:
```bash
grep -n "build_monitor_prompt\|render_monitor_html\|compute_relevance_score" main.py
```

For each:

(a) `build_monitor_prompt(articles, previous_summary)` → `build_monitor_prompt(articles, keywords_config, previous_summary)`

(b) `render_monitor_html(synthesis, schedule_text)` → `render_monitor_html(synthesis, schedule_text, keywords_config)`

(c) `compute_relevance_score(article, scoring, source_tier)` calls inside the monitor pipeline → add `keywords_config=keywords_config` kwarg.

(d) `compute_relevance_score` calls inside the digest pipeline → leave unchanged (default `None`).

- [ ] **Step 3: Verify the digest pipeline still works (no regression)**

```bash
.venv/bin/pytest tests/test_processor.py tests/test_orchestrator.py -v 2>&1 | tail -15
```

Expected: ALL pass.

- [ ] **Step 4: Run full test suite**

```bash
.venv/bin/pytest 2>&1 | tail -10
```

Expected: 131+ tests pass.

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "feat(main): thread keywords_config through monitor pipeline

Wires keywords_config (already loaded at main.py:486) into:
- build_monitor_prompt
- render_monitor_html
- compute_relevance_score (monitor calls only; digest unchanged)"
```

---

## Task 8: Sanitize docstrings — mcp_server.py + config.py

**Files:**
- Modify: `news/mcp_server.py`
- Modify: `news/config.py`

- [ ] **Step 1: Locate NBG strings in both files**

```bash
grep -n "NBG\|nbg" news/mcp_server.py news/config.py
```

Expected: ~5 docstring matches (no functional code).

- [ ] **Step 2: Edit `news/mcp_server.py`**

Replace docstring snippets:

| Line | Old | New |
|------|-----|-----|
| ~21 | `"News intelligence platform — search articles from digest and NBG monitor "` | `"News intelligence platform — search articles from digest and brand monitor "` |
| ~25 | `"plus a 00:00 catch-up for NBG brand mentions."` | `"plus a 00:00 catch-up for brand mentions."` |
| ~85 | `pipeline: 'digest' for news digests, 'monitor' for NBG brand monitoring (default: digest)` | `pipeline: 'digest' for news digests, 'monitor' for brand monitoring (default: digest)` |

- [ ] **Step 3: Edit `news/config.py` docstrings**

| Line | Old | New |
|------|-----|-----|
| ~4 | `'monitor' for NBG brand monitoring). Each profile has its own config directory:` | `'monitor' for brand monitoring). Each profile has its own config directory:` |
| ~101 | `Only used by the monitor profile. Contains NBG name variants,` | `Only used by the monitor profile. Contains brand name variants,` |
| ~102 (continuation) | `competitor names, key people, etc.` | `competitor names, key people, etc.` (no change unless still NBG-tagged) |

- [ ] **Step 4: Verify no NBG strings remain in tracked python**

```bash
grep -in "nbg\|plessas\|mylonas\|theofilidi\|ethniki\|piraeus\|alpha bank\|eurobank\|megalou\|psaltis\|karavias" news/*.py main.py templates/*.html
```

Expected: NO output. (The local untracked `keywords.yaml` will still contain these — that's correct.)

- [ ] **Step 5: Run full test suite once more**

```bash
.venv/bin/pytest 2>&1 | tail -5
```

Expected: 131+ pass.

- [ ] **Step 6: Commit**

```bash
git add news/mcp_server.py news/config.py
git commit -m "docs: strip NBG references from mcp_server + config docstrings"
```

---

## Task 9: End-to-end verification + push

**Files:** none modified — verification only.

- [ ] **Step 1: Final repo-wide brand-leak scan**

```bash
git ls-files | xargs grep -in "plessas\|mylonas\|theofilidi\|ethniki trapeza\|megalou\|psaltis\|karavias" 2>/dev/null
```

Expected: NO output from tracked files. (Hits in `.env`, `config/monitor/keywords.yaml`, `config/monitor/sources.yaml` are gitignored and won't appear.)

- [ ] **Step 2: Dry-run a monitor synthesis (no email send)**

```bash
.venv/bin/python -c "
from news.config import get_keywords, get_sources, get_settings
from news.monitor_synth import build_monitor_prompt
kw = get_keywords(profile='monitor')
prompt = build_monitor_prompt(articles=[], keywords_config=kw)
print('Prompt length:', len(prompt), 'chars')
print('---FIRST 800 CHARS---')
print(prompt[:800])
print('---HAS company_mentions:', 'company_mentions' in prompt)
print('---HAS nbg_mentions (should be False):', 'nbg_mentions' in prompt)
"
```

Expected: prompt opens with "monitoring National Bank of Greece (NBG)" (because YOUR local keywords.yaml has those values), `company_mentions` present, `nbg_mentions` absent.

- [ ] **Step 3: Run full test suite one last time**

```bash
.venv/bin/pytest 2>&1 | tail -5
```

Expected: 131+ pass.

- [ ] **Step 4: Push**

```bash
git push origin master 2>&1 | tail -5
```

Expected: commits land on `weirdapps/news`.

- [ ] **Step 5: Confirm to user**

Report back with:
- # commits pushed
- Final test count
- Any anomalies encountered

---

## Self-Review Notes

Spec coverage: ✅ all 5 architectural decisions land in concrete tasks (Task 1: keywords schema; Task 2: section builder + JSON rename; Task 4: optional keywords_config preserves digest behavior; Task 5: display label; Task 6 + spec: nbg→company top-level rename in local file).

Type consistency: `keywords_config: dict` everywhere required, `dict | None = None` only on `compute_relevance_score`. `build_roster`, `build_monitor_prompt`, `render_monitor_html` all expect dict.

Placeholder check: no TBDs. Each step has executable command or code block.
