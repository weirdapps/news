# news (newsreader)

Personal news intelligence platform with AI synthesis. Pulls articles from configured RSS feeds, extracts full article text, runs LLM synthesis to produce a curated daily briefing scoped to the user's interests (banking, finance, AI), and emails the digest.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Run

```bash
# One-off daily digest
python -m news.synthesizer

# Real-time monitor (brand monitoring + competitor mentions)
python -m news.monitor_synth
```

LaunchAgents in `launchd/` schedule both:
- `com.news.digest.plist` — runs `synthesizer` once per morning
- `com.news.monitor.plist` — runs `monitor_synth` periodically (configurable cadence)

## Configuration

`config/` holds:
- Feed list (`feeds.yaml`)
- Topic/keyword scoring rules
- Recipient settings

Email delivery uses outlook-cli (or osascript fallback) — see `weirdapps/communications-marketplace` for the broader email tooling.

## Synthesis prompt anchoring

`news/roster.py` defines a canonical executive roster for Greek banking competitors. Both `synthesizer` and `monitor_synth` inject this into LLM prompts to prevent hallucinated names/transliterations (a recurring failure mode pre-2026-04-25).

## Layout

```
news/                  # core package
  synthesizer.py       # daily digest LLM pipeline
  monitor_synth.py     # real-time mention monitor
  roster.py            # canonical exec name roster (anti-hallucination)
config/                # feed list + topic rules
launchd/               # macOS LaunchAgent plists
templates/             # Jinja2 email templates
data/                  # SQLite + run logs (gitignored)
tests/                 # pytest
```

## MCP

The `mcp[cli]` dependency means this package can also be exposed as an MCP server — letting Claude Code or other MCP clients query stored articles directly.
