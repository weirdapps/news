# News Reader

Personal news intelligence platform with three profiles sharing a five-stage pipeline.

## Profiles

| Profile | Cadence | Scope |
|---------|---------|-------|
| `digest` | 5x daily — 00, 09, 13, 17, 21 Athens | Broad: business, AI, tech, Greece, banking |
| `monitor` | Bi-hourly 08–22 Athens + 00:00 catch-up | NBG brand mentions + competitor tracking |
| `topic` | Ad-hoc only (`--query`) | Single subject from a Google News RSS query |

## Quick Reference

```bash
python3 main.py --adhoc                                      # digest (forced run)
python3 main.py --profile monitor --adhoc                    # monitor
python3 main.py --profile topic --query 'ECB rates' --print  # topic brief to stdout
pytest                                                        # 223 tests
bash run_mcp.sh                                              # start MCP server
```

`--scheduled` (default when no flag passed) skips silently if a recent run already happened.

## Pipeline Architecture

```text
fetch → process → store → synthesize → deliver
```

| Module | Stage | Responsibility |
|--------|-------|----------------|
| `news/fetcher.py` | fetch | RSS feeds + optional NewsAPI/WebSearch, concurrent |
| `news/processor.py` | process | Full text via trafilatura, relevance scoring |
| `news/storage.py` | store | SQLite FTS5 (`data/news.db`, gitignored) |
| `news/synthesizer.py` | synthesize (digest) | One-pass curation prompt |
| `news/monitor_synth.py` | synthesize (monitor) | Brand-aware prompt + competitor watch |
| `news/topic_synth.py` | synthesize (topic) | Topic-focused prompt + Google News RSS builder |
| `news/deliver.py` | deliver | Jinja2 HTML → outlook-cli |

LLM calls go through the local `claude` CLI subprocess (Vertex AI, NBG-billed) — see `news/synthesizer.py:143-203`.

## Tech Stack

Python 3.12+, feedparser, httpx, trafilatura, Jinja2, SQLite (FTS5), mcp[cli]>=2, claude CLI.

## Configuration

```text
config/                     # digest profile
  sources.yaml              # RSS feeds, NewsAPI, websearch queries
  categories.yaml           # topic categories + keyword scoring
  settings.yaml             # pipeline settings
  tickers.yaml              # stock-ticker dictionary
config/monitor/             # monitor profile
  sources.yaml              # brand RSS feeds (gitignored — copy from *.example.*)
  keywords.yaml             # brand identity: names, leadership, competitors (gitignored)
  settings.yaml
config/topic/               # topic profile
  settings.yaml
```

`.env` vars: `NEWS_RECIPIENT`, `NEWS_MONITOR_RECIPIENT`, `NEWS_TOPIC_RECIPIENT`, `NEWS_CLAUDE_COMMAND`.

## Testing

All tests use in-memory SQLite and mock all HTTP + `claude` subprocess calls — no real LLM calls.

```bash
pytest -v          # run all 223 tests
ruff check .       # lint (also runs in CI, pinned to ruff 0.16.1)
ruff format .      # format
```

CI: lint → test (GitHub Actions `ci.yml`). SonarCloud on push to master.

## MCP Server

`run_mcp.sh` starts `news/mcp_server.py` — exposes article search, digest history, and stats over FTS5 to Claude Code and other MCP clients.

## VPS Deployment (systemd timers)

All three pipelines run as systemd timers on the Hetzner VPS (167.233.42.38):

| Timer | Schedule | Pipeline |
|-------|----------|----------|
| `news-digest` | 00,09,13,17,21:00 Athens | digest |
| `news-monitor` | 00,08,10,12,14,16,18,20,22:00 Athens | monitor |
| `news-stack` | 13:00 Athens | tech/stack news (topic variant) |

Data volume: `news/data/` → `/mnt/data/news-data/` (news.db ~620 MB).

```bash
ssh vps systemctl status news-digest    # check timer status
ssh vps journalctl -u news-digest -n 50 # recent logs
```

## Known Issues

- **`TimeoutStartSec`**: The digest systemd timer unit must have `TimeoutStartSec=1200` (≥20 min). The digest synthesis step can take 10–15 min for large batches; the default 90 s will kill it mid-run.
