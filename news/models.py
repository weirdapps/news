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
    # Fact-extracted distillation of a video transcript. Deliberately separate
    # from ``content`` so it stays out of compute_hash(): an abstract arriving
    # after the article was first stored would otherwise change the hash and
    # re-insert the same video as a new row.
    transcript_abstract: str = ""
    # Deterministic delta of what a dated changelog entry changed, plus which
    # producer wrote it ("deterministic" at parse time, "llm" once the optional
    # prose upgrade lands). Both stay out of compute_hash() for the same reason
    # as ``transcript_abstract``: a digest arriving after the article was first
    # stored -- and the LLM upgrade can arrive a run later still -- would
    # otherwise change the hash and re-insert the same entry as a new row.
    changelog_digest: str = ""
    changelog_digest_source: str = ""

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
