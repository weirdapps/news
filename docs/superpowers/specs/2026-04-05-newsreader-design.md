# News Reader — Design Specification

**Date:** 2026-04-05
**Status:** Approved
**Author:** Dimitrios Plessas + Claude

## Overview

A personal news intelligence platform that fetches news from reputable sources across multiple interest areas, processes and deduplicates articles, synthesizes them using Claude (via NBG's Vertex AI), and delivers formatted HTML digests via email on a fixed schedule and on demand.

## Relationship to Existing Systems

This system **complements** the trading hub's `/news` and `/briefing` commands. The trading hub retains ownership of market-specific, portfolio-aware, pre-market intelligence. This system covers the broader news landscape: business, tech, AI, banking industry, Greece, and general interests.

## Architecture

**Type:** Standalone Python project with thin Claude Code skill (`/newsfeed`)
**Location:** `~/SourceCode/news/`

### Five-Stage Pipeline

```
cron (launchd) ──→ main.py ──→ fetch ──→ process ──→ store ──→ synthesize ──→ deliver
                                                       │           │             │
                                                    SQLite    Claude CLI    Gmail API
                                                              (Vertex)   plessas@nbg.gr
```

1. **Fetch** — pulls from RSS feeds, NewsAPI, and WebSearch in parallel
2. **Process** — deduplicates (content hash), classifies by topic, extracts content, cleans HTML
3. **Store** — persists to SQLite with full metadata
4. **Synthesize** — sends curated article batch to Claude Code CLI (`--print` mode) for analysis
5. **Deliver** — renders HTML email from synthesis output, sends via Gmail API

Each stage is a standalone Python module that can run and be tested independently. The orchestrator (`main.py`) chains them and handles errors.

### On-Demand Access

Claude Code skill `/newsfeed` invokes `python3 ~/SourceCode/news/main.py --adhoc`. Identical pipeline to scheduled runs, only the trigger differs.

## News Sources & Topic Categories

Seven categories, each with curated sources. All source definitions live in `config/sources.yaml`.

| Category | RSS Feeds | API/WebSearch Keywords |
|----------|-----------|----------------------|
| **Business & Finance** | Reuters Business, FT, Bloomberg (via RSS), WSJ | "global markets", "corporate earnings" |
| **Investments & Trading** | Seeking Alpha, MarketWatch, Finviz | "algorithmic trading", "automated trading systems", "quantitative finance" |
| **Tech & Internet** | TechCrunch, Ars Technica, The Verge, Wired | "tech industry", "internet regulation" |
| **AI & Agents** | The Batch (deeplearning.ai), MIT Tech Review, AI News (artificialintelligence-news.com), Import AI newsletter | "Claude", "Claude Code", "agentic AI", "AI agents", "MCP servers", "LLM tools" |
| **Apple & Gadgets** | 9to5Mac, MacRumors, Engadget | "macOS", "Apple", "gadgets", "consumer tech" |
| **Greece & Local** | Kathimerini (EN+GR), Capital.gr, Euro2day, Naftemporiki | "Greece economy", "Athens" |
| **Banking** | Banking Dive, Finextra, Reuters Banking | "National Bank of Greece", "NBG", "Greek banks", "European banking", "ECB" |

### Source Configuration

Each source is tagged with: category, language (EN/GR), tier (1=premium, 2=standard, 3=niche), reliability score. NewsAPI keyword searches and WebSearch query templates also defined in the same config file.

### Article Relevance Scoring

| Signal | Points |
|--------|--------|
| Direct mention of NBG/National Bank of Greece | +30 |
| Greek banking sector | +20 |
| Category match to interests | +10 |
| Source tier bonus (tier 1/2/3) | +15/+10/+5 |
| Recency: last 4h / 8h / 24h | +15/+10/+5 |

Articles scoring below 20 points are stored but not included in digest. Threshold is configurable in `config/settings.yaml`.

## Data Pipeline Details

### Fetch (`news/fetcher.py`)

- Parallel fetching using `asyncio` + `httpx`
- `feedparser` for RSS/Atom parsing
- NewsAPI client for keyword-based discovery
- WebSearch fallback via `claude --print` for niche topics without feeds
- Rate limiting per source (respect `robots.txt`)
- Timeout + retry with exponential backoff (max 3 retries)
- Each fetched item normalized to a common `Article` dataclass

### Process (`news/processor.py`)

- **Dedup:** SHA-256 hash of normalized title + first 200 chars of content. Existing hash = skip (record as "also reported by")
- **Classification:** Regex + keyword matching against category definitions from config. Articles can belong to multiple categories.
- **Content extraction:** `trafilatura` (primary), `readability-lxml` (fallback). Strips ads/nav/boilerplate.
- **Language detection:** `langdetect` — tag as EN or GR
- **Quality filter:** Drop articles shorter than 100 words, older than 36 hours, or from unknown/untrusted sources

### Store (`news/storage.py`)

SQLite database: `data/news.db`

```sql
articles     — url (PK), title, source, author, published_at, content,
               summary, content_hash, categories (JSON array), language,
               relevance_score, fetched_at, included_in_digest_id

article_categories — article_url (FK), category (FK) — junction table
                     for querying articles by category efficiently

digests      — id (PK), type (scheduled/adhoc), created_at,
               article_count, synthesis_text, html_output, sent_at

sources      — id (PK), name, url, category, tier, language,
               last_fetched, fetch_count, error_count
```

Auto-cleanup: articles older than 30 days archived to compressed JSON, then deleted from DB. Expected DB size: ~10-50MB for a month.

## AI Synthesis Layer (`news/synthesizer.py`)

### Input Preparation

- Query DB for all articles since last digest, ordered by relevance score
- Group by category, cap at 15 articles per category (context limit management, configurable in settings)
- Build structured prompt with article summaries (not full content)

### Synthesis Prompt

Claude receives:
1. Previous digest highlights (top 3 points) for continuity
2. Time window for this digest
3. Structured JSON of article summaries by category

Claude produces:
1. **Executive Brief:** exactly 5 bullets — most important things to know now
2. **Category sections:** synthesis (not summary) across sources, noting disagreements, flagging fact vs. opinion
3. **High-value stories:** cross-category connections, what changed since last digest
4. **Output format:** structured JSON mapping to email template sections

### Token Budget

- Input: ~4,000-6,000 tokens per digest
- Output: ~2,000-3,000 tokens per digest
- Daily total: ~28,000-36,000 tokens (4 scheduled runs)

### Invocation

Via Claude Code CLI: `claude --print` with structured prompt piped as input. Uses NBG's Vertex AI authentication (gcloud auth).

### Fallback

If Claude CLI fails (auth expired, timeout): retry once, then fall back to raw digest (categorized headlines + links, no synthesis). Notification email sent about synthesis failure.

## Email Delivery & Template

### Delivery (`news/deliver.py`)

- Gmail API via existing `gmail-operations.js` script
- Credentials: `~/.google-skills/gmail/GMailSkill-Credentials.json`
- Recipient: `plessas@nbg.gr`
- Subject format: `news digest — 09:00 sat 5 apr` (always lowercase)
- Ad-hoc subject: `news digest — ad hoc 15:42 sat 5 apr`

### Email Structure

```
┌──────────────────────────────────────────────┐
│  NEWS DIGEST                    Sat 5 Apr    │
│  09:00 Athens                   47 articles  │
├──────────────────────────────────────────────┤
│  EXECUTIVE BRIEF                             │
│  • 5 key bullets                             │
├──────────────────────────────────────────────┤
│  WHAT CHANGED since last digest              │
│  • Delta notes (if applicable)               │
├──────────────────────────────────────────────┤
│  [CATEGORY SECTIONS]                         │
│  Synthesis paragraph                         │
│  Opposing views (where applicable)           │
│  Fact check notes (where applicable)         │
│  Sources listed                              │
├──────────────────────────────────────────────┤
│  Footer: article count, source count, next   │
└──────────────────────────────────────────────┘
```

### Outlook Mac Compatibility

- Tables for all layout (no divs/flexbox)
- All CSS inline on elements (no CSS classes)
- No `<p>` tags — `<br>` for line breaks, `<br><br>` for paragraph spacing
- `mso-` conditional comments for Outlook-specific fixes
- Width set in HTML attributes (`<table width="600">`)
- No background images — solid colors only
- Font: Aptos 12pt, color `#404040`
- Category sections only rendered if they have content
- High-value stories get subtle left border accent
- Template: Jinja2 (`templates/digest.html`)

## Scheduling & Orchestration

### Scheduled Runs (launchd)

Four plist files in `~/Library/LaunchAgents/`:

| Plist | Athens Time | UTC |
|-------|------------|-----|
| `com.news.digest.0900.plist` | 09:00 | 06:00 |
| `com.news.digest.1300.plist` | 13:00 | 10:00 |
| `com.news.digest.1700.plist` | 17:00 | 14:00 |
| `com.news.digest.2100.plist` | 21:00 | 18:00 |

Each runs: `python3 ~/SourceCode/news/main.py --scheduled`

DST handled via `zoneinfo` (Python stdlib).

### Orchestrator Flow (`main.py`)

```
1. Pre-flight checks
   ├── PID lock file — exit if another instance running
   ├── gcloud auth check — if expired, send notification email, exit
   └── Network reachability — retry once after 5 min, then give up

2. Run pipeline
   ├── fetch  → log count, errors
   ├── process → log dedup stats
   ├── store  → log new article count
   ├── synthesize (claude --print) → log token usage
   └── deliver → log send confirmation

3. Post-run
   ├── Update digest record in DB
   ├── Log run summary to data/runs.log
   └── If any stage failed, note in email subject
```

### On-Demand (`/newsfeed` command)

Claude Code skill invokes `python3 ~/SourceCode/news/main.py --adhoc`. Identical pipeline to scheduled runs.

## Error Handling

| Failure | Response |
|---------|----------|
| gcloud auth expired | Send plain notification email via Gmail, skip run |
| RSS feed down | Log, continue with other sources. If >50% fail, flag in subject: `news digest — partial sources` |
| NewsAPI quota exceeded | Skip API sources, proceed with RSS + WebSearch |
| Claude CLI timeout (>120s) | Retry once. If still fails, send raw digest (headlines + links, no synthesis) |
| Claude CLI auth fail | Same as gcloud auth expired |
| Gmail send fail | Save HTML to `~/Downloads/`, log error, macOS notification via `osascript` |
| Concurrent run | PID lock file prevents overlap, second instance exits |
| No new articles | Send short "no significant news" email (confirms system alive) |

### Monitoring

- **Run log:** `data/runs.log` — one line per run with timestamp, type, article count, status, duration
- **Weekly health check:** `scripts/health_check.py` — verifies all 28 weekly runs completed. Alert if >3 missed.

## Project Structure

```
~/SourceCode/news/
├── main.py                    # Orchestrator entry point
├── config/
│   ├── sources.yaml           # RSS feeds, API keywords, WebSearch queries
│   ├── categories.yaml        # Topic definitions, classification rules
│   └── settings.yaml          # Schedule, email, thresholds, paths
├── news/
│   ├── __init__.py
│   ├── fetcher.py             # RSS + NewsAPI + WebSearch fetching
│   ├── processor.py           # Dedup, classify, extract, clean
│   ├── storage.py             # SQLite operations
│   ├── synthesizer.py         # Claude CLI invocation, prompt building
│   ├── deliver.py             # HTML rendering (Jinja2) + Gmail sending
│   └── models.py              # Article, Digest, Source dataclasses
├── templates/
│   └── digest.html            # Jinja2 email template (Outlook-safe)
├── data/
│   ├── news.db                # SQLite database (gitignored)
│   ├── runs.log               # Run history (gitignored)
│   └── archive/               # Compressed old articles (gitignored)
├── scripts/
│   ├── install_launchd.sh     # Installs the 4 launchd plists
│   ├── health_check.py        # Weekly run completion checker
│   └── init_db.py             # Create tables on first run
├── tests/
│   └── ...
├── requirements.txt
├── pyproject.toml
├── .gitignore
└── CLAUDE.md                  # Project-specific instructions
```

## Dependencies

| Package | Purpose |
|---------|---------|
| `feedparser` | RSS/Atom feed parsing |
| `httpx` | Async HTTP client |
| `trafilatura` | Article content extraction from HTML |
| `readability-lxml` | Fallback content extraction |
| `langdetect` | Language detection (EN/GR) |
| `jinja2` | HTML email templating |
| `pyyaml` | Config file parsing |
| `defusedxml` | Safe XML parsing (security) |

Standard library (no install needed): `sqlite3`, `zoneinfo`, `json`, `subprocess`, `asyncio`, `hashlib`, `logging`

Email sending: reuses existing `gmail-operations.js` from manage-gmail skill.
