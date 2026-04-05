# News Reader

Personal news intelligence platform. Fetches news from RSS/API/WebSearch,
deduplicates, classifies, synthesizes via Claude (Vertex AI), and delivers
HTML email digests.

## Quick Reference

- **Run manually:** `python3 main.py --adhoc`
- **Run tests:** `pytest`
- **Config:** `config/sources.yaml`, `config/categories.yaml`, `config/settings.yaml`
- **Database:** `data/news.db` (SQLite, gitignored)
- **Spec:** `docs/superpowers/specs/2026-04-05-newsreader-design.md`

## Architecture

Five-stage pipeline: fetch → process → store → synthesize → deliver.
Each stage is a standalone module in `news/`.

## Tech Stack

Python 3.12+, feedparser, httpx, trafilatura, Jinja2, SQLite, Claude Code CLI.

## Testing

All tests use in-memory SQLite and mocked HTTP/subprocess calls.
Run: `pytest -v`

## Email

Sends via existing gmail-operations.js. Recipient: plessas@nbg.gr.
Outlook Mac compatible (table layout, inline CSS, no <p> tags).
