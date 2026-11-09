"""Everything a video brief is written from: the video's metadata and a timestamped transcript.

Used by the /video-brief skill, on demand, for videos the owner picks. Captions first,
as the harvester does; when they give nothing, the audio is transcribed here with local
Whisper, the fallback from George Marinos's the-agent-daily-publisher. Runs on the Mac
only, for the harvester's reasons: YouTube refuses the VPS, and Whisper needs Apple
Silicon.

    uv run python scripts/video_source.py <url-or-id> [-o source.json]

The output path must sit inside ~/Downloads (scripts/video_paths.py says why).

Writes JSON: video (id, url, title, channel, published, duration_seconds, description,
chapters), method ('captions' or 'whisper'), and segments [{start, text}]. Exit 1 with a
reason on stderr when no transcript could be had.
"""

import argparse
import json
import logging
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.video_paths import inside  # noqa: E402
from scripts.youtube_harvest import _YTDLP, audio_segments, whisper_available  # noqa: E402

logger = logging.getLogger(__name__)

_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})")
_METADATA_TIMEOUT = 60
# On demand the owner may pick a long talk, so the download gets more room than
# the harvester's, and there is no length cap.
_DOWNLOAD_TIMEOUT = 1800


class NoTranscript(Exception):
    """Neither captions nor the audio gave any text."""


def video_id(url_or_id: str) -> str:
    """The 11-character id from any YouTube URL form, or the id itself."""
    value = url_or_id.strip()
    match = _ID.search(value)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value
    raise ValueError(f"not a YouTube URL or video id: {url_or_id!r}")


def metadata(vid: str) -> dict[str, Any]:
    """Title, channel, date, length, description and chapters, from one yt-dlp call."""
    url = f"https://www.youtube.com/watch?v={vid}"
    result = subprocess.run(
        [*_YTDLP, "-J", "--skip-download", url],
        capture_output=True,
        text=True,
        timeout=_METADATA_TIMEOUT,
    )
    if result.returncode != 0:
        raise NoTranscript(
            f"yt-dlp could not read the video: {(result.stderr or '').strip()[-300:]}"
        )
    info = json.loads(result.stdout)
    published = None
    if info.get("timestamp"):
        published = datetime.fromtimestamp(info["timestamp"], UTC).isoformat()
    elif info.get("upload_date"):
        d = info["upload_date"]
        published = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return {
        "id": vid,
        "url": url,
        "title": info.get("title") or vid,
        "channel": info.get("channel") or info.get("uploader") or "",
        "published": published,
        "duration_seconds": int(info.get("duration") or 0),
        "description": info.get("description") or "",
        "chapters": [
            {"start": int(c.get("start_time") or 0), "title": c.get("title") or ""}
            for c in (info.get("chapters") or [])
        ],
    }


def caption_segments(vid: str) -> list[dict[str, Any]]:
    """Timestamped caption segments, or [] when the video has none we can fetch."""
    from youtube_transcript_api import YouTubeTranscriptApi

    try:
        fetched = YouTubeTranscriptApi().fetch(vid)
    except Exception as e:
        logger.info(f"{vid}: no captions ({type(e).__name__}); trying the audio")
        return []
    return [
        {"start": int(s.start), "text": s.text.strip()}
        for s in fetched
        if s.text and s.text.strip()
    ]


def whisper_segments(vid: str) -> list[dict[str, Any]]:
    """Timestamped segments from the audio, transcribed here with Whisper."""
    if not whisper_available():
        raise NoTranscript("no captions, and Whisper is not available on this host")
    segments = audio_segments(vid, timeout=_DOWNLOAD_TIMEOUT)
    if segments is None:
        raise NoTranscript("no captions, and the audio could not be downloaded or transcribed")
    return segments


def source(url_or_id: str) -> dict[str, Any]:
    """Metadata plus the best transcript we can get, captions before audio."""
    vid = video_id(url_or_id)
    info = metadata(vid)
    segments = caption_segments(vid)
    method = "captions"
    if not segments:
        segments = whisper_segments(vid)
        method = "whisper"
    if not segments:
        raise NoTranscript("captions and audio both gave no text")
    return {"video": info, "method": method, "segments": segments}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    parser = argparse.ArgumentParser(
        description="Metadata and a timestamped transcript for one video"
    )
    parser.add_argument("video", help="YouTube URL or 11-character video id")
    parser.add_argument(
        "-o", "--output", help="write the JSON here (inside ~/Downloads) instead of stdout"
    )
    args = parser.parse_args()
    try:
        output = inside(args.output) if args.output else None
        data = source(args.video)
    except (ValueError, NoTranscript, subprocess.TimeoutExpired) as e:
        print(f"video_source: {e}", file=sys.stderr)
        return 1
    text = json.dumps(data, ensure_ascii=False, indent=1)
    if output:
        output.write_text(text, encoding="utf-8")
        words = sum(len(s["text"].split()) for s in data["segments"])
        print(
            f"{output}: {data['video']['title']} | {data['method']} | "
            f"{len(data['segments'])} segments, {words} words",
            file=sys.stderr,
        )
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
