# News Reader

Personal news intelligence platform with two profiles:
- **digest**: Broad news across business, AI, tech, Greece, banking (4x daily)
- **monitor**: NBG brand monitoring with competitor tracking (hourly)

Both share the same pipeline: fetch → process → store → synthesize → deliver.

## Quick Reference

- **Run digest:** `python3 main.py --adhoc`
- **Run monitor:** `python3 main.py --profile monitor --adhoc`
- **Run tests:** `pytest`
- **Digest config:** `config/sources.yaml`, `config/categories.yaml`, `config/settings.yaml`
- **Monitor config:** `config/monitor/sources.yaml`, `config/monitor/settings.yaml`, `config/monitor/keywords.yaml`
- **Database:** `data/news.db` (SQLite, gitignored, shared by both profiles)
- **Spec:** `docs/superpowers/specs/2026-04-05-newsreader-design.md`

## Architecture

Five-stage pipeline: fetch → process → store → synthesize → deliver.
Each stage is a standalone module in `news/`. The `--profile` flag selects
which config, synthesis prompt, and email template to use.

## Tech Stack

Python 3.12+, feedparser, httpx, trafilatura, Jinja2, SQLite, Claude Code CLI.

## Testing

All tests use in-memory SQLite and mocked HTTP/subprocess calls.
Run: `pytest -v`

## Email

Sends via existing gmail-operations.js. Recipient: plessas@nbg.gr.
Outlook Mac compatible (table layout, inline CSS, no <p> tags).
