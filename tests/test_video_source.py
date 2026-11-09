"""Tests for scripts/video_source.py. No network, no yt-dlp, no Whisper."""

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from scripts.video_source import NoTranscript, metadata, source, video_id

YTDLP_JSON = {
    "id": "BT1Mo-fD-g0",
    "title": "Your Coding Agent Can't Undo Its Own Mistakes",
    "channel": "AI Engineer",
    "timestamp": 1794124803,
    "upload_date": "20261108",
    "duration": 998,
    "description": "UndoBench and EvoUndo.",
    "chapters": [{"start_time": 0.0, "title": "Intro"}, {"start_time": 95.5, "title": "UndoBench"}],
}


@pytest.mark.parametrize(
    "value",
    [
        "https://www.youtube.com/watch?v=BT1Mo-fD-g0",
        "https://youtu.be/BT1Mo-fD-g0?si=abc",
        "https://www.youtube.com/live/BT1Mo-fD-g0",
        "https://www.youtube.com/shorts/BT1Mo-fD-g0",
        "BT1Mo-fD-g0",
    ],
)
def test_video_id_reads_every_url_form(value):
    assert video_id(value) == "BT1Mo-fD-g0"


def test_video_id_refuses_what_is_not_a_video():
    with pytest.raises(ValueError):
        video_id("https://example.com/talk")


def test_metadata_keeps_title_channel_date_length_and_chapters():
    done = Mock(returncode=0, stdout=json.dumps(YTDLP_JSON), stderr="")
    with patch("scripts.video_source.subprocess.run", return_value=done):
        info = metadata("BT1Mo-fD-g0")

    assert info["title"].startswith("Your Coding Agent")
    assert info["channel"] == "AI Engineer"
    assert info["published"].startswith("2026-11-08")
    assert info["duration_seconds"] == 998
    assert info["chapters"] == [{"start": 0, "title": "Intro"}, {"start": 95, "title": "UndoBench"}]


def _info():
    return {"id": "BT1Mo-fD-g0", "title": "t", "channel": "c", "url": "u"}


def test_source_prefers_captions_and_keeps_their_timestamps():
    snippets = [SimpleNamespace(text="Hi everyone", start=0.4), SimpleNamespace(text="", start=2.0)]
    api = Mock()
    api.return_value.fetch.return_value = snippets
    with (
        patch("scripts.video_source.metadata", return_value=_info()),
        patch("youtube_transcript_api.YouTubeTranscriptApi", api),
        patch("scripts.video_source.whisper_segments") as whisper,
    ):
        data = source("BT1Mo-fD-g0")

    whisper.assert_not_called()
    assert data["method"] == "captions"
    assert data["segments"] == [{"start": 0, "text": "Hi everyone"}]


def test_source_falls_back_to_whisper_when_there_are_no_captions():
    api = Mock()
    api.return_value.fetch.side_effect = RuntimeError("TranscriptsDisabled")
    with (
        patch("scripts.video_source.metadata", return_value=_info()),
        patch("youtube_transcript_api.YouTubeTranscriptApi", api),
        patch("scripts.video_source.whisper_available", return_value=True),
        patch(
            "scripts.video_source.audio_segments", return_value=[{"start": 3, "text": "spoken"}]
        ) as audio,
    ):
        data = source("BT1Mo-fD-g0")

    assert data["method"] == "whisper"
    assert data["segments"] == [{"start": 3, "text": "spoken"}]
    assert audio.call_args.kwargs["timeout"] >= 1800  # on demand, long talks are allowed


def test_source_says_why_when_nothing_gives_text():
    api = Mock()
    api.return_value.fetch.side_effect = RuntimeError("TranscriptsDisabled")
    with (
        patch("scripts.video_source.metadata", return_value=_info()),
        patch("youtube_transcript_api.YouTubeTranscriptApi", api),
        patch("scripts.video_source.whisper_available", return_value=False),
        pytest.raises(NoTranscript, match="Whisper is not available"),
    ):
        source("BT1Mo-fD-g0")
