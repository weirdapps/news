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
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import feedparser
import httpx
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    VideoUnplayable,
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
from scripts.fence import within  # noqa: E402

logger = logging.getLogger(__name__)

# The store lives in the repo's data/ folder on both hosts, and nowhere else: --db
# must resolve inside it (NEWS_DATA_DIR moves it, for tests), and the push target is
# fixed, so no argument reaches rsync except a fenced local path.
DATA_DIR = Path(os.environ.get("NEWS_DATA_DIR") or Path(__file__).resolve().parent.parent / "data")
VPS_STORE = "vps:~/SourceCode/news/data/transcripts.db"

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
# successive runs rather than firing hundreds of CLI calls at once. Kept at 3
# because bursts of ~10 fetches drew YouTube IP blocks; 3 an hour still clears
# the ~25 a day the roster produces.
_DEFAULT_LIMIT = 3

# After YouTube refuses this IP, stop asking for this long. Hourly runs against an
# active block kept knocking from the very address the block is keyed on: from
# 2026-09-22 22:52 every run for a day went 0 for 25 on IpBlocked, and exited 0.
BLOCK_COOLDOWN = timedelta(hours=6)

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
- If the transcript has too little substance for an abstract (music only, a
  trailer, a few sentences), output exactly NO_CONTENT and nothing else.

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


# How YouTube words a video that exists but has not aired. It has no captions
# YET, so it is neither terminal nor a failure: on 2026-11-09 a premiere 21 hours
# away spent all 8 hourly attempts before it aired and was never harvested.
_NOT_AIRED_PREFIXES = ("Premieres in", "This live event will begin in")

# What the claude CLI prints when the model declines the prompt on policy
# grounds (security talks draw `[cyber]`). The same transcript draws the same
# answer, so retrying it only spends another call.
_REFUSAL_MARKERS = ("can't help with this", "anthropic.com/legal/aup")


# The audio fallback, after Marinos's the-agent-daily-publisher
# (scripts/transcribe_local.py): when captions give nothing, download the audio
# and transcribe it here with Whisper. large-v3-turbo, never `base`, which
# mangles the product and CLI names a summary exists to keep. Measured on this
# M4 Max on 2026-11-09: 80 s for a 16.6-minute talk, about 12x real time.
WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"

# Longer than this is a livestream or a conference day (AI Engineer posts
# 8-hour streams), not a talk: 40 minutes of Whisper and a prompt too long to
# summarise into 800 characters. Recorded as too_long, which is terminal.
MAX_AUDIO_SECONDS = 2 * 60 * 60

_PROBE_TIMEOUT = 60
_DOWNLOAD_TIMEOUT = 900


class DistilRefused(Exception):
    """The model declined to distil this transcript; retrying will not change it."""


def fetch_transcript(video_id: str) -> tuple[str, str]:
    """Fetch a video's captions. Returns (text, status).

    Status is 'ok', 'no_captions' when the video will never have captions,
    'upcoming' when it has not aired yet (a premiere or a scheduled live event),
    'blocked' when YouTube is refusing this IP, or 'fetch_failed' for anything
    else transient such as a timeout. The distinction matters: no_captions is
    recorded as terminal, while fetch_failed is retried on later runs up to the
    attempt ceiling. harvest() stores 'blocked' as fetch_failed, because it says
    nothing about the video, and uses it to stop asking.
    """
    try:
        snippets = YouTubeTranscriptApi().fetch(video_id)
        return " ".join(snippet.text for snippet in snippets).strip(), "ok"
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable):
        return "", "no_captions"
    except VideoUnplayable as e:
        if (e.reason or "").startswith(_NOT_AIRED_PREFIXES):
            logger.info(f"{video_id}: not aired yet ({e.reason}); will check again next run")
            return "", "upcoming"
        logger.warning(f"{video_id}: transcript fetch failed: {type(e).__name__}: {e}")
        return "", "fetch_failed"
    except RequestBlocked as e:
        # IpBlocked is a subclass. Both are about this IP, not this video.
        logger.warning(f"{video_id}: transcript fetch failed: {type(e).__name__}: {e}")
        return "", "blocked"
    except Exception as e:
        logger.warning(f"{video_id}: transcript fetch failed: {type(e).__name__}: {e}")
        return "", "fetch_failed"


# By module, not by PATH: the locked yt-dlp in this venv, whatever brew has.
_YTDLP = [sys.executable, "-m", "yt_dlp", "--no-update", "--no-warnings", "-q"]


def _watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def probe_duration(video_id: str) -> tuple[float | None, str]:
    """The video's length in seconds, or (None, 'upcoming' | 'failed')."""
    try:
        probe = subprocess.run(
            [*_YTDLP, "--skip-download", "--print", "duration", _watch_url(video_id)],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning(f"{video_id}: audio probe failed: {type(e).__name__}: {e}")
        return None, "failed"
    try:
        return float((probe.stdout or "").strip().splitlines()[-1]), ""
    except (ValueError, IndexError):
        pass
    # A premiere or a live event has no duration yet; yt-dlp says why on stderr.
    detail = (probe.stderr or "").strip()[-300:] or (probe.stdout or "").strip()[:100]
    if any(f": {prefix}" in detail for prefix in _NOT_AIRED_PREFIXES):
        logger.info(f"{video_id}: not aired yet; will check again next run")
        return None, "upcoming"
    logger.warning(f"{video_id}: audio probe gave no duration (rc={probe.returncode}): {detail}")
    return None, "failed"


def audio_segments(video_id: str, timeout: int = _DOWNLOAD_TIMEOUT) -> list[dict[str, Any]] | None:
    """Download the audio and transcribe it with Whisper: [{start, text}], or None on failure.

    The caller has already checked that mlx_whisper imports.
    """
    import mlx_whisper

    with tempfile.TemporaryDirectory(prefix="yt-audio-") as tmp:
        try:
            download = subprocess.run(
                [
                    *_YTDLP,
                    "-f",
                    "bestaudio/best",
                    "-o",
                    f"{tmp}/{video_id}.%(ext)s",
                    _watch_url(video_id),
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f"{video_id}: audio download failed: {type(e).__name__}: {e}")
            return None
        audio = sorted(Path(tmp).glob(f"{video_id}.*"))
        if download.returncode != 0 or not audio:
            detail = (download.stderr or "").strip()[-300:]
            logger.warning(
                f"{video_id}: audio download failed (rc={download.returncode}): {detail}"
            )
            return None
        try:
            result = mlx_whisper.transcribe(
                str(audio[0]), path_or_hf_repo=WHISPER_MODEL, verbose=None
            )
        except Exception as e:
            logger.warning(f"{video_id}: Whisper failed: {type(e).__name__}: {e}")
            return None
    return [
        {"start": int(seg.get("start") or 0), "text": seg["text"].strip()}
        for seg in result.get("segments", [])
        if (seg.get("text") or "").strip()
    ]


def whisper_available() -> bool:
    try:
        import mlx_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def transcribe_audio(video_id: str) -> tuple[str, str]:
    """Transcribe a video from its audio with local Whisper. Returns (text, status).

    Status is 'ok'; 'upcoming' for a premiere or live event not yet aired;
    'too_long' past MAX_AUDIO_SECONDS, which is terminal;
    'failed' when the probe, the download or the transcription breaks, which is
    retried; or 'unavailable' when this host cannot run Whisper at all (the VPS),
    in which case the caller keeps whatever the captions said.
    """
    if not whisper_available():
        return "", "unavailable"
    duration, status = probe_duration(video_id)
    if duration is None:
        return "", status
    if duration > MAX_AUDIO_SECONDS:
        logger.info(f"{video_id}: {duration / 3600:.1f} h of audio, past the cap; not transcribing")
        return "", "too_long"
    segments = audio_segments(video_id)
    text = " ".join(seg["text"] for seg in segments or [])
    if not text:
        return "", "failed"
    logger.info(f"{video_id}: transcribed {duration / 60:.0f} min of audio with Whisper")
    return text, "ok"


def _audio_fallback(video_id: str, caption_status: str, stats: dict[str, int]) -> tuple[str, str]:
    """Try the audio for a video whose captions gave nothing. Returns (text, status)."""
    text, audio = transcribe_audio(video_id)
    if audio == "ok":
        stats["whisper"] += 1
        return text, "ok"
    if audio in ("too_long", "upcoming"):
        return "", audio
    if audio == "failed":
        # Retryable even when the captions said never: the audio may come next run.
        return "", "fetch_failed"
    return "", caption_status


# Below this many spoken words there is nothing to abstract: Anthropic's launch
# videos are music, trailers are a sentence or two. Sent anyway, the model wrote
# "I can't write an abstract..." and 36 of those were stored as summaries.
MIN_SPEECH_WORDS = 30
_CUES = re.compile(r"\[[^\]]*\]|\u266a|>>")
# A reply that opens by declining, or the sentinel the prompt offers. A real
# abstract mentions "the transcript" only mid-sentence, never as its opening.
_DECLINES = re.compile(
    r"^\s*(?:NO_CONTENT|I can['\u2019]?t\b|I cannot\b|I['\u2019]m unable\b|I am unable\b"
    r"|(?:The|This) (?:transcript|excerpt)\b)",
    re.IGNORECASE,
)


def speech_words(transcript: str) -> int:
    """Words actually spoken, with music and applause cues taken out."""
    return len(_CUES.sub(" ", transcript).split())


def _abstract_for(text: str, title: str) -> tuple[str, str]:
    """The abstract and the status it earns: ok, too_thin, refused or summary_failed."""
    if speech_words(text) < MIN_SPEECH_WORDS:
        return "", "too_thin"
    try:
        reply = distil(text, title)
    except DistilRefused:
        return "", "refused"
    if _DECLINES.match(reply):
        return "", "too_thin"
    abstract = trim_to_sentence(reply, ABSTRACT_MAX_CHARS)
    return (abstract, "ok") if abstract else ("", "summary_failed")


def _settle(
    conn: sqlite3.Connection,
    video_id: str,
    channel: str,
    title: str,
    published: str | None,
    text: str,
    status: str,
    stats: dict[str, int],
) -> None:
    """Distil a fetched transcript, record the outcome, and count it."""
    abstract = ""
    if status == "ok":
        abstract, status = _abstract_for(text, title)

    upsert_transcript(conn, video_id, channel, title, published, text, abstract, status)

    if status == "ok":
        stats["ok"] += 1
    elif status in ("no_captions", "too_long", "too_thin"):
        stats["no_captions"] += 1
    elif status == "refused":
        stats["refused"] += 1
    else:
        stats["failed"] += 1


def _new_stats() -> dict[str, int]:
    return {
        "channels": 0,
        "attempted": 0,
        "ok": 0,
        "no_captions": 0,
        "failed": 0,
        "blocked": 0,
        "upcoming": 0,
        "refused": 0,
        "whisper": 0,
    }


def distil(transcript: str, title: str) -> str:
    """Distil a transcript into a factual abstract via the local claude CLI.

    Routed through the CLI (Vertex) rather than any SDK, per project policy.
    Returns "" on any failure; the caller records summary_failed and keeps the
    raw transcript so only the cheap half is retried. Raises DistilRefused when
    the model declines on policy grounds, which a retry would not change.
    """
    prompt = _DISTIL_PROMPT.format(title=title, transcript=transcript)
    model, run_env = vertex_cli_model_and_env("sonnet")
    try:
        # --bare: a plain prose call needs no plugins, hooks or MCP servers, and
        # without it every call cold-starts all of them.
        # --tools "": the input is fetched text, so it must not be able to drive
        # Read or Bash (claude --print runs tools without asking).
        result = subprocess.run(
            ["claude", "--model", model, "--print", "--bare", "--tools", ""],
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
        if any(marker in detail for marker in _REFUSAL_MARKERS):
            logger.warning(f"claude CLI refused the transcript: {detail[:300]}")
            raise DistilRefused(detail[:300])
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

    stats = _new_stats()

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

                # Not aired yet: record nothing, so no attempt is spent, and look
                # again next run. It gives its slot of the run limit back: on
                # 2026-11-09 premieres held 2 of the 3 slots for a day, so one
                # real video an hour got through. The check is one caption
                # request per scheduled premiere, a handful at most, so the burst
                # the limit guards against does not come from here.
                if status == "upcoming":
                    stats["attempted"] -= 1
                    stats["upcoming"] += 1
                    continue

                # For the video a block is an ordinary retryable failure.
                blocked = status == "blocked"
                if blocked:
                    status = "fetch_failed"
                    stats["blocked"] += 1

                # An 'ok' with no words is a caption track that exists but is
                # empty; distilling it would send the model an empty prompt.
                if status == "ok" and not text.strip():
                    text, status = "", "no_captions"

                # Captions gave nothing: try the audio. Never under a block,
                # which means stop asking YouTube for anything at all.
                if status in ("no_captions", "fetch_failed") and not blocked:
                    text, status = _audio_fallback(video_id, status, stats)
                    if status == "upcoming":
                        stats["attempted"] -= 1
                        stats["upcoming"] += 1
                        continue

                _settle(
                    conn, video_id, name, video["title"], video["published"], text, status, stats
                )

                if blocked:
                    # Every later fetch this run would be refused the same way,
                    # each spending another pending video's attempt budget.
                    logger.warning("YouTube is blocking this IP; stopping at the first refusal")
                    return stats
    finally:
        conn.close()

    return stats


def backfill(
    db_path: str | Path, days: int, limit: int, channels: list[str] | None = None
) -> dict[str, int]:
    """Transcribe from audio the recent videos whose captions gave nothing.

    The store is the work list, not the feeds: a channel's feed holds only its
    latest videos, and most of these have left it. Captions are not asked again;
    they already failed for every row here.
    """
    db_path = Path(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_transcript_db(conn)
    stats = _new_stats()
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    only = ""
    params: list[Any] = [cutoff]
    if channels:
        only = f" AND channel IN ({','.join('?' * len(channels))})"
        params.extend(channels)
    try:
        rows = conn.execute(
            "SELECT video_id, channel, title, published_at, status FROM transcripts "
            "WHERE status IN ('no_captions', 'fetch_failed') AND published_at >= ?"
            + only
            + " ORDER BY published_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        for row in rows:
            text, status = _audio_fallback(row["video_id"], row["status"], stats)
            if status == "upcoming":
                # Not aired: nothing recorded, nothing failed; a later run takes it.
                stats["upcoming"] += 1
                continue
            stats["attempted"] += 1
            _settle(
                conn,
                row["video_id"],
                row["channel"],
                row["title"],
                row["published_at"],
                text,
                status,
                stats,
            )
    finally:
        conn.close()
    return stats


def _cooldown_file(db_path: Path) -> Path:
    """The state file holding when the current IP-block cooldown ends."""
    return db_path.with_name(db_path.name + ".cooldown")


def cooldown_until(db_path: Path, now: datetime | None = None) -> datetime | None:
    """Return when the current IP-block cooldown ends, or None when there is none.

    An unreadable state file is logged and ignored: failing open costs one run of
    fetches, failing closed would stop the harvester for good.
    """
    path = _cooldown_file(db_path)
    try:
        until = datetime.fromisoformat(path.read_text().strip())
        active = until > (now or datetime.now(UTC))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError) as e:
        logger.warning(f"ignoring unreadable cooldown file {path}: {type(e).__name__}: {e}")
        return None
    return until if active else None


def record_block(db_path: Path, blocked: bool, now: datetime | None = None) -> None:
    """Start a cooldown after a run that hit an IP block; clear it after one that did not."""
    path = _cooldown_file(db_path)
    if not blocked:
        path.unlink(missing_ok=True)
        return
    until = (now or datetime.now(UTC)) + BLOCK_COOLDOWN
    path.write_text(until.isoformat() + "\n")
    logger.warning(f"No transcript fetches until {until.astimezone():%Y-%m-%d %H:%M %Z}")


def push_to_vps(db_path: Path) -> bool:
    """rsync the store to its one place on the VPS. Returns True on success."""
    try:
        result = subprocess.run(
            ["rsync", "-az", "--timeout=30", str(db_path), VPS_STORE],
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


def _configure_logging() -> None:
    """Routine progress to stdout, WARNING and above to stderr.

    basicConfig() with no `stream=` installs a StreamHandler on sys.stderr, so
    every INFO line this script emitted went to the error stream. Under launchd
    that is StandardErrorPath: the .err file had grown to several MB of ordinary
    per-video progress while the .log file beside it sat at 0 bytes. An error
    stream that carries routine output cannot be read as errors, which is the
    whole reason to have two streams.

    The split is by level, not by call site, so logger.error/logger.warning keep
    reaching stderr unchanged and nothing else in the module has to know.
    """
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    out = logging.StreamHandler(sys.stdout)
    out.setLevel(logging.DEBUG)
    out.addFilter(lambda record: record.levelno < logging.WARNING)
    out.setFormatter(fmt)

    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.WARNING)
    err.setFormatter(fmt)

    logging.basicConfig(level=logging.INFO, handlers=[out, err])


def main() -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(description="Harvest YouTube transcripts for a profile")
    parser.add_argument("--profile", default="stack")
    parser.add_argument("--limit", type=int, default=_DEFAULT_LIMIT)
    parser.add_argument(
        "--db",
        default=str(DATA_DIR / "transcripts.db"),
        help="the transcript store; it must sit inside the repo's data/ folder",
    )
    parser.add_argument("--no-push", action="store_true")
    parser.add_argument(
        "--backfill-days",
        type=int,
        default=0,
        help="instead of the feeds, transcribe from audio the videos of the last N days "
        "whose captions gave nothing (up to --limit of them)",
    )
    parser.add_argument(
        "--channel",
        action="append",
        help='with --backfill-days, only this channel as stored, e.g. "YouTube: Anthropic" '
        "(repeatable)",
    )
    args = parser.parse_args()
    try:
        db_path = within(args.db, DATA_DIR)
    except ValueError as e:
        print(f"youtube_harvest: --db refused: {e}", file=sys.stderr)
        return 2

    until = cooldown_until(db_path)
    if until is not None:
        # Non-zero like the run that started it: nothing new is being fetched, and a
        # cooldown that exited 0 would grade green for five hours in six.
        logger.warning(
            f"IP-block cooldown until {until.astimezone():%Y-%m-%d %H:%M %Z}: skipping "
            f"the fetch, pushing the existing store unchanged. Delete "
            f"{_cooldown_file(db_path)} to retry sooner."
        )
        produced = False
    else:
        if args.backfill_days:
            stats = backfill(
                db_path, days=args.backfill_days, limit=args.limit, channels=args.channel
            )
        else:
            stats = harvest(get_sources(profile=args.profile), db_path, limit=args.limit)
        logger.info(
            f"Harvest complete: {stats['channels']} channels, {stats['attempted']} attempted, "
            f"{stats['ok']} ok, {stats['no_captions']} no captions, {stats['failed']} failed, "
            f"{stats['upcoming']} not aired, {stats['refused']} refused, "
            f"{stats['whisper']} from audio"
        )
        record_block(db_path, stats["blocked"] > 0)
        # Nothing attempted is a quiet hour or an empty channel list, and all
        # no_captions is a correct answer. Only attempts that all failed are a failure.
        produced = not (stats["attempted"] > 0 and stats["ok"] == 0 and stats["failed"] > 0)
        if not produced:
            logger.error(
                f"Harvest produced nothing: {stats['failed']} of {stats['attempted']} "
                "attempts failed"
            )

    pushed = args.no_push or push_to_vps(db_path)
    return 0 if produced and pushed else 1


if __name__ == "__main__":
    sys.exit(main())
