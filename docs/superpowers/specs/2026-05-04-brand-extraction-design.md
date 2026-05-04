# Brand Extraction — Minimum Viable Strip

**Date:** 2026-05-04
**Status:** Approved (pending user spec review)
**Context:** Follow-on to the email-recovery commit (`9bcb268`). The earlier
`dcb4d7c` "genericize for public release" commit only sanitized YAML files.
The Python source and HTML template still hardcode NBG-specific strings
(executive names, competitor mappings, "NBG MONITOR" labels, Greek-language
disambiguation prose, JSON key `nbg_mentions`), all currently public on
GitHub at `weirdapps/news`.

## Goal

Strip every brand-specific literal from tracked source files. The repo
becomes a generic brand-monitoring scaffold; brand identity (names,
leadership, competitors, false-positive filters, display labels) lives
exclusively in the gitignored `config/monitor/keywords.yaml`. Forkers fill
in their own `keywords.yaml` (using `keywords.example.yaml` as a template)
and the system runs.

## Non-goals

- Jinja templating of the synth prompt
- Generalized transliteration rules engine
- Restructuring `roster.py` for non-Greek brands
- README "fork for your brand" guide
- Migrating `nbg:` top-level key in keywords.yaml beyond the rename to `company:`

## Architectural decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Single source of brand truth = `config/monitor/keywords.yaml` (already gitignored) | Avoids new file proliferation. Keywords + identity already conceptually live together. |
| 2 | Pass `keywords_config: dict` as explicit param through `compute_relevance_score`, `build_monitor_prompt`, `render_monitor_html` | YAGNI. No `Brand` class, no module-level lazy load, no caching layer. Tests pass mock dicts. |
| 3 | Section-conditional fail-soft prompt | Forker who only fills `display.full_name` gets a generic-but-working prompt. Forker who fills leadership/competitors/false_positives gets a richer prompt. No hard errors. |
| 4 | Rename JSON contract `nbg_mentions` → `company_mentions` | Internal consistency. The LLM output schema is part of the brand surface — leaving `nbg_mentions` in the prompt re-leaks NBG via the Claude API call. |
| 5 | Local `keywords.yaml` top-level key migrates `nbg:` → `company:` | Already so in `.example` (since `dcb4d7c`). Eliminates a divergence between the gitignored real file and the tracked template. |

## Schema additions to `keywords.yaml`

New top-level `display:` block. Other existing blocks unchanged in shape.

```yaml
display:
  full_name: "National Bank of Greece"   # used in prompt opening, email subject
  short_name: "NBG"                       # used in repeated prompt references, "X MENTIONS" header
  monitor_label: "NBG MONITOR"            # used in HTML banner
  ticker_primary: "ETE.AT"                # used in stock disambiguation prompt section

# Existing blocks (unchanged shape; top-level renamed nbg: → company:):
company:
  names: [...]
  stock_symbols: [...]
  false_positives: [...]
  leadership: [...]
  products: [...]

competitors: { ... }
regulators: [ ... ]
categories: { ... }
display_order: [ ... ]
```

`.example.yaml` mirrors the schema with placeholder values
(`"Your Company Name"`, `"TICKER"`, etc.).

## File-by-file changes

### `news/monitor_synth.py`

Decompose the ~200-line prompt string constant into 5 small builder
functions. Each section returns `""` when its source data is empty.

```python
def _base_prompt(display: dict) -> str: ...
def _disambiguation_section(false_positives: list[str]) -> str: ...
def _name_anchoring_section(leadership: list[dict]) -> str: ...
def _competitor_section(competitors: dict) -> str: ...
def _output_format_section(short_name: str) -> str: ...

def build_monitor_prompt(articles, keywords_config, previous_summary=None):
    display = keywords_config.get("display", {})
    company = keywords_config.get("company", {})
    sections = [
        _base_prompt(display),
        _disambiguation_section(company.get("false_positives", [])),
        _name_anchoring_section(company.get("leadership", [])),
        _competitor_section(keywords_config.get("competitors", {})),
        _output_format_section(display.get("short_name", "the company")),
    ]
    return "".join(s for s in sections if s) + _articles_section(articles, previous_summary)
```

All hardcoded NBG/Greek prose deleted from the module. The Greek-language
false-positive disambiguation lives in the user's `keywords.yaml` data
(already does, in `company.false_positives`), and is rendered into the
prompt at runtime.

JSON output schema in the prompt: `nbg_mentions` → `company_mentions`.

### `news/roster.py`

`EXECUTIVE_ROSTER` constant → `build_roster(keywords_config) -> str` function.
Returns the assembled roster string for inclusion in synthesis prompts.

Generic transliteration *rules* are retained (e.g., "preserve the original
surname suffix when transliterating", "never invent first names"), but all
Greek-specific *examples* (e.g., "Θεοφιλίδη → Theofilidi") and named
executives are deleted from the source. The current Greek-aware behavior is
preserved for the user's local case because their `keywords.yaml.company.leadership`
provides the names + roles.

```python
def build_roster(keywords_config: dict) -> str:
    leadership = keywords_config.get("company", {}).get("leadership", [])
    competitors = keywords_config.get("competitors", {})
    if not leadership and not competitors:
        return ""
    # ... assemble roster table from data
    # Generic transliteration guidance (no language-specific examples).
```

### `news/processor.py`

```python
# Old (lines 99-102):
company_patterns = ["national bank of greece", "nbg", "ethniki trapeza"]
if any(pattern in text for pattern in company_patterns):
    score += scoring.get("company_mention", 0)

# New:
def compute_relevance_score(article, scoring, source_tier, keywords_config=None):
    ...
    if keywords_config:
        company_names = [n.lower() for n in keywords_config.get("company", {}).get("names", [])]
        if any(name in text for name in company_names):
            score += scoring.get("company_mention", 0)
```

`keywords_config` defaults to `None` so the digest pipeline (which doesn't
load keywords) doesn't break. If `None`, the company_mention bonus simply
doesn't apply — which is correct for the digest profile.

### `news/deliver.py`

- `"NBG Monitor"` literal (line ~330) → `keywords_config["display"].get("monitor_label", "Brand Monitor")`
- `nbg_mentions = synthesis.get("nbg_mentions", [])` → `company_mentions = synthesis.get("company_mentions", [])`
- Template variable rename in `render_monitor_html` from `nbg_mentions=` to `company_mentions=`
- Pass `display=keywords_config.get("display", {})` to template context

### `templates/monitor.html`

| Old | New |
|-----|-----|
| `NBG MONITOR` (banner) | `{{ display.monitor_label }}` |
| `<!-- NBG Mentions -->` | `<!-- Company Mentions -->` |
| `{% if nbg_mentions %}` | `{% if company_mentions %}` |
| `NBG MENTIONS` (header) | `{{ display.short_name }} MENTIONS` |
| `{% for mention in nbg_mentions %}` | `{% for mention in company_mentions %}` |

### `news/mcp_server.py`

Three docstring NBG references at lines 21, 25, 85 → "brand monitoring".
No config read needed; docstrings are static.

### `news/config.py`

Two docstring NBG references at lines 4, 101 → generic.

### `main.py`

`keywords_config` is already loaded at line 486. Thread it into the monitor
pipeline call sites. New signatures (default values keep digest pipeline
backward-compatible — digest doesn't load keywords):

- `build_monitor_prompt(articles, keywords_config: dict, previous_summary=None)` — required dict
- `compute_relevance_score(article, scoring, source_tier, keywords_config: dict | None = None)` — optional, used only by monitor profile
- `render_monitor_html(synthesis, schedule_text, keywords_config: dict)` — required dict
- `build_roster(keywords_config: dict) -> str` — required dict, returns "" if empty

### Tests

| Test | Change |
|------|--------|
| `tests/test_monitor.py:67` | `keywords["nbg"]` → `keywords["company"]` |
| `tests/test_monitor.py:71-74` | Same |
| `tests/test_monitor.py:287, 330` | `nbg_mentions` → `company_mentions` |
| `tests/test_processor.py:127, 141` | Already updated (renamed in earlier commit) |
| New: `tests/test_monitor.py` | Add tests for section-conditional prompt builders: empty `false_positives` → no disambiguation section; empty `leadership` → no roster section; empty `competitors` → no competitor section. |

### Local `config/monitor/keywords.yaml` migration

One-time edit to the gitignored local file:
1. Rename top-level `nbg:` → `company:`
2. Add `display:` block at top with NBG-specific values

This is documented in the spec but executed manually — not part of the
tracked code changes.

## Testing strategy

1. Unit tests for each section builder (empty input → empty string; populated input → expected substring)
2. Existing `test_build_monitor_prompt_*` tests updated to pass `keywords_config` mock dict
3. Integration: full pytest run with both the user's real `keywords.yaml` (full NBG data → all sections present) and a minimal-fixture `keywords.yaml` (only `display.full_name` → only base prompt section present)
4. Sanity check: `grep -i "nbg\|plessas\|mylonas\|theofilidi\|piraeus\|alpha bank\|eurobank\|ethniki" news/*.py templates/*.html main.py` returns ZERO matches in tracked source after the change (only the local untracked `keywords.yaml` should hit)

## Risk and rollback

- **Risk:** Renaming the JSON key changes the LLM contract. The next monitor run after deploy would receive `company_mentions` from Claude only if the prompt is updated synchronously with the consumer. Both live in the same commit — atomic.
- **Risk:** A consumer of `nbg_mentions` outside this repo (e.g., a downstream MCP query) would break. Verified: the news-reader MCP exposes article search, not synthesis JSON, so no external consumer.
- **Rollback:** Single-commit revert restores the pre-extraction state. Local `keywords.yaml` migration is reversible by hand-editing.

## Out-of-band notes

- The hardcoded content has been public since the repo went public (no new exposure from this work — only future cleanliness).
- After this lands, the public repo's `git log` still contains historical NBG content. A `git filter-repo` rewrite would be needed to scrub history. Out of scope for this spec.
