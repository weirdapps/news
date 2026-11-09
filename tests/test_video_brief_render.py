"""Tests for scripts/video_brief_render.py: the page and the mail a brief becomes."""

from scripts.video_brief_render import clock, email, inline, page

VIDEO = {
    "id": "BT1Mo-fD-g0",
    "url": "https://www.youtube.com/watch?v=BT1Mo-fD-g0",
    "title": "Your Coding Agent Can't Undo Its Own Mistakes",
    "channel": "AI Engineer",
    "published": "2026-11-08T08:00:03+00:00",
    "duration_seconds": 998,
}

BRIEF = {
    "headline": "Agents that succeed cannot always undo",
    "summary": "UndoBench ran **2,880** paired trials.",
    "tags": ["agents", "undo"],
    "sections": [
        {
            "title": "The missing undo",
            "start": 0,
            "paragraphs": ["Git covers the repo.", "Not APIs."],
        },
        {"title": "UndoBench", "start": 3725, "paragraphs": ["Use `git revert`, then <script>."]},
    ],
    "takeaways": [{"lead": "Success is not recovery.", "text": "47% recovered."}],
    "resources": [
        {
            "name": "UndoBench",
            "url": "https://github.com/example/undobench",
            "note": "the benchmark",
        },
        {"name": "Bad link", "url": "javascript:alert(1)", "note": ""},
    ],
}

# The owner bans en and em dashes in anything written for them.
_DASHES = (chr(0x2013), chr(0x2014))


def test_clock_formats_minutes_and_hours():
    assert (clock(5), clock(125), clock(3725)) == ("0:05", "2:05", "1:02:05")


def test_inline_keeps_bold_and_code_and_escapes_the_rest():
    assert inline("**a** `b` <i>") == "<strong>a</strong> <code>b</code> &lt;i&gt;"


def test_every_section_opens_on_a_timestamp_that_links_into_the_video():
    html = page(BRIEF, VIDEO, "captions")

    assert 'href="https://www.youtube.com/watch?v=BT1Mo-fD-g0&amp;t=3725s">1:02:05</a>' in html
    assert html.count('class="ts"') == 4  # contents and heading, for each of the two sections


def test_the_page_escapes_prose_and_refuses_unsafe_links():
    html = page(BRIEF, VIDEO, "captions")

    assert "<script>." not in html and "&lt;script&gt;." in html
    assert "javascript:" not in html


def test_the_page_says_how_the_transcript_was_made():
    assert "transcribed from audio (Whisper)" in page(BRIEF, VIDEO, "whisper")
    assert "from captions" in page(BRIEF, VIDEO, "captions")


def test_the_mail_follows_the_owner_mail_format():
    body = email(BRIEF, VIDEO, "captions", signature="<div>SIG</div>")

    assert "<p" not in body  # no paragraphs; <br> only
    assert "font-family:Aptos" in body and "12pt" in body and "#404040" in body
    assert "<style" not in body and "class=" not in body  # inline styles only
    assert body.endswith("<div>SIG</div>")
    assert "&amp;t=3725s" in body


def test_neither_output_carries_a_dash_the_owner_bans():
    out = email(BRIEF, VIDEO, "captions") + page(BRIEF, VIDEO, "captions")

    assert not any(d in out for d in _DASHES)


def test_a_brief_without_resources_shows_no_empty_heading():
    bare = {**BRIEF, "resources": []}

    assert "Resources mentioned" not in page(bare, VIDEO, "captions")
    assert "Resources mentioned" not in email(bare, VIDEO, "captions")
