# news — personal news intelligence platform

A forkable Python scaffold that fetches RSS feeds, runs LLM synthesis, and emails curated digests on a schedule. Two profiles ship in the box: a broad multi-topic **digest** and a brand-aware **monitor** with competitor tracking.

## Profiles

| Profile | Cadence | Scope | Output | Config dir |
|---------|---------|-------|--------|------------|
| `digest` | 5x daily | Broad multi-topic news from your RSS list | Curated email briefing | `config/` |
| `monitor` | Bi-hourly during business hours | Mentions of your brand + competitors | Mention-focused email with competitor watch | `config/monitor/` |

Both profiles share the same five-stage pipeline and SQLite store. The `--profile` flag picks the config dir, synthesis prompt, and email template at runtime.

## Quickstart

```bash
# 1. Clone and create a venv
git clone https://github.com/weirdapps/news.git && cd news
python -m venv .venv && source .venv/bin/activate
pip install -e .

# 2. Set up env vars
cp .env.example .env
# edit .env: set NEWS_RECIPIENT (digest) and NEWS_MONITOR_RECIPIENT (monitor)

# 3. (Monitor only) Set up brand identity
cp config/monitor/keywords.example.yaml config/monitor/keywords.yaml
cp config/monitor/sources.example.yaml config/monitor/sources.yaml
# edit both: replace placeholders with your brand identity + RSS feeds

# 4. Verify
pytest                                       # 158 tests should pass
python3 main.py --adhoc                      # ad-hoc digest run
python3 main.py --profile monitor --adhoc    # ad-hoc monitor run

# 5. (Optional, macOS) Schedule via launchd
cp launchd/com.news.digest.example.plist  ~/Library/LaunchAgents/com.news.digest.plist
cp launchd/com.news.monitor.example.plist ~/Library/LaunchAgents/com.news.monitor.plist
# edit both plists: replace /PATH/TO/... placeholders with absolute paths
launchctl load ~/Library/LaunchAgents/com.news.digest.plist
launchctl load ~/Library/LaunchAgents/com.news.monitor.plist
```

`--scheduled` (the default when no flag is passed) silently skips if a recent run already happened and logs to file. `--adhoc` forces a run regardless of schedule — use it for first-run smoke tests.

## Configuration reference

### Environment variables (`.env`)

| Variable | Required | Purpose |
|----------|----------|---------|
| `NEWS_RECIPIENT` | Yes (digest) | Email address that receives digest runs |
| `NEWS_MONITOR_RECIPIENT` | Yes (monitor) | Email address that receives monitor runs |
| `NEWS_CLAUDE_COMMAND` | No | Override the default `claude` CLI binary path |

### YAML files

| File | Purpose | Tracked? |
|------|---------|----------|
| `.env.example` | Env-var template | Yes |
| `.env` | Your actual env values | No |
| `config/settings.yaml` | Digest pipeline settings | Yes |
| `config/sources.yaml` | Digest RSS feeds, NewsAPI, websearch queries | Yes |
| `config/categories.yaml` | Topic categories + keyword scoring | Yes |
| `config/tickers.yaml` | Stock-ticker dictionary for the news tagger | Yes |
| `config/monitor/settings.yaml` | Monitor pipeline settings | Yes |
| `config/monitor/sources.example.yaml` | Monitor RSS feeds template | Yes |
| `config/monitor/sources.yaml` | Your actual monitor feeds | No |
| `config/monitor/keywords.example.yaml` | Brand identity schema (display, company, leadership, competitors) | Yes |
| `config/monitor/keywords.yaml` | Your actual brand identity | No |
| `launchd/com.news.{digest,monitor}.example.plist` | macOS LaunchAgent templates | Yes |
| `launchd/com.news.{digest,monitor}.plist` | Your actual plists | No |

The `*.example.*` files are committed; the real working copies are gitignored. Forkers copy each example, fill in their own values, and the system runs without ever exposing your brand or RSS list to the public repo.

## Architecture

Five-stage pipeline shared by both profiles:

```text
fetch  →  process  →  store  →  synthesize  →  deliver
```

Each stage is a standalone module under `news/`:

| Module | Stage | Responsibility |
|--------|-------|----------------|
| `news/fetcher.py` | fetch | Pulls RSS feeds (and optional NewsAPI / WebSearch) concurrently |
| `news/processor.py` | process | Extracts full article text via trafilatura, scores relevance |
| `news/storage.py` | store | SQLite persistence (`data/news.db`) with FTS5 search |
| `news/synthesizer.py` | synthesize (digest) | One-pass curation prompt for the broad digest |
| `news/monitor_synth.py` | synthesize (monitor) | Brand-aware composition prompt with competitor watch |
| `news/deliver.py` | deliver | Renders Jinja2 HTML and sends via outlook-cli |

The `--profile` flag selects which synth module + email template + config dir to use; everything else is shared.

### Brand-extraction architecture (monitor)

The monitor profile reads its identity from `config/monitor/keywords.yaml` at runtime — nothing brand-specific is hardcoded. Key seams:

- `news/roster.py` — `NAME_HANDLING_RULES` (brand-neutral guidance) plus `build_roster(keywords_config)` (brand-aware roster builder; returns just the rules when called with no args).
- `news/monitor_synth.py` — composes the prompt from four section builders (`_base_prompt`, `_disambiguation_section`, `_competitor_section`, `_output_format_section`). Each returns `""` when its data is empty, so the prompt is fail-soft.
- `news/processor.py compute_relevance_score()` — reads patterns from `keywords_config["company"]["names"]` and `keywords_config["competitors"]` instead of any baked-in lists.
- `templates/monitor.html` — iterates `competitor_watch.items()` and uses `display.monitor_label` / `display.short_name` for labels.

For the full design rationale, see `docs/superpowers/specs/2026-04-05-newsreader-design.md`.

## Email delivery

Sending goes through [`outlook-cli`](https://github.com/weirdapps/outlook-cli) (Microsoft Graph API) using your existing user auth. The integration point is `news/deliver.py:181`.

If you'd rather use a different sender — Gmail API, SMTP, SendGrid, anything that accepts an HTML body — replace `send_email()` in `news/deliver.py`. Templates already produce Outlook-Mac-compatible HTML (table layout, inline CSS, no `<p>` tags), so most clients render cleanly.

## Tests and development

```bash
pytest                # 158 tests in <1s
pre-commit install    # ruff format + mypy + gitleaks
```

Tests use in-memory SQLite, mock all HTTP, and mock the `claude` CLI subprocess — no real LLM calls during testing.

The `pyproject.toml` `[[tool.mypy.overrides]]` block for `scripts.*` is intentional: scripts under `scripts/` are utility one-offs and are excluded from strict type checking.

## MCP server (optional)

`run_mcp.sh` launches the news-reader MCP server defined in `news/mcp_server.py`. It exposes article search, digest history, and stats over FTS5. Only relevant if you run Claude Code (or another MCP client) and want it to query the news store directly.

Add to your Claude Code MCP config:

```json
{
  "mcpServers": {
    "news-reader": {
      "command": "/absolute/path/to/news/run_mcp.sh"
    }
  }
}
```

## Tech stack

Python 3.12+, feedparser, httpx, trafilatura, Jinja2, SQLite (FTS5), the Claude Code CLI, and `outlook-cli` for delivery. macOS LaunchAgents for scheduling (any cron-equivalent works on other platforms — invoke `python3 main.py --scheduled` on the cadence you want).

## Requirements

- macOS for the bundled launchd plists (any scheduler works elsewhere)
- Python 3.12+
- `claude` CLI on `PATH` (or set `NEWS_CLAUDE_COMMAND`)
- `outlook-cli` installed and authenticated, or a drop-in replacement
- Vertex AI / cloud auth as required by your `claude` CLI setup (see `news/auth.py`)

## License

MIT.
