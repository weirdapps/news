# News Reader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a personal news intelligence platform that fetches articles from RSS/API/WebSearch, deduplicates and classifies them, synthesizes via Claude (Vertex), and delivers HTML email digests 4x daily + on-demand.

**Architecture:** Five-stage Python pipeline (fetch → process → store → synthesize → deliver) with SQLite persistence, Claude Code CLI for AI synthesis, and Gmail API for delivery. Scheduled via macOS launchd, on-demand via `/newsfeed` Claude Code skill.

**Tech Stack:** Python 3.12+, feedparser, httpx, trafilatura, Jinja2, SQLite, Claude Code CLI, Gmail API (via existing gmail-operations.js)

**Spec:** `docs/superpowers/specs/2026-04-05-newsreader-design.md`

---

## File Map

```
~/SourceCode/news/
├── main.py                          # Orchestrator — CLI entry point, pipeline coordination
├── config/
│   ├── sources.yaml                 # RSS feed URLs, NewsAPI keywords, WebSearch queries
│   ├── categories.yaml              # Topic definitions with classification keywords
│   └── settings.yaml                # Thresholds, email config, paths, schedule
├── news/
│   ├── __init__.py                  # Package init
│   ├── models.py                    # Article, Digest, Source dataclasses
│   ├── config.py                    # YAML config loader, settings access
│   ├── storage.py                   # SQLite schema, CRUD, cleanup
│   ├── fetcher.py                   # RSS + NewsAPI + WebSearch parallel fetching
│   ├── processor.py                 # Dedup, classify, extract, score, filter
│   ├── synthesizer.py               # Claude CLI prompt building + invocation
│   ├── deliver.py                   # Jinja2 HTML rendering + Gmail sending
│   └── auth.py                      # gcloud auth check, notification on failure
├── templates/
│   └── digest.html                  # Outlook-safe Jinja2 email template
├── data/                            # Runtime data (gitignored)
│   ├── news.db
│   ├── runs.log
│   └── archive/
├── scripts/
│   ├── install_launchd.sh           # Install/uninstall launchd plists
│   └── health_check.py              # Weekly run completion audit
├── launchd/                         # Plist templates (committed to git)
│   ├── com.news.digest.0900.plist
│   ├── com.news.digest.1300.plist
│   ├── com.news.digest.1700.plist
│   └── com.news.digest.2100.plist
├── tests/
│   ├── conftest.py                  # Shared fixtures (tmp DB, sample articles)
│   ├── test_models.py
│   ├── test_config.py
│   ├── test_storage.py
│   ├── test_fetcher.py
│   ├── test_processor.py
│   ├── test_synthesizer.py
│   ├── test_deliver.py
│   └── test_orchestrator.py
├── requirements.txt
├── pyproject.toml
├── .gitignore
└── CLAUDE.md
```

---

### Task 1: Project Scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `requirements.txt`
- Create: `.gitignore`
- Create: `CLAUDE.md`
- Create: `news/__init__.py`

- [ ] **Step 1: Initialize git repo**

```bash
cd ~/SourceCode/news
git init
```

- [ ] **Step 2: Create pyproject.toml**

```toml
[project]
name = "newsreader"
version = "0.1.0"
description = "Personal news intelligence platform with AI synthesis"
requires-python = ">=3.12"

[project.scripts]
newsreader = "main:main"

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

- [ ] **Step 3: Create requirements.txt**

```
feedparser>=6.0
httpx>=0.27
trafilatura>=1.12
readability-lxml>=0.8
langdetect>=1.0
jinja2>=3.1
pyyaml>=6.0
defusedxml>=0.7
pytest>=8.0
pytest-asyncio>=0.24
```

- [ ] **Step 4: Create .gitignore**

```gitignore
# Runtime data
data/news.db
data/runs.log
data/archive/

# Python
__pycache__/
*.pyc
*.pyo
.venv/
venv/
*.egg-info/

# IDE
.vscode/
.idea/

# OS
.DS_Store
```

- [ ] **Step 5: Create CLAUDE.md**

```markdown
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
```

- [ ] **Step 6: Create package init**

```python
# news/__init__.py
```

- [ ] **Step 7: Create data directory with .gitkeep**

```bash
mkdir -p data/archive
touch data/.gitkeep
```

- [ ] **Step 8: Create virtual environment and install dependencies**

```bash
cd ~/SourceCode/news
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml requirements.txt .gitignore CLAUDE.md news/__init__.py data/.gitkeep
git commit -m "chore: scaffold project structure and dependencies"
```

---

### Task 2: Configuration Files + Loader

**Files:**
- Create: `config/sources.yaml`
- Create: `config/categories.yaml`
- Create: `config/settings.yaml`
- Create: `news/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write the failing test for config loader**

```python
# tests/test_config.py
import os
import tempfile
from pathlib import Path

import yaml

from news.config import load_config, get_sources, get_categories, get_settings


def test_load_config_reads_yaml(tmp_path):
    config_file = tmp_path / "test.yaml"
    config_file.write_text(yaml.dump({"key": "value", "nested": {"a": 1}}))
    result = load_config(config_file)
    assert result == {"key": "value", "nested": {"a": 1}}


def test_get_sources_returns_feed_list(tmp_path):
    sources_yaml = tmp_path / "sources.yaml"
    sources_yaml.write_text(yaml.dump({
        "rss_feeds": [
            {"name": "TechCrunch", "url": "https://techcrunch.com/feed/",
             "category": "tech", "tier": 1, "language": "en"}
        ],
        "newsapi_keywords": [{"category": "tech", "query": "AI agents"}],
        "websearch_queries": [{"category": "banking", "template": "NBG news {date}"}],
    }))
    sources = get_sources(sources_yaml)
    assert len(sources["rss_feeds"]) == 1
    assert sources["rss_feeds"][0]["name"] == "TechCrunch"
    assert len(sources["newsapi_keywords"]) == 1
    assert len(sources["websearch_queries"]) == 1


def test_get_categories_returns_category_definitions(tmp_path):
    cats_yaml = tmp_path / "categories.yaml"
    cats_yaml.write_text(yaml.dump({
        "categories": {
            "tech": {
                "display_name": "Tech & Internet",
                "keywords": ["technology", "software", "startup"],
                "priority": 3,
            }
        }
    }))
    cats = get_categories(cats_yaml)
    assert "tech" in cats["categories"]
    assert cats["categories"]["tech"]["display_name"] == "Tech & Internet"


def test_get_settings_returns_defaults(tmp_path):
    settings_yaml = tmp_path / "settings.yaml"
    settings_yaml.write_text(yaml.dump({
        "relevance_threshold": 20,
        "max_articles_per_category": 15,
        "email": {"recipient": "plessas@nbg.gr"},
    }))
    settings = get_settings(settings_yaml)
    assert settings["relevance_threshold"] == 20
    assert settings["max_articles_per_category"] == 15
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/SourceCode/news
source .venv/bin/activate
pytest tests/test_config.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'news.config'`

- [ ] **Step 3: Implement config loader**

```python
# news/config.py
from pathlib import Path
from typing import Any

import yaml


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"


def load_config(path: Path) -> dict[str, Any]:
    """Load a YAML configuration file and return its contents as a dict."""
    with open(path) as f:
        return yaml.safe_load(f)


def get_sources(path: Path | None = None) -> dict[str, Any]:
    """Load RSS feeds, NewsAPI keywords, and WebSearch queries."""
    path = path or _CONFIG_DIR / "sources.yaml"
    return load_config(path)


def get_categories(path: Path | None = None) -> dict[str, Any]:
    """Load topic category definitions with classification keywords."""
    path = path or _CONFIG_DIR / "categories.yaml"
    return load_config(path)


def get_settings(path: Path | None = None) -> dict[str, Any]:
    """Load application settings (thresholds, email, paths)."""
    path = path or _CONFIG_DIR / "settings.yaml"
    return load_config(path)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_config.py -v
```

Expected: 4 passed

- [ ] **Step 5: Create sources.yaml**

```yaml
# config/sources.yaml
# News sources organized by type: RSS feeds, NewsAPI keywords, WebSearch queries

rss_feeds:
  # Business & Finance
  - name: "Reuters Business"
    url: "https://www.reutersagency.com/feed/?best-topics=business-finance"
    category: "business"
    tier: 1
    language: "en"
  - name: "Financial Times"
    url: "https://www.ft.com/rss/home"
    category: "business"
    tier: 1
    language: "en"
  - name: "WSJ Markets"
    url: "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"
    category: "business"
    tier: 1
    language: "en"

  # Investments & Trading
  - name: "Seeking Alpha"
    url: "https://seekingalpha.com/market_currents.xml"
    category: "trading"
    tier: 2
    language: "en"
  - name: "MarketWatch Top Stories"
    url: "https://feeds.marketwatch.com/marketwatch/topstories"
    category: "trading"
    tier: 2
    language: "en"

  # Tech & Internet
  - name: "TechCrunch"
    url: "https://techcrunch.com/feed/"
    category: "tech"
    tier: 1
    language: "en"
  - name: "Ars Technica"
    url: "https://feeds.arstechnica.com/arstechnica/index"
    category: "tech"
    tier: 1
    language: "en"
  - name: "The Verge"
    url: "https://www.theverge.com/rss/index.xml"
    category: "tech"
    tier: 1
    language: "en"
  - name: "Wired"
    url: "https://www.wired.com/feed/rss"
    category: "tech"
    tier: 2
    language: "en"

  # AI & Agents
  - name: "MIT Technology Review"
    url: "https://www.technologyreview.com/feed/"
    category: "ai"
    tier: 1
    language: "en"
  - name: "AI News"
    url: "https://www.artificialintelligence-news.com/feed/"
    category: "ai"
    tier: 2
    language: "en"
  - name: "The Batch (deeplearning.ai)"
    url: "https://www.deeplearning.ai/the-batch/feed/"
    category: "ai"
    tier: 1
    language: "en"

  # Apple & Gadgets
  - name: "9to5Mac"
    url: "https://9to5mac.com/feed/"
    category: "apple"
    tier: 1
    language: "en"
  - name: "MacRumors"
    url: "https://feeds.macrumors.com/MacRumors-All"
    category: "apple"
    tier: 1
    language: "en"
  - name: "Engadget"
    url: "https://www.engadget.com/rss.xml"
    category: "apple"
    tier: 2
    language: "en"

  # Greece & Local
  - name: "Kathimerini English"
    url: "https://www.ekathimerini.com/rss"
    category: "greece"
    tier: 1
    language: "en"
  - name: "Kathimerini Greek"
    url: "https://www.kathimerini.gr/feed"
    category: "greece"
    tier: 1
    language: "gr"
  - name: "Capital.gr"
    url: "https://www.capital.gr/rss"
    category: "greece"
    tier: 2
    language: "gr"
  - name: "Naftemporiki"
    url: "https://www.naftemporiki.gr/feed"
    category: "greece"
    tier: 2
    language: "gr"

  # Banking
  - name: "Finextra"
    url: "https://www.finextra.com/rss/headlines.aspx"
    category: "banking"
    tier: 1
    language: "en"
  - name: "Banking Dive"
    url: "https://www.bankingdive.com/feeds/news/"
    category: "banking"
    tier: 2
    language: "en"

newsapi_keywords:
  - category: "business"
    query: "global markets OR corporate earnings"
  - category: "trading"
    query: "algorithmic trading OR automated trading systems OR quantitative finance"
  - category: "tech"
    query: "tech industry OR internet regulation"
  - category: "ai"
    query: "Claude Code OR agentic AI OR AI agents OR MCP servers OR LLM tools"
  - category: "apple"
    query: "macOS OR Apple gadgets OR consumer tech"
  - category: "greece"
    query: "Greece economy OR Athens"
  - category: "banking"
    query: "National Bank of Greece OR NBG OR Greek banks OR European banking OR ECB"

websearch_queries:
  - category: "banking"
    template: "National Bank of Greece news {date}"
  - category: "banking"
    template: "Greek banking sector news {date}"
  - category: "ai"
    template: "Claude Anthropic AI news {date}"
  - category: "ai"
    template: "Claude Code MCP agents news {date}"
```

- [ ] **Step 6: Create categories.yaml**

```yaml
# config/categories.yaml
# Topic definitions with keywords for article classification

categories:
  business:
    display_name: "Business & Finance"
    keywords:
      - "earnings"
      - "revenue"
      - "market cap"
      - "IPO"
      - "merger"
      - "acquisition"
      - "GDP"
      - "inflation"
      - "interest rate"
      - "stock market"
      - "S&P 500"
      - "Dow Jones"
      - "FTSE"
      - "corporate"
      - "quarterly results"
    priority: 1

  trading:
    display_name: "Investments & Trading"
    keywords:
      - "algorithmic trading"
      - "automated trading"
      - "quantitative"
      - "hedge fund"
      - "ETF"
      - "portfolio"
      - "derivatives"
      - "options"
      - "futures"
      - "forex"
      - "cryptocurrency"
      - "bitcoin"
      - "trading strategy"
    priority: 2

  tech:
    display_name: "Tech & Internet"
    keywords:
      - "software"
      - "startup"
      - "cloud"
      - "cybersecurity"
      - "data privacy"
      - "social media"
      - "streaming"
      - "e-commerce"
      - "SaaS"
      - "open source"
      - "browser"
      - "internet"
      - "app"
    priority: 3

  ai:
    display_name: "AI & Agents"
    keywords:
      - "artificial intelligence"
      - "machine learning"
      - "deep learning"
      - "LLM"
      - "large language model"
      - "GPT"
      - "Claude"
      - "Anthropic"
      - "OpenAI"
      - "Google Gemini"
      - "agentic"
      - "AI agent"
      - "MCP"
      - "model context protocol"
      - "generative AI"
      - "GenAI"
      - "transformer"
      - "neural network"
      - "prompt engineering"
      - "fine-tuning"
      - "RAG"
    priority: 4

  apple:
    display_name: "Apple & Gadgets"
    keywords:
      - "Apple"
      - "iPhone"
      - "iPad"
      - "Mac"
      - "macOS"
      - "iOS"
      - "watchOS"
      - "AirPods"
      - "Vision Pro"
      - "WWDC"
      - "MacBook"
      - "gadget"
      - "wearable"
    priority: 5

  greece:
    display_name: "Greece & Local"
    keywords:
      - "Greece"
      - "Greek"
      - "Athens"
      - "Hellenic"
      - "Mitsotakis"
      - "parliament"
      - "tourism"
      - "shipping"
      - "Mediterranean"
    priority: 6

  banking:
    display_name: "Banking — NBG & Sector"
    keywords:
      - "National Bank of Greece"
      - "NBG"
      - "Ethniki"
      - "Greek bank"
      - "Eurobank"
      - "Alpha Bank"
      - "Piraeus Bank"
      - "ECB"
      - "European Central Bank"
      - "banking regulation"
      - "Basel"
      - "fintech"
      - "digital banking"
      - "neobank"
      - "credit card"
      - "payment"
      - "SWIFT"
    priority: 7

# Display order for email digest (banking/greece prioritized for personal relevance)
display_order:
  - "banking"
  - "greece"
  - "business"
  - "trading"
  - "ai"
  - "tech"
  - "apple"
```

- [ ] **Step 7: Create settings.yaml**

```yaml
# config/settings.yaml
# Application settings — thresholds, email config, paths

pipeline:
  relevance_threshold: 20
  max_articles_per_category: 15
  max_article_age_hours: 36
  min_article_length_words: 100
  dedup_hash_chars: 200

email:
  recipient: "plessas@nbg.gr"
  subject_prefix: "news digest"
  gmail_script: "~/.claude/plugins/cache/marketplace-utility/manage-gmail/1.0.0/skills/manage-gmail/scripts/dist/gmail-operations.js"

schedule:
  timezone: "Europe/Athens"
  runs:
    - "09:00"
    - "13:00"
    - "17:00"
    - "21:00"

storage:
  db_path: "data/news.db"
  run_log_path: "data/runs.log"
  archive_dir: "data/archive"
  archive_after_days: 30

synthesis:
  claude_command: "claude"
  claude_args: ["--print"]
  timeout_seconds: 120
  max_retries: 1

newsapi:
  # Set NEWSAPI_KEY environment variable
  base_url: "https://newsapi.org/v2"
  page_size: 20

scoring:
  nbg_mention: 30
  greek_banking: 20
  category_match: 10
  tier_1_bonus: 15
  tier_2_bonus: 10
  tier_3_bonus: 5
  recency_4h: 15
  recency_8h: 10
  recency_24h: 5
```

- [ ] **Step 8: Commit**

```bash
git add config/ news/config.py tests/test_config.py
git commit -m "feat: add configuration files and YAML config loader"
```

---

### Task 3: Data Models

**Files:**
- Create: `news/models.py`
- Create: `tests/test_models.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models.py
from datetime import datetime, timezone

from news.models import Article, Digest, Source


def test_article_creation():
    article = Article(
        url="https://example.com/article-1",
        title="Test Article",
        source="TestSource",
        author="John Doe",
        published_at=datetime(2026, 4, 5, 9, 0, tzinfo=timezone.utc),
        content="This is the full article content for testing purposes.",
        summary="Test summary of the article.",
        categories=["tech", "ai"],
        language="en",
    )
    assert article.url == "https://example.com/article-1"
    assert article.categories == ["tech", "ai"]
    assert article.relevance_score == 0
    assert article.content_hash == ""
    assert article.included_in_digest_id is None


def test_article_compute_hash():
    article = Article(
        url="https://example.com/1",
        title="Test Article",
        source="Src",
        content="Some content here that is long enough to hash properly for dedup.",
        categories=["tech"],
        language="en",
    )
    article.compute_hash()
    assert len(article.content_hash) == 64  # SHA-256 hex digest
    # Same content produces same hash
    article2 = Article(
        url="https://example.com/2",
        title="Test Article",
        source="OtherSrc",
        content="Some content here that is long enough to hash properly for dedup.",
        categories=["tech"],
        language="en",
    )
    article2.compute_hash()
    assert article.content_hash == article2.content_hash


def test_digest_creation():
    digest = Digest(
        digest_type="scheduled",
        article_count=47,
    )
    assert digest.id is None
    assert digest.digest_type == "scheduled"
    assert digest.article_count == 47
    assert digest.synthesis_text == ""
    assert digest.html_output == ""
    assert digest.sent_at is None


def test_source_creation():
    source = Source(
        name="TechCrunch",
        url="https://techcrunch.com/feed/",
        category="tech",
        tier=1,
        language="en",
    )
    assert source.name == "TechCrunch"
    assert source.tier == 1
    assert source.fetch_count == 0
    assert source.error_count == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_models.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'news.models'`

- [ ] **Step 3: Implement data models**

```python
# news/models.py
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Article:
    url: str
    title: str
    source: str
    content: str
    categories: list[str]
    language: str
    author: str = ""
    published_at: datetime | None = None
    summary: str = ""
    content_hash: str = ""
    relevance_score: int = 0
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    included_in_digest_id: int | None = None
    also_reported_by: list[str] = field(default_factory=list)

    def compute_hash(self) -> None:
        """Compute SHA-256 hash from normalized title + first 200 chars of content."""
        normalized_title = self.title.strip().lower()
        content_prefix = self.content[:200].strip().lower()
        raw = f"{normalized_title}|{content_prefix}"
        self.content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class Digest:
    digest_type: str  # "scheduled" or "adhoc"
    article_count: int = 0
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    synthesis_text: str = ""
    html_output: str = ""
    sent_at: datetime | None = None


@dataclass
class Source:
    name: str
    url: str
    category: str
    tier: int
    language: str
    id: int | None = None
    last_fetched: datetime | None = None
    fetch_count: int = 0
    error_count: int = 0
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_models.py -v
```

Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add news/models.py tests/test_models.py
git commit -m "feat: add Article, Digest, Source data models"
```

---

### Task 4: Storage Layer

**Files:**
- Create: `news/storage.py`
- Create: `tests/test_storage.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Create shared test fixtures**

```python
# tests/conftest.py
import sqlite3
from datetime import datetime, timezone

import pytest

from news.storage import init_db
from news.models import Article


@pytest.fixture
def db():
    """In-memory SQLite database initialized with schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def sample_article():
    """A sample Article for testing."""
    article = Article(
        url="https://example.com/test-article",
        title="Test Article About AI Agents",
        source="TechCrunch",
        author="Jane Doe",
        published_at=datetime(2026, 4, 5, 8, 0, tzinfo=timezone.utc),
        content="This is a test article about AI agents and their impact on the industry. " * 10,
        summary="AI agents are changing the industry.",
        categories=["ai", "tech"],
        language="en",
        relevance_score=35,
    )
    article.compute_hash()
    return article
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_storage.py
import json
from datetime import datetime, timezone, timedelta

from news.models import Article, Digest
from news.storage import (
    init_db,
    insert_article,
    get_article_by_url,
    get_article_by_hash,
    get_articles_since,
    insert_digest,
    get_last_digest,
    update_digest_sent,
    get_run_stats,
)


def test_init_db_creates_tables(db):
    cursor = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row["name"] for row in cursor.fetchall()]
    assert "articles" in tables
    assert "article_categories" in tables
    assert "digests" in tables
    assert "sources" in tables


def test_insert_and_retrieve_article(db, sample_article):
    insert_article(db, sample_article)
    retrieved = get_article_by_url(db, sample_article.url)
    assert retrieved is not None
    assert retrieved.title == "Test Article About AI Agents"
    assert retrieved.source == "TechCrunch"
    assert retrieved.categories == ["ai", "tech"]
    assert retrieved.relevance_score == 35


def test_duplicate_article_skipped(db, sample_article):
    insert_article(db, sample_article)
    # Same URL should not raise, just skip
    insert_article(db, sample_article)
    cursor = db.execute("SELECT COUNT(*) as cnt FROM articles")
    assert cursor.fetchone()["cnt"] == 1


def test_get_article_by_hash(db, sample_article):
    insert_article(db, sample_article)
    found = get_article_by_hash(db, sample_article.content_hash)
    assert found is not None
    assert found.url == sample_article.url


def test_get_articles_since(db, sample_article):
    insert_article(db, sample_article)
    # Articles since 1 hour ago should include our article
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    articles = get_articles_since(db, since)
    assert len(articles) == 1
    assert articles[0].url == sample_article.url

    # Articles since 1 hour in the future should return empty
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    articles = get_articles_since(db, future)
    assert len(articles) == 0


def test_insert_and_get_digest(db):
    digest = Digest(digest_type="scheduled", article_count=42)
    digest_id = insert_digest(db, digest)
    assert digest_id is not None

    last = get_last_digest(db)
    assert last is not None
    assert last.id == digest_id
    assert last.article_count == 42
    assert last.sent_at is None


def test_update_digest_sent(db):
    digest = Digest(digest_type="adhoc", article_count=10)
    digest_id = insert_digest(db, digest)
    update_digest_sent(db, digest_id)
    last = get_last_digest(db)
    assert last.sent_at is not None


def test_get_run_stats_empty(db):
    stats = get_run_stats(db)
    assert stats["total_articles"] == 0
    assert stats["total_digests"] == 0
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_storage.py -v
```

Expected: FAIL — `ImportError: cannot import name 'init_db' from 'news.storage'`

- [ ] **Step 4: Implement storage layer**

```python
# news/storage.py
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from news.models import Article, Digest


def init_db(conn: sqlite3.Connection) -> None:
    """Create database tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS articles (
            url TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            author TEXT DEFAULT '',
            published_at TEXT,
            content TEXT NOT NULL,
            summary TEXT DEFAULT '',
            content_hash TEXT NOT NULL,
            language TEXT NOT NULL,
            relevance_score INTEGER DEFAULT 0,
            fetched_at TEXT NOT NULL,
            included_in_digest_id INTEGER,
            also_reported_by TEXT DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS article_categories (
            article_url TEXT NOT NULL,
            category TEXT NOT NULL,
            PRIMARY KEY (article_url, category),
            FOREIGN KEY (article_url) REFERENCES articles(url)
        );

        CREATE TABLE IF NOT EXISTS digests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            digest_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            article_count INTEGER DEFAULT 0,
            synthesis_text TEXT DEFAULT '',
            html_output TEXT DEFAULT '',
            sent_at TEXT
        );

        CREATE TABLE IF NOT EXISTS sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL,
            tier INTEGER NOT NULL,
            language TEXT NOT NULL,
            last_fetched TEXT,
            fetch_count INTEGER DEFAULT 0,
            error_count INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_articles_hash ON articles(content_hash);
        CREATE INDEX IF NOT EXISTS idx_articles_fetched ON articles(fetched_at);
        CREATE INDEX IF NOT EXISTS idx_article_categories_cat ON article_categories(category);
    """)
    conn.commit()


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection with row factory enabled."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _dt_to_str(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


def _str_to_dt(s: str | None) -> datetime | None:
    if s is None:
        return None
    return datetime.fromisoformat(s)


def insert_article(conn: sqlite3.Connection, article: Article) -> bool:
    """Insert an article. Returns True if inserted, False if duplicate (by URL)."""
    try:
        conn.execute(
            """INSERT INTO articles
               (url, title, source, author, published_at, content, summary,
                content_hash, language, relevance_score, fetched_at,
                included_in_digest_id, also_reported_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                article.url, article.title, article.source, article.author,
                _dt_to_str(article.published_at), article.content, article.summary,
                article.content_hash, article.language, article.relevance_score,
                _dt_to_str(article.fetched_at), article.included_in_digest_id,
                json.dumps(article.also_reported_by),
            ),
        )
        for cat in article.categories:
            conn.execute(
                "INSERT INTO article_categories (article_url, category) VALUES (?, ?)",
                (article.url, cat),
            )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def _row_to_article(conn: sqlite3.Connection, row: sqlite3.Row) -> Article:
    """Convert a database row to an Article, loading categories from junction table."""
    cat_rows = conn.execute(
        "SELECT category FROM article_categories WHERE article_url = ?",
        (row["url"],),
    ).fetchall()
    categories = [r["category"] for r in cat_rows]

    return Article(
        url=row["url"],
        title=row["title"],
        source=row["source"],
        author=row["author"],
        published_at=_str_to_dt(row["published_at"]),
        content=row["content"],
        summary=row["summary"],
        content_hash=row["content_hash"],
        categories=categories,
        language=row["language"],
        relevance_score=row["relevance_score"],
        fetched_at=_str_to_dt(row["fetched_at"]),
        included_in_digest_id=row["included_in_digest_id"],
        also_reported_by=json.loads(row["also_reported_by"]),
    )


def get_article_by_url(conn: sqlite3.Connection, url: str) -> Article | None:
    """Retrieve an article by its URL."""
    row = conn.execute("SELECT * FROM articles WHERE url = ?", (url,)).fetchone()
    if row is None:
        return None
    return _row_to_article(conn, row)


def get_article_by_hash(conn: sqlite3.Connection, content_hash: str) -> Article | None:
    """Find an article by its content hash (for deduplication)."""
    row = conn.execute(
        "SELECT * FROM articles WHERE content_hash = ?", (content_hash,)
    ).fetchone()
    if row is None:
        return None
    return _row_to_article(conn, row)


def get_articles_since(
    conn: sqlite3.Connection,
    since: datetime,
    min_score: int = 0,
    category: str | None = None,
) -> list[Article]:
    """Get articles fetched since a given datetime, optionally filtered."""
    since_str = _dt_to_str(since)

    if category:
        rows = conn.execute(
            """SELECT a.* FROM articles a
               JOIN article_categories ac ON a.url = ac.article_url
               WHERE a.fetched_at >= ? AND a.relevance_score >= ? AND ac.category = ?
               ORDER BY a.relevance_score DESC""",
            (since_str, min_score, category),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM articles
               WHERE fetched_at >= ? AND relevance_score >= ?
               ORDER BY relevance_score DESC""",
            (since_str, min_score),
        ).fetchall()

    return [_row_to_article(conn, row) for row in rows]


def insert_digest(conn: sqlite3.Connection, digest: Digest) -> int:
    """Insert a digest record and return its ID."""
    cursor = conn.execute(
        """INSERT INTO digests (digest_type, created_at, article_count,
                                synthesis_text, html_output, sent_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            digest.digest_type, _dt_to_str(digest.created_at),
            digest.article_count, digest.synthesis_text,
            digest.html_output, _dt_to_str(digest.sent_at),
        ),
    )
    conn.commit()
    return cursor.lastrowid


def get_last_digest(conn: sqlite3.Connection) -> Digest | None:
    """Get the most recent digest."""
    row = conn.execute(
        "SELECT * FROM digests ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return Digest(
        id=row["id"],
        digest_type=row["digest_type"],
        created_at=_str_to_dt(row["created_at"]),
        article_count=row["article_count"],
        synthesis_text=row["synthesis_text"],
        html_output=row["html_output"],
        sent_at=_str_to_dt(row["sent_at"]),
    )


def update_digest_sent(conn: sqlite3.Connection, digest_id: int) -> None:
    """Mark a digest as sent."""
    now = _dt_to_str(datetime.now(timezone.utc))
    conn.execute("UPDATE digests SET sent_at = ? WHERE id = ?", (now, digest_id))
    conn.commit()


def get_run_stats(conn: sqlite3.Connection) -> dict:
    """Get summary statistics for monitoring."""
    articles = conn.execute("SELECT COUNT(*) as cnt FROM articles").fetchone()["cnt"]
    digests = conn.execute("SELECT COUNT(*) as cnt FROM digests").fetchone()["cnt"]
    return {"total_articles": articles, "total_digests": digests}
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_storage.py -v
```

Expected: 8 passed

- [ ] **Step 6: Commit**

```bash
git add news/storage.py tests/conftest.py tests/test_storage.py
git commit -m "feat: add SQLite storage layer with CRUD operations"
```

---

### Task 5: RSS Fetcher

**Files:**
- Create: `news/fetcher.py`
- Create: `tests/test_fetcher.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fetcher.py
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from news.fetcher import parse_rss_feed, fetch_rss_feeds, normalize_rss_entry
from news.models import Article


SAMPLE_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Test Feed</title>
  <item>
    <title>AI Agents Transform Banking</title>
    <link>https://example.com/ai-banking</link>
    <description>A story about AI agents in banking.</description>
    <pubDate>Sat, 05 Apr 2026 08:00:00 GMT</pubDate>
    <author>jane@example.com (Jane Doe)</author>
  </item>
  <item>
    <title>New MacOS Features</title>
    <link>https://example.com/macos-features</link>
    <description>macOS gets new features.</description>
    <pubDate>Sat, 05 Apr 2026 07:00:00 GMT</pubDate>
  </item>
</channel>
</rss>"""


def test_parse_rss_feed_extracts_entries():
    source_config = {
        "name": "TestFeed",
        "url": "https://example.com/feed",
        "category": "tech",
        "tier": 1,
        "language": "en",
    }
    articles = parse_rss_feed(SAMPLE_RSS_XML, source_config)
    assert len(articles) == 2
    assert articles[0].title == "AI Agents Transform Banking"
    assert articles[0].source == "TestFeed"
    assert articles[0].categories == ["tech"]
    assert articles[0].language == "en"
    assert articles[0].url == "https://example.com/ai-banking"


def test_normalize_rss_entry_handles_missing_fields():
    entry = {
        "title": "Minimal Article",
        "link": "https://example.com/minimal",
    }
    source_config = {
        "name": "Src",
        "category": "ai",
        "tier": 2,
        "language": "en",
    }
    article = normalize_rss_entry(entry, source_config)
    assert article.title == "Minimal Article"
    assert article.author == ""
    assert article.summary == ""
    assert article.categories == ["ai"]


@pytest.mark.asyncio
async def test_fetch_rss_feeds_handles_errors():
    """Feeds that fail should be logged but not crash the pipeline."""
    sources = [
        {"name": "Bad Feed", "url": "https://bad.example.com/feed",
         "category": "tech", "tier": 1, "language": "en"},
    ]
    with patch("news.fetcher.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client_cls.return_value = mock_client

        articles, errors = await fetch_rss_feeds(sources)
        assert len(articles) == 0
        assert len(errors) == 1
        assert "Bad Feed" in errors[0]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_fetcher.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'news.fetcher'`

- [ ] **Step 3: Implement the fetcher**

```python
# news/fetcher.py
import asyncio
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import httpx

from news.models import Article

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30
_MAX_CONCURRENT = 10


def normalize_rss_entry(entry: dict, source_config: dict) -> Article:
    """Convert a feedparser entry dict into an Article."""
    title = entry.get("title", "").strip()
    link = entry.get("link", "").strip()
    summary = entry.get("summary", entry.get("description", "")).strip()
    content = ""
    if "content" in entry and entry["content"]:
        content = entry["content"][0].get("value", "")
    if not content:
        content = summary

    author = ""
    if "author" in entry:
        author = entry["author"]
    elif "author_detail" in entry:
        author = entry["author_detail"].get("name", "")

    published_at = None
    for date_field in ("published_parsed", "updated_parsed"):
        parsed = entry.get(date_field)
        if parsed:
            try:
                published_at = datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
            break
    if published_at is None and "published" in entry:
        try:
            published_at = parsedate_to_datetime(entry["published"])
        except (TypeError, ValueError):
            pass

    return Article(
        url=link,
        title=title,
        source=source_config["name"],
        author=author,
        published_at=published_at,
        content=content,
        summary=summary,
        categories=[source_config["category"]],
        language=source_config.get("language", "en"),
    )


def parse_rss_feed(xml_content: str, source_config: dict) -> list[Article]:
    """Parse RSS/Atom XML content into a list of Articles."""
    feed = feedparser.parse(xml_content)
    articles = []
    for entry in feed.entries:
        try:
            article = normalize_rss_entry(entry, source_config)
            if article.url and article.title:
                articles.append(article)
        except Exception as e:
            logger.warning("Failed to parse entry from %s: %s", source_config["name"], e)
    return articles


async def _fetch_single_feed(
    client: httpx.AsyncClient, source: dict
) -> tuple[list[Article], str | None]:
    """Fetch and parse a single RSS feed. Returns (articles, error_or_none)."""
    try:
        response = await client.get(source["url"], timeout=_REQUEST_TIMEOUT)
        response.raise_for_status()
        articles = parse_rss_feed(response.text, source)
        logger.info("Fetched %d articles from %s", len(articles), source["name"])
        return articles, None
    except Exception as e:
        error_msg = f"{source['name']}: {e}"
        logger.warning("Failed to fetch %s: %s", source["name"], e)
        return [], error_msg


async def fetch_rss_feeds(
    sources: list[dict],
) -> tuple[list[Article], list[str]]:
    """Fetch all RSS feeds in parallel. Returns (all_articles, errors)."""
    all_articles: list[Article] = []
    errors: list[str] = []
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT)

    async def _limited_fetch(client: httpx.AsyncClient, source: dict):
        async with semaphore:
            return await _fetch_single_feed(client, source)

    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": "NewsReader/1.0 (personal news aggregator)"},
    ) as client:
        tasks = [_limited_fetch(client, s) for s in sources]
        results = await asyncio.gather(*tasks)

    for articles, error in results:
        all_articles.extend(articles)
        if error:
            errors.append(error)

    logger.info(
        "RSS fetch complete: %d articles from %d feeds, %d errors",
        len(all_articles), len(sources), len(errors),
    )
    return all_articles, errors
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_fetcher.py -v
```

Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add news/fetcher.py tests/test_fetcher.py
git commit -m "feat: add RSS feed fetcher with parallel async fetching"
```

---

### Task 6: Processor (Dedup, Classify, Score, Filter)

**Files:**
- Create: `news/processor.py`
- Create: `tests/test_processor.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_processor.py
from datetime import datetime, timezone, timedelta

from news.models import Article
from news.processor import (
    deduplicate,
    classify_article,
    compute_relevance_score,
    filter_quality,
    extract_content,
    process_articles,
)


def _make_article(title="Test", content="Long enough content. " * 20,
                   url="https://example.com/1", source="Src",
                   categories=None, language="en",
                   published_at=None) -> Article:
    if categories is None:
        categories = ["tech"]
    if published_at is None:
        published_at = datetime.now(timezone.utc)
    return Article(
        url=url, title=title, source=source, content=content,
        categories=categories, language=language, published_at=published_at,
    )


def test_deduplicate_removes_exact_duplicates():
    a1 = _make_article(title="Same Title", content="Same content. " * 20,
                       url="https://a.com/1", source="Source A")
    a2 = _make_article(title="Same Title", content="Same content. " * 20,
                       url="https://b.com/1", source="Source B")
    a3 = _make_article(title="Different", content="Other content. " * 20,
                       url="https://c.com/1", source="Source C")
    unique, dupes = deduplicate([a1, a2, a3], existing_hashes=set())
    assert len(unique) == 2
    assert len(dupes) == 1


def test_deduplicate_respects_existing_hashes():
    a1 = _make_article(title="Old News", content="Already in DB. " * 20)
    a1.compute_hash()
    existing = {a1.content_hash}
    unique, dupes = deduplicate([a1], existing_hashes=existing)
    assert len(unique) == 0
    assert len(dupes) == 1


def test_classify_article_adds_categories():
    categories_config = {
        "categories": {
            "ai": {
                "display_name": "AI & Agents",
                "keywords": ["artificial intelligence", "AI agent", "Claude"],
                "priority": 4,
            },
            "banking": {
                "display_name": "Banking",
                "keywords": ["National Bank of Greece", "NBG", "ECB"],
                "priority": 7,
            },
        }
    }
    article = _make_article(
        title="Claude AI Agent Used by NBG",
        content="National Bank of Greece adopts Claude AI agents for operations.",
        categories=[],
    )
    classify_article(article, categories_config)
    assert "ai" in article.categories
    assert "banking" in article.categories


def test_compute_relevance_score():
    scoring = {
        "nbg_mention": 30, "greek_banking": 20, "category_match": 10,
        "tier_1_bonus": 15, "tier_2_bonus": 10, "tier_3_bonus": 5,
        "recency_4h": 15, "recency_8h": 10, "recency_24h": 5,
    }
    article = _make_article(
        title="NBG Reports Record Profits",
        content="National Bank of Greece posted record earnings.",
        categories=["banking"],
        published_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    score = compute_relevance_score(article, scoring, source_tier=1)
    # NBG mention (30) + category match (10) + tier 1 (15) + recency 4h (15) = 70
    assert score >= 70


def test_filter_quality_drops_short_articles():
    short = _make_article(content="Too short.")
    long = _make_article(content="This is a properly long article. " * 20)
    kept, dropped = filter_quality(
        [short, long], min_words=100, max_age_hours=36
    )
    assert len(kept) == 1
    assert len(dropped) == 1


def test_filter_quality_drops_old_articles():
    old = _make_article(
        published_at=datetime.now(timezone.utc) - timedelta(hours=48)
    )
    recent = _make_article(
        published_at=datetime.now(timezone.utc) - timedelta(hours=2)
    )
    kept, dropped = filter_quality(
        [old, recent], min_words=5, max_age_hours=36
    )
    assert len(kept) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_processor.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'news.processor'`

- [ ] **Step 3: Implement processor**

```python
# news/processor.py
import logging
import re
from datetime import datetime, timezone, timedelta

from news.models import Article

logger = logging.getLogger(__name__)


def deduplicate(
    articles: list[Article], existing_hashes: set[str]
) -> tuple[list[Article], list[Article]]:
    """Remove duplicate articles by content hash.

    Returns (unique_articles, duplicate_articles).
    Duplicates get their source added to the original's also_reported_by.
    """
    seen: dict[str, Article] = {}
    unique = []
    dupes = []

    for article in articles:
        article.compute_hash()
        if article.content_hash in existing_hashes:
            dupes.append(article)
            continue
        if article.content_hash in seen:
            seen[article.content_hash].also_reported_by.append(article.source)
            dupes.append(article)
        else:
            seen[article.content_hash] = article
            unique.append(article)

    logger.info("Dedup: %d unique, %d duplicates", len(unique), len(dupes))
    return unique, dupes


def classify_article(article: Article, categories_config: dict) -> None:
    """Classify an article into categories based on keyword matching.

    Modifies article.categories in place. An article can belong to multiple categories.
    """
    text = f"{article.title} {article.content}".lower()
    matched = set(article.categories)  # keep existing (from RSS source category)

    for cat_id, cat_def in categories_config["categories"].items():
        for keyword in cat_def["keywords"]:
            if keyword.lower() in text:
                matched.add(cat_id)
                break

    article.categories = list(matched)


def compute_relevance_score(
    article: Article, scoring: dict, source_tier: int = 2
) -> int:
    """Compute relevance score for an article based on content and metadata."""
    score = 0
    text = f"{article.title} {article.content}".lower()

    # NBG mention
    nbg_patterns = ["national bank of greece", "nbg", "ethniki trapeza"]
    if any(p in text for p in nbg_patterns):
        score += scoring["nbg_mention"]

    # Greek banking sector
    greek_bank_patterns = ["greek bank", "hellenic bank", "alpha bank",
                           "eurobank", "piraeus bank", "greek banking"]
    if any(p in text for p in greek_bank_patterns):
        score += scoring["greek_banking"]

    # Category match (article has at least one interest category)
    if article.categories:
        score += scoring["category_match"]

    # Source tier
    tier_key = f"tier_{source_tier}_bonus"
    score += scoring.get(tier_key, 0)

    # Recency
    if article.published_at:
        age = datetime.now(timezone.utc) - article.published_at
        if age < timedelta(hours=4):
            score += scoring["recency_4h"]
        elif age < timedelta(hours=8):
            score += scoring["recency_8h"]
        elif age < timedelta(hours=24):
            score += scoring["recency_24h"]

    article.relevance_score = score
    return score


def filter_quality(
    articles: list[Article],
    min_words: int = 100,
    max_age_hours: int = 36,
) -> tuple[list[Article], list[Article]]:
    """Filter out low-quality articles. Returns (kept, dropped)."""
    kept = []
    dropped = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

    for article in articles:
        word_count = len(article.content.split())
        if word_count < min_words:
            dropped.append(article)
            continue
        if article.published_at and article.published_at < cutoff:
            dropped.append(article)
            continue
        kept.append(article)

    logger.info("Quality filter: %d kept, %d dropped", len(kept), len(dropped))
    return kept, dropped


def extract_content(article: Article) -> None:
    """Extract clean article text using trafilatura, with readability fallback.

    Modifies article.content and article.summary in place. If the article already
    has substantial content (from RSS feed), skip extraction.
    """
    # If we already have substantial content from the feed, skip
    if len(article.content.split()) > 100:
        if not article.summary:
            article.summary = " ".join(article.content.split()[:50]) + "..."
        return

    try:
        import trafilatura
        downloaded = trafilatura.fetch_url(article.url)
        if downloaded:
            extracted = trafilatura.extract(downloaded)
            if extracted:
                article.content = extracted
                article.summary = " ".join(extracted.split()[:50]) + "..."
                return
    except Exception as e:
        logger.debug("trafilatura failed for %s: %s", article.url, e)

    try:
        from readability import Document
        import httpx
        resp = httpx.get(article.url, timeout=15, follow_redirects=True)
        doc = Document(resp.text)
        article.content = doc.summary()
        article.summary = doc.short_title()
    except Exception as e:
        logger.debug("readability fallback failed for %s: %s", article.url, e)


def process_articles(
    articles: list[Article],
    existing_hashes: set[str],
    categories_config: dict,
    scoring_config: dict,
    source_tiers: dict[str, int],
    min_words: int = 100,
    max_age_hours: int = 36,
) -> tuple[list[Article], dict]:
    """Run the full processing pipeline on a batch of articles.

    Returns (processed_articles, stats_dict).
    """
    # 1. Quality filter
    articles, dropped = filter_quality(articles, min_words, max_age_hours)

    # 2. Deduplicate
    articles, dupes = deduplicate(articles, existing_hashes)

    # 3. Classify and score
    for article in articles:
        classify_article(article, categories_config)
        tier = source_tiers.get(article.source, 2)
        compute_relevance_score(article, scoring_config, source_tier=tier)

    stats = {
        "total_input": len(articles) + len(dropped) + len(dupes),
        "quality_dropped": len(dropped),
        "duplicates": len(dupes),
        "processed": len(articles),
    }
    return articles, stats
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_processor.py -v
```

Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add news/processor.py tests/test_processor.py
git commit -m "feat: add article processor with dedup, classification, scoring, filtering"
```

---

### Task 7: Synthesizer (Claude CLI)

**Files:**
- Create: `news/synthesizer.py`
- Create: `tests/test_synthesizer.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_synthesizer.py
import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from news.models import Article, Digest
from news.synthesizer import (
    build_prompt,
    invoke_claude,
    parse_synthesis_output,
    build_fallback_digest,
    synthesize,
)


def _make_articles():
    return [
        Article(
            url="https://example.com/1", title="NBG Record Profits",
            source="Reuters", content="National Bank of Greece reported record profits.",
            categories=["banking"], language="en", relevance_score=70,
        ),
        Article(
            url="https://example.com/2", title="Claude Code Update",
            source="TechCrunch", content="Anthropic released a major Claude Code update.",
            categories=["ai"], language="en", relevance_score=45,
        ),
    ]


def test_build_prompt_includes_articles():
    articles = _make_articles()
    prompt = build_prompt(
        articles_by_category={"banking": [articles[0]], "ai": [articles[1]]},
        previous_highlights=["NBG had a strong quarter"],
        time_window="09:00-13:00 Athens, April 5 2026",
    )
    assert "NBG Record Profits" in prompt
    assert "Claude Code Update" in prompt
    assert "09:00-13:00 Athens" in prompt
    assert "NBG had a strong quarter" in prompt


def test_build_prompt_requests_json_output():
    articles = _make_articles()
    prompt = build_prompt(
        articles_by_category={"banking": [articles[0]]},
        previous_highlights=[],
        time_window="09:00-13:00",
    )
    assert "JSON" in prompt


def test_parse_synthesis_output_valid_json():
    raw = json.dumps({
        "executive_brief": ["Point 1", "Point 2", "Point 3", "Point 4", "Point 5"],
        "what_changed": ["NBG upgraded by analysts"],
        "sections": [
            {
                "category": "banking",
                "display_name": "Banking — NBG & Sector",
                "synthesis": "NBG reported strong results.",
                "opposing_views": "",
                "fact_check": "",
                "sources": ["Reuters", "FT"],
            }
        ],
    })
    result = parse_synthesis_output(raw)
    assert len(result["executive_brief"]) == 5
    assert len(result["sections"]) == 1
    assert result["sections"][0]["category"] == "banking"


def test_parse_synthesis_output_extracts_json_from_prose():
    raw = 'Here is the analysis:\n```json\n{"executive_brief": ["P1"], "what_changed": [], "sections": []}\n```\nDone.'
    result = parse_synthesis_output(raw)
    assert result["executive_brief"] == ["P1"]


def test_build_fallback_digest():
    articles = _make_articles()
    fallback = build_fallback_digest(
        {"banking": [articles[0]], "ai": [articles[1]]},
        {"banking": "Banking — NBG & Sector", "ai": "AI & Agents"},
    )
    assert "NBG Record Profits" in fallback
    assert "Claude Code Update" in fallback


def test_invoke_claude_success():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = '{"executive_brief": ["test"], "what_changed": [], "sections": []}'

    with patch("news.synthesizer.subprocess.run", return_value=mock_result) as mock_run:
        output = invoke_claude("test prompt", timeout=30)
        assert output == mock_result.stdout
        mock_run.assert_called_once()


def test_invoke_claude_timeout():
    with patch("news.synthesizer.subprocess.run", side_effect=TimeoutError):
        output = invoke_claude("test prompt", timeout=1)
        assert output is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_synthesizer.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'news.synthesizer'`

- [ ] **Step 3: Implement synthesizer**

```python
# news/synthesizer.py
import json
import logging
import re
import subprocess

from news.models import Article

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a personal news analyst for a senior banking executive at National Bank of Greece.
Your job is to synthesize news articles into an intelligence brief.

RULES:
- Synthesize, don't summarize. Find the story across sources.
- Where sources disagree, note the tension explicitly.
- Flag what is fact vs. what is opinion or speculation.
- For high-value stories (especially NBG, Greek banking, major AI developments): connect dots across categories and note what changed since the last digest.
- Be concise but substantive. Every sentence should earn its place.
- Output MUST be valid JSON matching the schema below. No markdown, no prose outside the JSON.

OUTPUT SCHEMA:
{
  "executive_brief": ["string — exactly 5 bullets, most important things right now"],
  "what_changed": ["string — what's different since last digest, empty array if first run"],
  "sections": [
    {
      "category": "string — category id",
      "display_name": "string — human readable category name",
      "synthesis": "string — the synthesized analysis",
      "opposing_views": "string — where sources disagree, empty if N/A",
      "fact_check": "string — fact vs opinion flags, empty if N/A",
      "sources": ["string — source names cited"],
      "high_value": false
    }
  ]
}"""


def build_prompt(
    articles_by_category: dict[str, list[Article]],
    previous_highlights: list[str],
    time_window: str,
) -> str:
    """Build the synthesis prompt with article data."""
    articles_json = {}
    for category, articles in articles_by_category.items():
        articles_json[category] = [
            {
                "title": a.title,
                "source": a.source,
                "summary": a.summary or a.content[:300],
                "url": a.url,
                "published": a.published_at.isoformat() if a.published_at else "",
                "language": a.language,
            }
            for a in articles
        ]

    context = {
        "time_window": time_window,
        "previous_highlights": previous_highlights if previous_highlights else ["First run — no previous digest"],
        "articles_by_category": articles_json,
    }

    prompt = f"""{_SYSTEM_PROMPT}

CONTEXT:
{json.dumps(context, indent=2, ensure_ascii=False)}

Produce the JSON output now."""
    return prompt


def invoke_claude(
    prompt: str,
    timeout: int = 120,
    claude_command: str = "claude",
    claude_args: list[str] | None = None,
) -> str | None:
    """Invoke Claude Code CLI with the given prompt. Returns stdout or None on failure."""
    if claude_args is None:
        claude_args = ["--print"]

    cmd = [claude_command] + claude_args
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            logger.error("Claude CLI failed (code %d): %s", result.returncode, result.stderr)
            return None
        return result.stdout
    except subprocess.TimeoutExpired:
        logger.error("Claude CLI timed out after %ds", timeout)
        return None
    except (TimeoutError, Exception) as e:
        logger.error("Claude CLI error: %s", e)
        return None


def parse_synthesis_output(raw: str) -> dict:
    """Parse Claude's output into structured synthesis data.

    Handles both clean JSON and JSON embedded in markdown code blocks.
    """
    # Try direct JSON parse first
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Try extracting JSON from markdown code block
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try finding JSON object in the text
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    logger.error("Failed to parse synthesis output as JSON")
    return {
        "executive_brief": ["Synthesis output could not be parsed."],
        "what_changed": [],
        "sections": [],
    }


def build_fallback_digest(
    articles_by_category: dict[str, list[Article]],
    category_display_names: dict[str, str],
) -> str:
    """Build a plain-text fallback digest when synthesis fails.

    Returns raw text with categorized headlines and links.
    """
    lines = ["SYNTHESIS UNAVAILABLE — Raw headlines below:", ""]
    for category, articles in articles_by_category.items():
        display_name = category_display_names.get(category, category.title())
        lines.append(f"=== {display_name} ===")
        for a in articles[:10]:
            lines.append(f"  - {a.title} ({a.source})")
            lines.append(f"    {a.url}")
        lines.append("")
    return "\n".join(lines)


def synthesize(
    articles_by_category: dict[str, list[Article]],
    category_display_names: dict[str, str],
    previous_highlights: list[str],
    time_window: str,
    claude_command: str = "claude",
    claude_args: list[str] | None = None,
    timeout: int = 120,
    max_retries: int = 1,
) -> tuple[dict, bool]:
    """Run the full synthesis pipeline.

    Returns (synthesis_data, used_claude). If Claude fails after retries,
    returns fallback data with used_claude=False.
    """
    prompt = build_prompt(articles_by_category, previous_highlights, time_window)

    for attempt in range(1 + max_retries):
        raw = invoke_claude(prompt, timeout, claude_command, claude_args)
        if raw:
            result = parse_synthesis_output(raw)
            if result.get("sections") or result.get("executive_brief"):
                logger.info("Synthesis succeeded on attempt %d", attempt + 1)
                return result, True
        if attempt < max_retries:
            logger.warning("Synthesis attempt %d failed, retrying...", attempt + 1)

    logger.warning("All synthesis attempts failed, using fallback")
    fallback_text = build_fallback_digest(articles_by_category, category_display_names)
    return {
        "executive_brief": ["Synthesis unavailable — see categorized headlines below."],
        "what_changed": [],
        "sections": [],
        "_fallback_text": fallback_text,
    }, False
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_synthesizer.py -v
```

Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add news/synthesizer.py tests/test_synthesizer.py
git commit -m "feat: add AI synthesis layer with Claude CLI invocation and fallback"
```

---

### Task 8: Email Template (Jinja2, Outlook-safe)

**Files:**
- Create: `templates/digest.html`

- [ ] **Step 1: Create the Outlook-compatible Jinja2 template**

```html
<!--[if mso]>
<noscript>
<xml>
<o:OfficeDocumentSettings>
<o:PixelsPerInch>96</o:PixelsPerInch>
</o:OfficeDocumentSettings>
</xml>
</noscript>
<![endif]-->
<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ subject }}</title>
</head>
<body style="margin:0; padding:0; background-color:#f5f5f5; font-family:Aptos, Calibri, Arial, sans-serif; font-size:12pt; color:#404040;">

<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f5f5f5;">
<tr><td align="center" style="padding:20px 10px;">

<!-- Main container -->
<table width="600" cellpadding="0" cellspacing="0" border="0" style="background-color:#ffffff; border:1px solid #e0e0e0;">

<!-- Header -->
<tr>
<td style="padding:24px 30px 16px 30px; border-bottom:2px solid #2c3e50; font-family:Aptos, Calibri, Arial, sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0">
<tr>
<td style="font-size:20pt; font-weight:bold; color:#2c3e50; font-family:Aptos, Calibri, Arial, sans-serif;">NEWS DIGEST</td>
<td align="right" style="font-size:11pt; color:#808080; font-family:Aptos, Calibri, Arial, sans-serif;">{{ date_display }}</td>
</tr>
<tr>
<td style="font-size:11pt; color:#808080; padding-top:4px; font-family:Aptos, Calibri, Arial, sans-serif;">{{ time_display }} Athens</td>
<td align="right" style="font-size:11pt; color:#808080; padding-top:4px; font-family:Aptos, Calibri, Arial, sans-serif;">{{ article_count }} articles</td>
</tr>
</table>
</td>
</tr>

<!-- Executive Brief -->
<tr>
<td style="padding:24px 30px 20px 30px; font-family:Aptos, Calibri, Arial, sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f8f9fa; border-left:4px solid #2c3e50;">
<tr>
<td style="padding:16px 20px; font-family:Aptos, Calibri, Arial, sans-serif;">
<span style="font-size:11pt; font-weight:bold; color:#2c3e50; text-transform:uppercase; letter-spacing:1px;">Executive Brief</span><br><br>
{% for bullet in executive_brief %}
<span style="color:#404040; font-size:12pt;">&bull; {{ bullet }}</span><br>
{% if not loop.last %}<br>{% endif %}
{% endfor %}
</td>
</tr>
</table>
</td>
</tr>

<!-- What Changed -->
{% if what_changed %}
<tr>
<td style="padding:0 30px 20px 30px; font-family:Aptos, Calibri, Arial, sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#fff8e1; border-left:4px solid #f9a825;">
<tr>
<td style="padding:12px 20px; font-family:Aptos, Calibri, Arial, sans-serif;">
<span style="font-size:10pt; font-weight:bold; color:#f57f17; text-transform:uppercase; letter-spacing:1px;">What Changed</span><br><br>
{% for delta in what_changed %}
<span style="color:#404040; font-size:11pt;">&bull; {{ delta }}</span><br>
{% if not loop.last %}<br>{% endif %}
{% endfor %}
</td>
</tr>
</table>
</td>
</tr>
{% endif %}

<!-- Category Sections -->
{% for section in sections %}
<tr>
<td style="padding:0 30px 20px 30px; font-family:Aptos, Calibri, Arial, sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0"{% if section.high_value %} style="border-left:4px solid #1565c0;"{% endif %}>
<tr>
<td style="padding:{% if section.high_value %}16px 20px{% else %}16px 0{% endif %}; font-family:Aptos, Calibri, Arial, sans-serif;">

<!-- Section header -->
<span style="font-size:13pt; font-weight:bold; color:#2c3e50; font-family:Aptos, Calibri, Arial, sans-serif;">{{ section.display_name }}</span>
{% if section.high_value %}<span style="font-size:9pt; color:#1565c0; font-weight:bold; padding-left:8px;">HIGH VALUE</span>{% endif %}
<br><br>

<!-- Synthesis -->
<span style="color:#404040; font-size:12pt; line-height:1.5;">{{ section.synthesis }}</span><br>

<!-- Opposing views -->
{% if section.opposing_views %}
<br>
<span style="font-size:10pt; color:#7b1fa2; font-weight:bold;">Opposing Views:</span><br>
<span style="color:#404040; font-size:11pt;">{{ section.opposing_views }}</span><br>
{% endif %}

<!-- Fact check -->
{% if section.fact_check %}
<br>
<span style="font-size:10pt; color:#e65100; font-weight:bold;">Fact Check:</span><br>
<span style="color:#404040; font-size:11pt;">{{ section.fact_check }}</span><br>
{% endif %}

<!-- Sources -->
<br>
<span style="font-size:9pt; color:#9e9e9e;">Sources: {{ section.sources | join(', ') }}</span>

</td>
</tr>
</table>
<!-- Section divider -->
<table width="100%" cellpadding="0" cellspacing="0" border="0">
<tr><td style="border-bottom:1px solid #eeeeee; padding-top:4px;">&nbsp;</td></tr>
</table>
</td>
</tr>
{% endfor %}

<!-- Fallback text (when synthesis fails) -->
{% if fallback_text %}
<tr>
<td style="padding:0 30px 20px 30px; font-family:Aptos, Calibri, Arial, sans-serif;">
<span style="color:#404040; font-size:11pt; white-space:pre-line;">{{ fallback_text }}</span>
</td>
</tr>
{% endif %}

<!-- Footer -->
<tr>
<td style="padding:20px 30px; border-top:1px solid #e0e0e0; font-family:Aptos, Calibri, Arial, sans-serif;">
<span style="font-size:9pt; color:#9e9e9e;">{{ article_count }} articles processed from {{ source_count }} sources<br>
{% if next_digest %}Next digest: {{ next_digest }}{% endif %}</span>
</td>
</tr>

</table>
<!-- End main container -->

</td></tr>
</table>

</body>
</html>
```

- [ ] **Step 2: Commit**

```bash
git add templates/digest.html
git commit -m "feat: add Outlook-safe Jinja2 email template"
```

---

### Task 9: Delivery (HTML Rendering + Gmail)

**Files:**
- Create: `news/deliver.py`
- Create: `tests/test_deliver.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_deliver.py
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from news.deliver import render_digest_html, build_subject, send_email, save_fallback


def _sample_synthesis():
    return {
        "executive_brief": ["Point 1", "Point 2", "Point 3", "Point 4", "Point 5"],
        "what_changed": ["Markets shifted on ECB news"],
        "sections": [
            {
                "category": "banking",
                "display_name": "Banking — NBG & Sector",
                "synthesis": "NBG reported strong Q1.",
                "opposing_views": "",
                "fact_check": "",
                "sources": ["Reuters", "FT"],
                "high_value": True,
            },
        ],
    }


def test_render_digest_html_produces_valid_html():
    synthesis = _sample_synthesis()
    html = render_digest_html(
        synthesis=synthesis,
        article_count=42,
        source_count=18,
        time_display="09:00",
        date_display="Sat 5 Apr",
        next_digest="13:00",
    )
    assert "<html" in html
    assert "NEWS DIGEST" in html
    assert "Point 1" in html
    assert "Banking" in html
    assert "42 articles" in html
    # Outlook compatibility checks
    assert "<table" in html
    assert "<p>" not in html  # No <p> tags
    assert "Aptos" in html


def test_render_digest_html_skips_empty_what_changed():
    synthesis = _sample_synthesis()
    synthesis["what_changed"] = []
    html = render_digest_html(
        synthesis=synthesis,
        article_count=10,
        source_count=5,
        time_display="13:00",
        date_display="Sat 5 Apr",
    )
    assert "What Changed" not in html


def test_build_subject_scheduled():
    dt = datetime(2026, 4, 5, 9, 0, tzinfo=timezone.utc)
    subject = build_subject(dt, is_adhoc=False)
    assert subject == "news digest — 09:00 sat 5 apr"
    assert subject == subject.lower()


def test_build_subject_adhoc():
    dt = datetime(2026, 4, 5, 15, 42, tzinfo=timezone.utc)
    subject = build_subject(dt, is_adhoc=True)
    assert "ad hoc" in subject
    assert "15:42" in subject


def test_build_subject_partial_sources():
    dt = datetime(2026, 4, 5, 9, 0, tzinfo=timezone.utc)
    subject = build_subject(dt, is_adhoc=False, partial_sources=True)
    assert "partial sources" in subject


def test_send_email_calls_gmail_script():
    with patch("news.deliver.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = send_email(
            subject="test subject",
            html_body="<html>test</html>",
            recipient="test@example.com",
            gmail_script="/path/to/script.js",
        )
        assert result is True
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "node" in call_args[0]
        assert "--html" in call_args


def test_save_fallback_writes_file(tmp_path):
    html = "<html><body>test</body></html>"
    filepath = save_fallback(html, output_dir=str(tmp_path))
    assert Path(filepath).exists()
    assert Path(filepath).read_text() == html
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_deliver.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'news.deliver'`

- [ ] **Step 3: Implement delivery**

```python
# news/deliver.py
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader

from news.config import get_settings

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATES_DIR = _PROJECT_ROOT / "templates"
_ATHENS_TZ = ZoneInfo("Europe/Athens")


def render_digest_html(
    synthesis: dict,
    article_count: int,
    source_count: int,
    time_display: str,
    date_display: str,
    next_digest: str | None = None,
    subject: str = "",
) -> str:
    """Render synthesis data into Outlook-safe HTML using Jinja2 template."""
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=True,
    )
    template = env.get_template("digest.html")

    return template.render(
        subject=subject,
        executive_brief=synthesis.get("executive_brief", []),
        what_changed=synthesis.get("what_changed", []),
        sections=synthesis.get("sections", []),
        fallback_text=synthesis.get("_fallback_text", ""),
        article_count=article_count,
        source_count=source_count,
        time_display=time_display,
        date_display=date_display,
        next_digest=next_digest,
    )


def build_subject(
    dt: datetime,
    is_adhoc: bool = False,
    partial_sources: bool = False,
    synthesis_failed: bool = False,
) -> str:
    """Build the email subject line (always lowercase)."""
    time_str = dt.strftime("%H:%M")
    date_str = dt.strftime("%a %-d %b").lower()

    if is_adhoc:
        subject = f"news digest — ad hoc {time_str} {date_str}"
    else:
        subject = f"news digest — {time_str} {date_str}"

    if partial_sources:
        subject += " — partial sources"
    elif synthesis_failed:
        subject += " — synthesis unavailable"

    return subject.lower()


def send_email(
    subject: str,
    html_body: str,
    recipient: str,
    gmail_script: str,
) -> bool:
    """Send HTML email via gmail-operations.js. Returns True on success."""
    gmail_script = str(Path(gmail_script).expanduser())
    cmd = [
        "node", gmail_script, "send",
        "--to", recipient,
        "--subject", subject,
        "--body", "News digest (view in HTML-capable client)",
        "--html", html_body,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            logger.info("Email sent to %s: %s", recipient, subject)
            return True
        logger.error("Gmail send failed (code %d): %s", result.returncode, result.stderr)
        return False
    except Exception as e:
        logger.error("Gmail send error: %s", e)
        return False


def save_fallback(html: str, output_dir: str = "~/Downloads") -> str:
    """Save HTML to file as fallback when email fails. Returns filepath."""
    output_dir = Path(output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(_ATHENS_TZ)
    filename = now.strftime("%Y%m%d%H%M") + "_news_digest.html"
    filepath = output_dir / filename
    filepath.write_text(html, encoding="utf-8")
    logger.info("Fallback saved to %s", filepath)
    return str(filepath)


def notify_macos(title: str, message: str) -> None:
    """Send a macOS notification via osascript."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "{title}"'],
            timeout=5,
        )
    except Exception:
        pass  # Best-effort notification
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_deliver.py -v
```

Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add news/deliver.py tests/test_deliver.py
git commit -m "feat: add email delivery with Jinja2 rendering and Gmail sending"
```

---

### Task 10: Auth Check

**Files:**
- Create: `news/auth.py`

- [ ] **Step 1: Implement auth checker**

```python
# news/auth.py
import logging
import subprocess

logger = logging.getLogger(__name__)


def check_gcloud_auth() -> bool:
    """Check if gcloud auth tokens are valid for Vertex AI access.

    Returns True if auth is valid, False if expired or unavailable.
    """
    try:
        result = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            logger.info("gcloud auth: valid")
            return True
        logger.warning("gcloud auth: invalid or expired")
        return False
    except FileNotFoundError:
        logger.warning("gcloud CLI not found")
        return False
    except Exception as e:
        logger.warning("gcloud auth check failed: %s", e)
        return False


def send_auth_failure_notification(
    recipient: str, gmail_script: str
) -> None:
    """Send a plain-text notification that Vertex auth has expired."""
    from news.deliver import send_email
    send_email(
        subject="news digest — auth expired, action required",
        html_body=(
            '<table width="600" cellpadding="0" cellspacing="0" border="0">'
            '<tr><td style="font-family:Aptos,Calibri,Arial,sans-serif; font-size:12pt; color:#404040; padding:20px;">'
            'The news digest could not run because gcloud authentication has expired.<br><br>'
            'To fix, run this command in your terminal:<br><br>'
            '<span style="font-family:monospace; background-color:#f5f5f5; padding:4px 8px;">'
            'gcloud auth login</span><br><br>'
            'The digest will resume automatically at the next scheduled time.'
            '</td></tr></table>'
        ),
        recipient=recipient,
        gmail_script=gmail_script,
    )
```

- [ ] **Step 2: Commit**

```bash
git add news/auth.py
git commit -m "feat: add gcloud auth checker with failure notification"
```

---

### Task 11: Orchestrator

**Files:**
- Create: `main.py`
- Create: `tests/test_orchestrator.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_orchestrator.py
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from main import (
    acquire_lock,
    release_lock,
    get_time_window,
    get_next_digest_time,
    log_run,
    run_pipeline,
)


def test_acquire_and_release_lock(tmp_path):
    lock_file = tmp_path / "test.lock"
    assert acquire_lock(str(lock_file)) is True
    assert lock_file.exists()
    # Second acquire should fail
    assert acquire_lock(str(lock_file)) is False
    release_lock(str(lock_file))
    assert not lock_file.exists()


def test_get_time_window():
    now = datetime(2026, 4, 5, 13, 0, tzinfo=timezone.utc)
    last_digest_at = datetime(2026, 4, 5, 9, 0, tzinfo=timezone.utc)
    window = get_time_window(now, last_digest_at, "Europe/Athens")
    assert "09:00" in window or "12:00" in window  # depends on TZ conversion
    assert "April" in window or "Apr" in window


def test_get_next_digest_time():
    # At 09:01, next should be 13:00
    now_str = "09:01"
    schedule = ["09:00", "13:00", "17:00", "21:00"]
    assert get_next_digest_time(now_str, schedule) == "13:00"

    # At 21:01, next should be 09:00 (tomorrow)
    assert get_next_digest_time("21:01", schedule) == "09:00"


def test_log_run(tmp_path):
    log_path = tmp_path / "runs.log"
    log_run(
        log_path=str(log_path),
        run_type="scheduled",
        article_count=42,
        new_count=12,
        synthesis_ok=True,
        sent_ok=True,
        duration_seconds=8.3,
    )
    content = log_path.read_text()
    assert "scheduled" in content
    assert "42" in content
    assert "synthesis OK" in content
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_orchestrator.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'main'`

- [ ] **Step 3: Implement orchestrator**

```python
# main.py
"""News Reader — Orchestrator

Entry point for both scheduled and ad-hoc digest runs.
Usage:
    python main.py --scheduled    # Scheduled run (from launchd)
    python main.py --adhoc        # Ad-hoc run (from /newsfeed command)
"""
import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from news.config import get_sources, get_categories, get_settings
from news.storage import get_connection, init_db, insert_article, get_articles_since, get_last_digest, insert_digest, update_digest_sent, get_article_by_hash
from news.fetcher import fetch_rss_feeds
from news.processor import process_articles
from news.synthesizer import synthesize
from news.deliver import render_digest_html, build_subject, send_email, save_fallback, notify_macos
from news.auth import check_gcloud_auth, send_auth_failure_notification
from news.models import Digest

logger = logging.getLogger("newsreader")
_ATHENS_TZ = ZoneInfo("Europe/Athens")
_PROJECT_ROOT = Path(__file__).resolve().parent


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def acquire_lock(lock_path: str) -> bool:
    """Try to acquire a PID lock. Returns True if acquired."""
    lock = Path(lock_path)
    if lock.exists():
        try:
            pid = int(lock.read_text().strip())
            # Check if PID is still running
            os.kill(pid, 0)
            return False  # Process is running
        except (ProcessLookupError, ValueError):
            lock.unlink()  # Stale lock
    lock.write_text(str(os.getpid()))
    return True


def release_lock(lock_path: str) -> None:
    """Release the PID lock."""
    lock = Path(lock_path)
    if lock.exists():
        lock.unlink()


def get_time_window(
    now: datetime, last_digest_at: datetime | None, tz_name: str = "Europe/Athens"
) -> str:
    """Build a human-readable time window string for the synthesis prompt."""
    tz = ZoneInfo(tz_name)
    now_local = now.astimezone(tz)
    if last_digest_at:
        last_local = last_digest_at.astimezone(tz)
        return f"{last_local.strftime('%H:%M')}–{now_local.strftime('%H:%M')} Athens, {now_local.strftime('%B %-d %Y')}"
    return f"Up to {now_local.strftime('%H:%M')} Athens, {now_local.strftime('%B %-d %Y')}"


def get_next_digest_time(current_time_str: str, schedule: list[str]) -> str:
    """Given current HH:MM, return the next scheduled time."""
    for t in schedule:
        if t > current_time_str:
            return t
    return schedule[0]  # Wrap to tomorrow


def log_run(
    log_path: str,
    run_type: str,
    article_count: int,
    new_count: int,
    synthesis_ok: bool,
    sent_ok: bool,
    duration_seconds: float,
) -> None:
    """Append a one-line run summary to the log file."""
    log = Path(log_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(_ATHENS_TZ).isoformat()
    synth = "synthesis OK" if synthesis_ok else "synthesis FAILED"
    send = "sent OK" if sent_ok else "send FAILED"
    line = f"{now} | {run_type} | {article_count} articles | {new_count} new | {synth} | {send} | {duration_seconds:.1f}s\n"
    with open(log, "a") as f:
        f.write(line)


async def run_pipeline(run_type: str = "scheduled") -> None:
    """Execute the full news digest pipeline."""
    start = time.monotonic()
    settings = get_settings()
    sources_config = get_sources()
    categories_config = get_categories()

    db_path = _PROJECT_ROOT / settings["storage"]["db_path"]
    log_path = _PROJECT_ROOT / settings["storage"]["run_log_path"]
    email_settings = settings["email"]
    scoring = settings["scoring"]
    pipeline_settings = settings["pipeline"]
    synth_settings = settings["synthesis"]
    schedule = settings["schedule"]

    # Connect to DB
    conn = get_connection(db_path)
    init_db(conn)

    # Check auth
    if not check_gcloud_auth():
        send_auth_failure_notification(
            email_settings["recipient"], email_settings["gmail_script"]
        )
        logger.error("Auth check failed, aborting")
        conn.close()
        return

    # Determine time window
    now = datetime.now(timezone.utc)
    last_digest = get_last_digest(conn)
    last_digest_at = last_digest.created_at if last_digest else None
    time_window = get_time_window(now, last_digest_at, schedule["timezone"])

    # Previous highlights for continuity
    previous_highlights = []
    if last_digest and last_digest.synthesis_text:
        try:
            prev = __import__("json").loads(last_digest.synthesis_text)
            previous_highlights = prev.get("executive_brief", [])[:3]
        except Exception:
            pass

    # === FETCH ===
    logger.info("=== FETCH ===")
    rss_articles, rss_errors = await fetch_rss_feeds(sources_config["rss_feeds"])
    total_fetched = len(rss_articles)
    total_errors = len(rss_errors)
    all_articles = rss_articles

    # === PROCESS ===
    logger.info("=== PROCESS ===")
    existing_hashes = set()
    for row in conn.execute("SELECT content_hash FROM articles").fetchall():
        existing_hashes.add(row["content_hash"])

    source_tiers = {s["name"]: s["tier"] for s in sources_config["rss_feeds"]}

    processed, stats = process_articles(
        all_articles,
        existing_hashes,
        categories_config,
        scoring,
        source_tiers,
        min_words=pipeline_settings["min_article_length_words"],
        max_age_hours=pipeline_settings["max_article_age_hours"],
    )

    # === STORE ===
    logger.info("=== STORE ===")
    new_count = 0
    for article in processed:
        if insert_article(conn, article):
            new_count += 1

    # === SYNTHESIZE ===
    logger.info("=== SYNTHESIZE ===")
    threshold = pipeline_settings["relevance_threshold"]
    max_per_cat = pipeline_settings["max_articles_per_category"]
    since = last_digest_at or (now - timedelta(hours=24))
    digest_articles = get_articles_since(conn, since, min_score=threshold)

    # Group by category
    articles_by_category: dict[str, list] = {}
    for a in digest_articles:
        for cat in a.categories:
            articles_by_category.setdefault(cat, [])
            if len(articles_by_category[cat]) < max_per_cat:
                articles_by_category[cat].append(a)

    cat_display_names = {
        cid: cdef["display_name"]
        for cid, cdef in categories_config["categories"].items()
    }

    synthesis_data, synthesis_ok = synthesize(
        articles_by_category,
        cat_display_names,
        previous_highlights,
        time_window,
        claude_command=synth_settings["claude_command"],
        claude_args=synth_settings["claude_args"],
        timeout=synth_settings["timeout_seconds"],
        max_retries=synth_settings["max_retries"],
    )

    # === DELIVER ===
    logger.info("=== DELIVER ===")
    now_athens = now.astimezone(_ATHENS_TZ)
    time_display = now_athens.strftime("%H:%M")
    date_display = now_athens.strftime("%a %-d %b")

    total_sources = len(set(a.source for a in digest_articles))
    partial = total_errors > len(sources_config["rss_feeds"]) / 2

    next_time = get_next_digest_time(
        time_display, schedule["runs"]
    ) if run_type == "scheduled" else None

    subject = build_subject(
        now_athens,
        is_adhoc=(run_type == "adhoc"),
        partial_sources=partial,
        synthesis_failed=not synthesis_ok,
    )

    html = render_digest_html(
        synthesis=synthesis_data,
        article_count=len(digest_articles),
        source_count=total_sources,
        time_display=time_display,
        date_display=date_display,
        next_digest=next_time,
        subject=subject,
    )

    # Record digest
    digest = Digest(
        digest_type=run_type,
        article_count=len(digest_articles),
        synthesis_text=__import__("json").dumps(synthesis_data, ensure_ascii=False),
        html_output=html,
    )
    digest_id = insert_digest(conn, digest)

    # Send email
    sent_ok = send_email(
        subject=subject,
        html_body=html,
        recipient=email_settings["recipient"],
        gmail_script=email_settings["gmail_script"],
    )

    if sent_ok:
        update_digest_sent(conn, digest_id)
    else:
        filepath = save_fallback(html)
        notify_macos("News Digest", f"Email failed. Saved to {filepath}")

    # Log run
    duration = time.monotonic() - start
    log_run(
        log_path=str(log_path),
        run_type=run_type,
        article_count=len(digest_articles),
        new_count=new_count,
        synthesis_ok=synthesis_ok,
        sent_ok=sent_ok,
        duration_seconds=duration,
    )

    conn.close()
    logger.info("Pipeline complete in %.1fs", duration)


def main():
    setup_logging()
    parser = argparse.ArgumentParser(description="News Reader Digest Pipeline")
    parser.add_argument("--scheduled", action="store_true", help="Scheduled run")
    parser.add_argument("--adhoc", action="store_true", help="Ad-hoc run")
    args = parser.parse_args()

    run_type = "adhoc" if args.adhoc else "scheduled"

    lock_path = str(_PROJECT_ROOT / "data" / "newsreader.lock")
    if not acquire_lock(lock_path):
        logger.warning("Another instance is running, exiting")
        sys.exit(0)

    try:
        asyncio.run(run_pipeline(run_type))
    finally:
        release_lock(lock_path)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_orchestrator.py -v
```

Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_orchestrator.py
git commit -m "feat: add orchestrator with pre-flight checks, pipeline coordination, and logging"
```

---

### Task 12: Scheduling (launchd)

**Files:**
- Create: `launchd/com.news.digest.0900.plist`
- Create: `launchd/com.news.digest.1300.plist`
- Create: `launchd/com.news.digest.1700.plist`
- Create: `launchd/com.news.digest.2100.plist`
- Create: `scripts/install_launchd.sh`

- [ ] **Step 1: Create the 09:00 plist (06:00 UTC)**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.news.digest.0900</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/plessas/SourceCode/news/.venv/bin/python3</string>
        <string>/Users/plessas/SourceCode/news/main.py</string>
        <string>--scheduled</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>6</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>WorkingDirectory</key>
    <string>/Users/plessas/SourceCode/news</string>
    <key>StandardOutPath</key>
    <string>/Users/plessas/SourceCode/news/data/launchd-0900.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/plessas/SourceCode/news/data/launchd-0900.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
```

- [ ] **Step 2: Create the 13:00 plist (10:00 UTC)**

Same structure as above with label `com.news.digest.1300`, Hour `10`, and log files `launchd-1300.*`.

- [ ] **Step 3: Create the 17:00 plist (14:00 UTC)**

Same structure with label `com.news.digest.1700`, Hour `14`, and log files `launchd-1700.*`.

- [ ] **Step 4: Create the 21:00 plist (18:00 UTC)**

Same structure with label `com.news.digest.2100`, Hour `18`, and log files `launchd-2100.*`.

- [ ] **Step 5: Create install script**

```bash
#!/bin/bash
# scripts/install_launchd.sh
# Install or uninstall news digest launchd jobs

set -euo pipefail

PLIST_DIR="$(cd "$(dirname "$0")/../launchd" && pwd)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

install() {
    echo "Installing launchd jobs..."
    for plist in "$PLIST_DIR"/com.news.digest.*.plist; do
        name=$(basename "$plist")
        cp "$plist" "$LAUNCH_AGENTS_DIR/$name"
        launchctl load "$LAUNCH_AGENTS_DIR/$name"
        echo "  Installed: $name"
    done
    echo "Done. Digest will run at 09:00, 13:00, 17:00, 21:00 Athens time."
}

uninstall() {
    echo "Uninstalling launchd jobs..."
    for plist in "$LAUNCH_AGENTS_DIR"/com.news.digest.*.plist; do
        if [ -f "$plist" ]; then
            launchctl unload "$plist" 2>/dev/null || true
            rm "$plist"
            echo "  Removed: $(basename "$plist")"
        fi
    done
    echo "Done."
}

status() {
    echo "News digest launchd status:"
    launchctl list | grep "com.news.digest" || echo "  No jobs loaded."
}

case "${1:-install}" in
    install)   install ;;
    uninstall) uninstall ;;
    status)    status ;;
    *)         echo "Usage: $0 {install|uninstall|status}" ;;
esac
```

- [ ] **Step 6: Make install script executable and commit**

```bash
chmod +x scripts/install_launchd.sh
git add launchd/ scripts/install_launchd.sh
git commit -m "feat: add launchd scheduling for 4 daily digest runs"
```

---

### Task 13: Health Check

**Files:**
- Create: `scripts/health_check.py`

- [ ] **Step 1: Implement health check**

```python
#!/usr/bin/env python3
"""Weekly health check — verifies digest runs completed as expected.

Usage: python scripts/health_check.py
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOG_PATH = _PROJECT_ROOT / "data" / "runs.log"
_EXPECTED_DAILY_RUNS = 4
_ATHENS_TZ = ZoneInfo("Europe/Athens")


def check_week(log_path: Path = _LOG_PATH, days: int = 7) -> dict:
    """Check the last N days of runs. Returns stats dict."""
    if not log_path.exists():
        return {"status": "error", "message": "No run log found", "runs": 0, "expected": days * _EXPECTED_DAILY_RUNS}

    cutoff = datetime.now(_ATHENS_TZ) - timedelta(days=days)
    runs = 0
    failures = 0

    for line in log_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            ts_str = line.split(" | ")[0].strip()
            ts = datetime.fromisoformat(ts_str)
            if ts < cutoff:
                continue
            runs += 1
            if "FAILED" in line:
                failures += 1
        except (ValueError, IndexError):
            continue

    expected = days * _EXPECTED_DAILY_RUNS
    missed = expected - runs

    if missed > 3:
        status = "warning"
    elif missed > 0:
        status = "ok"
    else:
        status = "healthy"

    return {
        "status": status,
        "runs": runs,
        "expected": expected,
        "missed": missed,
        "failures": failures,
    }


def main():
    result = check_week()
    print(f"Status: {result['status']}")
    print(f"Runs: {result['runs']}/{result['expected']} (missed: {result['missed']})")
    if result['failures']:
        print(f"Failures: {result['failures']}")

    if result["status"] == "warning":
        print("WARNING: >3 runs missed this week. Check launchd status and auth.")
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit**

```bash
git add scripts/health_check.py
git commit -m "feat: add weekly health check script"
```

---

### Task 14: /newsfeed Claude Code Skill

**Files:**
- Skill to be created in the appropriate plugin location

- [ ] **Step 1: Determine skill location**

The `/newsfeed` skill should be added to an existing plugin or created as a standalone. Since this complements the trading hub, it can go in the trading marketplace or as a standalone skill. For now, create a minimal skill definition.

Check the plugin structure:
```bash
ls ~/SourceCode/trading-marketplace/plugins/trading-hub/skills/
```

- [ ] **Step 2: Create the newsfeed skill**

Create as a new skill in the trading hub plugin (or a standalone plugin — adjust path based on Step 1 findings):

```markdown
---
name: newsfeed
description: Run the news intelligence digest pipeline on demand. Fetches latest news from RSS/API/WebSearch, deduplicates, synthesizes via Claude AI, and delivers an HTML email digest to plessas@nbg.gr
user_invocable: true
---

# News Feed Digest

Run the news reader pipeline to generate an on-demand digest.

## Steps

1. Run the pipeline:
```bash
cd ~/SourceCode/news && source .venv/bin/activate && python3 main.py --adhoc
```

2. If the pipeline succeeds, report the result to the user.
3. If the pipeline fails, report the error and suggest troubleshooting:
   - Auth issue: suggest `gcloud auth login`
   - Network issue: check connectivity
   - Other: check `data/runs.log` for details
```

- [ ] **Step 3: Register the skill in plugin.json or equivalent**

Add the skill reference to the plugin manifest so `/newsfeed` is recognized by Claude Code.

- [ ] **Step 4: Commit**

```bash
git add <skill-file-path>
git commit -m "feat: add /newsfeed Claude Code skill for on-demand digest"
```

---

### Task 15: Integration Test & First Run

**Files:**
- Modify: `tests/test_orchestrator.py` (add integration test)

- [ ] **Step 1: Run all unit tests**

```bash
cd ~/SourceCode/news
source .venv/bin/activate
pytest -v
```

Expected: All tests pass

- [ ] **Step 2: Initialize the database**

```bash
python3 -c "
from news.storage import get_connection, init_db
conn = get_connection('data/news.db')
init_db(conn)
print('Database initialized')
conn.close()
"
```

- [ ] **Step 3: Test RSS fetching in isolation**

```bash
python3 -c "
import asyncio
from news.config import get_sources
from news.fetcher import fetch_rss_feeds

sources = get_sources()
articles, errors = asyncio.run(fetch_rss_feeds(sources['rss_feeds'][:3]))
print(f'Fetched {len(articles)} articles, {len(errors)} errors')
for a in articles[:5]:
    print(f'  - {a.title} ({a.source})')
"
```

- [ ] **Step 4: Run full ad-hoc pipeline**

```bash
python3 main.py --adhoc
```

Check:
- Email arrives at plessas@nbg.gr
- HTML renders properly in Outlook Mac
- No errors in `data/runs.log`

- [ ] **Step 5: Verify run log entry**

```bash
cat data/runs.log
```

Expected: One line with `adhoc | N articles | ... | sent OK`

- [ ] **Step 6: Commit any fixes from integration testing**

```bash
git add -A
git commit -m "fix: integration test adjustments from first run"
```

---

### Task 16: Phase 2 Enhancements (NewsAPI, WebSearch, langdetect, archive cleanup)

**Files:**
- Modify: `news/fetcher.py` — add `fetch_newsapi()` and `fetch_websearch()` functions
- Modify: `news/processor.py` — add `detect_language()` using langdetect library
- Modify: `news/storage.py` — add `archive_old_articles()` function
- Modify: `main.py` — integrate new fetcher functions and archive cleanup
- Modify: `tests/test_fetcher.py`, `tests/test_processor.py`, `tests/test_storage.py`

- [ ] **Step 1: Add NewsAPI fetcher**

Add to `news/fetcher.py`:

```python
async def fetch_newsapi(
    keywords: list[dict],
    api_key: str | None = None,
    base_url: str = "https://newsapi.org/v2",
    page_size: int = 20,
) -> tuple[list[Article], list[str]]:
    """Fetch articles from NewsAPI by keyword queries."""
    api_key = api_key or os.environ.get("NEWSAPI_KEY")
    if not api_key:
        logger.warning("NEWSAPI_KEY not set, skipping NewsAPI")
        return [], ["NewsAPI: no API key configured"]

    all_articles: list[Article] = []
    errors: list[str] = []

    async with httpx.AsyncClient() as client:
        for kw in keywords:
            try:
                resp = await client.get(
                    f"{base_url}/everything",
                    params={
                        "q": kw["query"],
                        "pageSize": page_size,
                        "sortBy": "publishedAt",
                        "language": "en",
                        "apiKey": api_key,
                    },
                    timeout=_REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
                for item in data.get("articles", []):
                    article = Article(
                        url=item.get("url", ""),
                        title=item.get("title", ""),
                        source=item.get("source", {}).get("name", "NewsAPI"),
                        author=item.get("author", ""),
                        published_at=datetime.fromisoformat(
                            item["publishedAt"].replace("Z", "+00:00")
                        ) if item.get("publishedAt") else None,
                        content=item.get("content", item.get("description", "")),
                        summary=item.get("description", ""),
                        categories=[kw["category"]],
                        language="en",
                    )
                    if article.url and article.title:
                        all_articles.append(article)
            except Exception as e:
                errors.append(f"NewsAPI [{kw['category']}]: {e}")

    logger.info("NewsAPI: %d articles, %d errors", len(all_articles), len(errors))
    return all_articles, errors
```

- [ ] **Step 2: Add language detection to processor**

Add to `news/processor.py`:

```python
def detect_language(article: Article) -> None:
    """Detect article language using langdetect. Updates article.language in place."""
    try:
        from langdetect import detect
        detected = detect(article.title + " " + article.content[:500])
        if detected in ("el", "gr"):
            article.language = "gr"
        else:
            article.language = detected
    except Exception:
        pass  # Keep existing language tag from source config
```

Call `detect_language(article)` in `process_articles()` after classification.

- [ ] **Step 3: Add archive cleanup to storage**

Add to `news/storage.py`:

```python
import gzip
from datetime import timedelta

def archive_old_articles(
    conn: sqlite3.Connection,
    archive_dir: str | Path,
    max_age_days: int = 30,
) -> int:
    """Archive and delete articles older than max_age_days. Returns count archived."""
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)

    cutoff = _dt_to_str(datetime.now(timezone.utc) - timedelta(days=max_age_days))
    rows = conn.execute(
        "SELECT * FROM articles WHERE fetched_at < ?", (cutoff,)
    ).fetchall()

    if not rows:
        return 0

    # Write to compressed JSON
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    archive_path = archive_dir / f"articles-{date_str}.json.gz"
    articles_data = [dict(row) for row in rows]

    with gzip.open(archive_path, "wt", encoding="utf-8") as f:
        json.dump(articles_data, f, ensure_ascii=False)

    # Delete from DB
    conn.execute("DELETE FROM articles WHERE fetched_at < ?", (cutoff,))
    conn.execute(
        "DELETE FROM article_categories WHERE article_url NOT IN (SELECT url FROM articles)"
    )
    conn.commit()

    logger.info("Archived %d articles to %s", len(rows), archive_path)
    return len(rows)
```

- [ ] **Step 4: Integrate into orchestrator**

In `main.py`, after the deliver stage in `run_pipeline()`, add:

```python
    # Archive old articles
    from news.storage import archive_old_articles
    archive_dir = _PROJECT_ROOT / settings["storage"]["archive_dir"]
    archived = archive_old_articles(conn, archive_dir, settings["storage"]["archive_after_days"])
    if archived:
        logger.info("Archived %d old articles", archived)
```

- [ ] **Step 5: Write tests for new functions and run**

```bash
pytest -v
```

- [ ] **Step 6: Commit**

```bash
git add news/fetcher.py news/processor.py news/storage.py main.py tests/
git commit -m "feat: add NewsAPI fetcher, language detection, and article archival"
```

---

## Post-Implementation Checklist

After all tasks are complete, verify:

- [ ] All unit tests pass (`pytest -v`)
- [ ] RSS feeds are fetching real articles
- [ ] Deduplication works across runs (run twice, second run has fewer new articles)
- [ ] Claude synthesis produces structured JSON
- [ ] Email arrives and renders cleanly in Outlook Mac
- [ ] Fallback digest works when Claude is unavailable
- [ ] PID lock prevents concurrent runs
- [ ] Auth check sends notification on failure
- [ ] Run log captures each execution
- [ ] `scripts/install_launchd.sh install` loads the 4 scheduled jobs
- [ ] `scripts/install_launchd.sh status` shows all 4 jobs loaded
