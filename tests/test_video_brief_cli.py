"""The /video-brief scripts' command lines: they only read and write inside one folder."""

import json
import sys
from unittest.mock import patch

import pytest

from scripts import video_brief_render, video_source
from scripts.video_paths import inside

SOURCE = {
    "video": {
        "id": "BT1Mo-fD-g0",
        "url": "https://www.youtube.com/watch?v=BT1Mo-fD-g0",
        "title": "t",
        "channel": "c",
        "published": "2026-11-08",
        "duration_seconds": 60,
    },
    "method": "whisper",
    "segments": [{"start": 0, "text": "Hi everyone"}],
}
BRIEF = {
    "headline": "h",
    "summary": "s",
    "sections": [{"title": "One", "start": 0, "paragraphs": ["p"]}],
    "takeaways": [{"lead": "L", "text": "t"}],
    "resources": [],
}


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_BRIEF_DIR", str(tmp_path / "Downloads"))
    (tmp_path / "Downloads").mkdir()
    return tmp_path / "Downloads"


def _argv(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["prog", *args])


def test_inside_accepts_the_folder_and_refuses_a_way_out(folder):
    assert inside(str(folder / "run" / "a.json")) == (folder / "run" / "a.json").resolve()
    with pytest.raises(ValueError, match="outside"):
        inside(str(folder / ".." / "escape.json"))
    with pytest.raises(ValueError, match="outside"):
        inside("/etc/passwd")


def _render_inputs(folder):
    (folder / "b.json").write_text(json.dumps(BRIEF))
    (folder / "s.json").write_text(json.dumps(SOURCE))


def test_render_writes_page_and_mail_with_the_fixed_signature(folder, tmp_path, monkeypatch):
    _render_inputs(folder)
    signature = tmp_path / "signature.html"
    signature.write_text("<div>SIG</div>")
    monkeypatch.setattr(video_brief_render, "SIGNATURE", signature)
    _argv(
        monkeypatch,
        str(folder / "b.json"),
        str(folder / "s.json"),
        "--page",
        str(folder / "p.html"),
        "--email",
        str(folder / "m.html"),
        "--signature",
    )

    assert video_brief_render.main() == 0
    assert "<h1>h</h1>" in (folder / "p.html").read_text()
    assert (folder / "m.html").read_text().endswith("<div>SIG</div>")


def test_render_refuses_to_write_outside_the_folder(folder, tmp_path, monkeypatch):
    _render_inputs(folder)
    _argv(
        monkeypatch,
        str(folder / "b.json"),
        str(folder / "s.json"),
        "--page",
        str(tmp_path / "elsewhere.html"),
        "--email",
        str(folder / "m.html"),
    )

    assert video_brief_render.main() == 2
    assert not (tmp_path / "elsewhere.html").exists()


def test_source_writes_its_json_inside_the_folder(folder, monkeypatch):
    _argv(monkeypatch, "BT1Mo-fD-g0", "-o", str(folder / "s.json"))
    with patch("scripts.video_source.source", return_value=SOURCE):
        assert video_source.main() == 0

    assert json.loads((folder / "s.json").read_text())["method"] == "whisper"


def test_source_prints_to_stdout_without_an_output(folder, monkeypatch, capsys):
    _argv(monkeypatch, "BT1Mo-fD-g0")
    with patch("scripts.video_source.source", return_value=SOURCE):
        assert video_source.main() == 0

    assert json.loads(capsys.readouterr().out)["video"]["id"] == "BT1Mo-fD-g0"


def test_source_refuses_an_output_outside_the_folder(folder, tmp_path, monkeypatch):
    _argv(monkeypatch, "BT1Mo-fD-g0", "-o", str(tmp_path / "out.json"))
    with patch("scripts.video_source.source") as fetch:
        assert video_source.main() == 1

    fetch.assert_not_called()  # refused before any network work


def test_source_reports_a_video_without_any_transcript(folder, monkeypatch, capsys):
    _argv(monkeypatch, "BT1Mo-fD-g0")
    with patch("scripts.video_source.source", side_effect=video_source.NoTranscript("none")):
        assert video_source.main() == 1

    assert "none" in capsys.readouterr().err
