"""Mac-side YouTube transcript harvester.

YouTube serves caption data to residential IPs and refuses datacenter ones, so
this runs on the Mac rather than on the VPS with the rest of the pipeline.
Measured 2026-08-14: youtube-transcript-api returns 1,137 words from the Mac for
a video whose feed description is ~70 words of mostly sponsor copy, and raises
RequestBlocked ("an IP belonging to a cloud provider") from the Hetzner box.

It fetches transcripts for new videos on the stack profile's channels, distils
each into a factual abstract via the local claude CLI, and rsyncs the resulting
store to the VPS for the pipeline to read.

Run hourly via launchd. Idempotent: a video already settled is never refetched.
"""

import argparse
import logging
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import feedparser
import httpx
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)

# Ensure `news` imports resolve when launchd runs this by path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news.config import get_sources, vertex_cli_model_and_env  # noqa: E402
from news.transcripts import (  # noqa: E402
    init_transcript_db,
    pending_video_ids,
    stored_transcript,
    upsert_transcript,
)

logger = logging.getLogger(__name__)

_YT_FEED_HOST = "youtube.com/feeds/videos.xml"
_FEED_TIMEOUT = 30

# 300s, not 120s. A live run on 2026-08-14 lost one of two distillations to a
# 120s expiry, throwing away a transcript fetch that had already succeeded.
# Nothing here is on a deadline -- the harvester runs hourly on the Mac, hours
# ahead of the 13:00 stack run -- so a generous ceiling costs nothing.
_CLI_TIMEOUT = 300

# The synthesis snippet caps abstracts at this length, so trim to it at store
# time rather than letting stack_synth cut mid-clause. The same live run
# produced 1,119 characters against a 600-800 request; models overrun, and the
# raw transcript is retained anyway so nothing is lost by trimming here.
ABSTRACT_MAX_CHARS = 800

# Bounds the work and the spend in any single hourly run. A backlog drains over
# successive runs rather than firing hundreds of CLI calls at once.
_DEFAULT_LIMIT = 25

# The whole point of the feature: the uploader's description is written to sell
# the click, so we ask for the substance and name the things to throw away.
_DISTIL_PROMPT = """\
Below is the transcript of a technical video titled "{title}".

Write a dense factual abstract of it in 600-800 characters.

Rules:
- Lead with the single most consequential concrete fact.
- Keep specifics: names, versions, numbers, licences, benchmarks, tool names.
- Drop every sponsor read, discount code, subscribe request and channel plug.
- Drop hype framing ("insane", "game-changer", "you won't believe").
- Write plain declarative prose. No preamble, no markdown, no bullet points.
- Output the abstract only.

TRANSCRIPT:
{transcript}
"""


def youtube_channels(sources: dict[str, Any]) -> list[tuple[str, str]]:
    """Extract (feed name, channel id) for every YouTube feed in a sources config.

    Reading the roster from the same config the pipeline fetches means adding a
    channel stays a one-place edit.
    """
    channels: list[tuple[str, str]] = []
    for source in sources.get("rss_feeds", []):
        url = source.get("url", "")
        if _YT_FEED_HOST not in url:
            continue
        channel_id = parse_qs(urlparse(url).query).get("channel_id", [""])[0]
        if channel_id:
            channels.append((source["name"], channel_id))
    return channels


def videos_in_feed(xml: str) -> list[dict[str, str]]:
    """Parse a YouTube Atom feed into video records."""
    feed = feedparser.parse(xml)
    videos = []
    for entry in feed.entries:
        video_id = entry.get("yt_videoid")
        if not video_id:
            continue
        videos.append(
            {
                "video_id": video_id,
                "title": entry.get("title", ""),
                "published": entry.get("published", ""),
            }
        )
    return videos


def trim_to_sentence(text: str, limit: int) -> str:
    """Trim text to at most ``limit`` chars, preferring a sentence boundary.

    Falls back to a hard cut when there is no sentence end inside the limit,
    which only happens for pathological output.
    """
    if len(text) <= limit:
        return text

    cut = text[:limit]
    boundary = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
    if boundary > 0:
        return cut[: boundary + 1]
    return cut


def fetch_transcript(video_id: str) -> tuple[str, str]:
    """Fetch a video's captions. Returns (text, status).

    Status is 'ok', 'no_captions' when the video will never have captions, or
    'fetch_failed' for anything transient such as an IP block or a timeout.
    The distinction matters: no_captions is recorded as terminal, while
    fetch_failed is retried on later runs up to the attempt ceiling.
    """
    try:
        snippets = YouTubeTranscriptApi().fetch(video_id)
        return " ".join(snippet.text for snippet in snippets).strip(), "ok"
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable):
        return "", "no_captions"
    except Exception as e:
        logger.warning(f"{video_id}: transcript fetch failed: {type(e).__name__}: {e}")
        return "", "fetch_failed"


def distil(transcript: str, title: str) -> str:
    """Distil a transcript into a factual abstract via the local claude CLI.

    Routed through the CLI (Vertex) rather than any SDK, per project policy.
    Returns "" on any failure; the caller records summary_failed and keeps the
    raw transcript so only the cheap half is retried.
    """
    prompt = _DISTIL_PROMPT.format(title=title, transcript=transcript)
    model, run_env = vertex_cli_model_and_env("sonnet")
    try:
        result = subprocess.run(
            ["claude", "--model", model, "--print"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT,
            env=run_env,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning(f"distillation call failed: {type(e).__name__}: {e}")
        return ""

    if result.returncode != 0:
        # Vertex errors (notably a 429 on an unprovisioned model/region pairing)
        # arrive on stdout with stderr empty, so logging stderr alone produces a
        # message with no diagnostic in it at all.
        detail = (result.stderr or "").strip() or (result.stdout or "").strip()
        logger.warning(f"claude CLI exited {result.returncode}: {detail[:300]}")
        return ""

    return (result.stdout or "").strip()


def harvest(
    sources: dict[str, Any], db_path: str | Path, limit: int = _DEFAULT_LIMIT
) -> dict[str, int]:
    """Fetch and distil transcripts for new videos. Returns run counts."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_transcript_db(conn)

    stats = {"channels": 0, "attempted": 0, "ok": 0, "no_captions": 0, "failed": 0}

    try:
        for name, channel_id in youtube_channels(sources):
            stats["channels"] += 1
            url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
            try:
                response = httpx.get(url, timeout=_FEED_TIMEOUT, follow_redirects=True)
                response.raise_for_status()
            except Exception as e:
                # One dead channel must not abort the run.
                logger.warning(f"{name}: feed fetch failed: {type(e).__name__}: {e}")
                continue

            by_id = {v["video_id"]: v for v in videos_in_feed(response.text)}
            for video_id in pending_video_ids(conn, list(by_id)):
                if stats["attempted"] >= limit:
                    logger.info(f"Run limit {limit} reached; the rest waits for the next run")
                    return stats

                video = by_id[video_id]
                stats["attempted"] += 1

                # Reuse a transcript we already paid to fetch. This is what makes
                # a summary_failed retry cheap, and it stops a refetch that fails
                # from overwriting good text with an empty string.
                text = stored_transcript(conn, video_id)
                status = "ok" if text else ""
                if not text:
                    text, status = fetch_transcript(video_id)

                # An 'ok' with no words is a caption track that exists but is
                # empty; distilling it would send the model an empty prompt.
                if status == "ok" and not text.strip():
                    text, status = "", "no_captions"

                abstract = ""
                if status == "ok":
                    abstract = trim_to_sentence(distil(text, video["title"]), ABSTRACT_MAX_CHARS)
                    if not abstract:
                        status = "summary_failed"

                upsert_transcript(
                    conn,
                    video_id,
                    name,
                    video["title"],
                    video["published"],
                    text,
                    abstract,
                    status,
                )

                if status == "ok":
                    stats["ok"] += 1
                elif status == "no_captions":
                    stats["no_captions"] += 1
                else:
                    stats["failed"] += 1
    finally:
        conn.close()

    return stats


def push_to_vps(db_path: Path, remote: str) -> bool:
    """rsync the store to the VPS. Returns True on success."""
    try:
        result = subprocess.run(
            ["rsync", "-az", "--timeout=30", str(db_path), remote],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.error(f"rsync failed: {type(e).__name__}: {e}")
        return False

    if result.returncode != 0:
        logger.error(f"rsync exited {result.returncode}: {(result.stderr or '')[:200]}")
        return False
    return True


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="Harvest YouTube transcripts for a profile")
    parser.add_argument("--profile", default="stack")
    parser.add_argument("--limit", type=int, default=_DEFAULT_LIMIT)
    parser.add_argument("--db", default=str(Path.home() / "SourceCode/news/data/transcripts.db"))
    parser.add_argument("--remote", default="vps:~/SourceCode/news/data/transcripts.db")
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args()

    stats = harvest(get_sources(profile=args.profile), Path(args.db), limit=args.limit)
    logger.info(
        f"Harvest complete: {stats['channels']} channels, {stats['attempted']} attempted, "
        f"{stats['ok']} ok, {stats['no_captions']} no captions, {stats['failed']} failed"
    )

    if args.no_push:
        return 0
    return 0 if push_to_vps(Path(args.db), args.remote) else 1


if __name__ == "__main__":
    sys.exit(main())
