import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class Article:
    url: str
    title: str
    source: str
    content: str
    categories: list[str]
    language: str
    tickers: list[str] = field(default_factory=list)
    author: str = ""
    published_at: datetime | None = None
    summary: str = ""
    content_hash: str = ""
    relevance_score: int = 0
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    included_in_digest_id: int | None = None
    also_reported_by: list[str] = field(default_factory=list)
    pipeline: str = "digest"
    sentiment: str = ""
    mention_type: str = ""
    urgency: str = ""

    def compute_hash(self) -> None:
        normalized_title = self.title.strip().lower()
        content_prefix = self.content[:200].strip().lower()
        raw = f"{normalized_title}|{content_prefix}"
        self.content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class Digest:
    digest_type: str
    article_count: int = 0
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    synthesis_text: str = ""
    html_output: str = ""
    sent_at: datetime | None = None
    pipeline: str = "digest"


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
