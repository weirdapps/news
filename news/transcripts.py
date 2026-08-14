"""Transcript store: the bridge between the Mac harvester and the VPS pipeline.

YouTube serves caption data to residential IPs and refuses datacenter ones, so
transcripts are fetched on the Mac and rsynced to the VPS as a standalone SQLite
file. The Mac is the only writer; the pipeline opens it read-only. Keeping it
out of ``news.db`` means neither file ever has two writers, and it leaves the
existing VPS-to-Mac ``news.db`` sync untouched.
"""

import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from news.models import Article

logger = logging.getLogger(__name__)

# A transient failure is retried on later harvester runs up to this many times,
# then left alone rather than retried forever.
#
# 8, not 3. The harvester runs hourly, so this is the number of hours of
# transient failure a video can survive. Both observed failure modes last
# longer than three hours: YouTube answers IpBlocked under load (seen in a live
# run from the Mac), and a Vertex 429 persists until quota frees up. Abandoning
# a video permanently over either is the wrong trade, and retries are cheap now
# that an already-fetched transcript is reused rather than refetched.
MAX_ATTEMPTS = 8

# Statuses that will never change on a later run: the work is done, or the video
# simply has no captions and never will.
_TERMINAL_STATUSES = frozenset({"ok", "no_captions"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcripts (
    video_id        TEXT PRIMARY KEY,
    channel         TEXT NOT NULL,
    title           TEXT NOT NULL,
    published_at    TEXT,
    transcript      TEXT,
    abstract        TEXT,
    status          TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    updated_at      TEXT NOT NULL
);
"""

# Matches both the long watch URL and the youtu.be short form. YouTube ids are
# always 11 characters from the URL-safe alphabet.
_VIDEO_ID_RE = re.compile(r"(?:youtube\.com/watch\?(?:[^\s]*&)?v=|youtu\.be/)([A-Za-z0-9_-]{11})")


def init_transcript_db(conn: sqlite3.Connection) -> None:
    """Create the transcript table if it does not exist."""
    conn.executescript(_SCHEMA)
    conn.commit()


def upsert_transcript(
    conn: sqlite3.Connection,
    video_id: str,
    channel: str,
    title: str,
    published_at: str | None,
    transcript: str,
    abstract: str,
    status: str,
) -> None:
    """Insert or update one video's record, incrementing the attempt counter."""
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO transcripts (
            video_id, channel, title, published_at, transcript, abstract,
            status, attempts, last_attempt_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(video_id) DO UPDATE SET
            channel         = excluded.channel,
            title           = excluded.title,
            published_at    = excluded.published_at,
            transcript      = excluded.transcript,
            abstract        = excluded.abstract,
            status          = excluded.status,
            attempts        = transcripts.attempts + 1,
            last_attempt_at = excluded.last_attempt_at,
            updated_at      = excluded.updated_at
        """,
        (video_id, channel, title, published_at, transcript, abstract, status, now, now),
    )
    conn.commit()


def pending_video_ids(conn: sqlite3.Connection, candidates: list[str]) -> list[str]:
    """Filter candidates down to those still worth attempting."""
    if not candidates:
        return []

    placeholders = ",".join("?" * len(candidates))
    rows = conn.execute(
        f"SELECT video_id, status, attempts FROM transcripts WHERE video_id IN ({placeholders})",
        candidates,
    ).fetchall()

    settled = {
        row["video_id"]
        for row in rows
        if row["status"] in _TERMINAL_STATUSES or row["attempts"] >= MAX_ATTEMPTS
    }
    return [video_id for video_id in candidates if video_id not in settled]


def stored_transcript(conn: sqlite3.Connection, video_id: str) -> str:
    """Return a transcript already held for this video, or "".

    Lets the harvester retry a failed distillation without paying for the fetch
    again, and without risking the stored transcript being overwritten by a
    refetch that fails.
    """
    row = conn.execute(
        "SELECT transcript FROM transcripts WHERE video_id = ?", (video_id,)
    ).fetchone()
    return (row["transcript"] or "") if row else ""


def extract_video_id(url: str) -> str | None:
    """Return the YouTube video id in a URL, or None if it is not a video URL."""
    if not url:
        return None
    match = _VIDEO_ID_RE.search(url)
    return match.group(1) if match else None


def load_abstracts(db_path: str | Path, video_ids: list[str]) -> dict[str, str]:
    """Read abstracts for the given video ids. Returns {} if the store is unusable.

    Opened read-only: the VPS consumes this file and never writes it. A missing
    or corrupt store degrades to no enrichment rather than an error, which
    leaves the pipeline exactly where it was before this feature existed.
    """
    path = Path(db_path)
    if not path.exists() or not video_ids:
        return {}

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        logger.warning(f"Transcript store unreadable at {path}: {e}")
        return {}

    try:
        placeholders = ",".join("?" * len(video_ids))
        rows = conn.execute(
            "SELECT video_id, abstract FROM transcripts "
            "WHERE status = 'ok' AND abstract IS NOT NULL AND abstract != '' "
            f"AND video_id IN ({placeholders})",
            video_ids,
        ).fetchall()
    except sqlite3.Error as e:
        logger.warning(f"Transcript store query failed: {e}")
        return {}
    finally:
        conn.close()

    return dict(rows)


def enrich_articles(articles: list[Article], db_path: str | Path) -> tuple[int, int]:
    """Attach transcript abstracts to YouTube articles in place.

    Returns ``(enriched, total_video_items)`` so the caller can report coverage.
    A gap between the two means the harvester has not caught up, which is the
    expected state when the Mac was asleep.
    """
    video_items = []
    for article in articles:
        video_id = extract_video_id(article.url)
        if video_id:
            video_items.append((article, video_id))

    if not video_items:
        return 0, 0

    abstracts = load_abstracts(db_path, [video_id for _, video_id in video_items])

    enriched = 0
    for article, video_id in video_items:
        abstract = abstracts.get(video_id)
        if abstract:
            article.transcript_abstract = abstract
            enriched += 1

    return enriched, len(video_items)
