"""Tests for stack synthesis prompt construction."""

from news.models import Article
from news.stack_synth import build_stack_prompt


def _article(
    url: str = "https://example.com/story",
    source: str = "TechCrunch",
    content: str = "",
    transcript_abstract: str = "",
    changelog_digest: str = "",
) -> Article:
    return Article(
        url=url,
        title="A Story",
        source=source,
        content=content,
        categories=["tech"],
        language="en",
        transcript_abstract=transcript_abstract,
        changelog_digest=changelog_digest,
    )


def test_stack_prompt_gives_a_transcript_abstract_the_larger_allowance():
    article = _article(
        url="https://www.youtube.com/watch?v=G55HSGpuh1M",
        source="YouTube: Fireship",
        content="blurb",
        transcript_abstract="x" * 1000,
    )

    prompt = build_stack_prompt([article])

    assert "x" * 800 in prompt
    assert "x" * 801 not in prompt


def test_stack_prompt_keeps_the_300_char_cap_for_plain_articles():
    article = _article(content="y" * 500)

    prompt = build_stack_prompt([article])

    assert "y" * 300 in prompt
    assert "y" * 301 not in prompt


def test_stack_prompt_prefers_the_abstract_over_the_description():
    """A video's description is what we are deliberately getting away from."""
    article = _article(
        url="https://www.youtube.com/watch?v=G55HSGpuh1M",
        source="YouTube: Fireship",
        content="SPONSORED BY ACME, use code SAVE20",
        transcript_abstract="Meta released Muse Glimmer under Apache 2.0.",
    )

    prompt = build_stack_prompt([article])

    assert "Meta released Muse Glimmer under Apache 2.0." in prompt
    assert "SAVE20" not in prompt


def test_stack_prompt_prefers_the_changelog_digest_over_content():
    """For a system-prompt entry content[:300] is preamble that is byte-identical
    across 6 of the 29 dated entries, so it gives the synthesis nothing to cite."""
    article = _article(
        source="Claude System Prompts",
        content="The assistant is Claude, made by Anthropic. The current date is...",
        changelog_digest="MODEL IDS +: claude-opus-5",
    )

    prompt = build_stack_prompt([article])

    assert "MODEL IDS +: claude-opus-5" in prompt
    assert "The current date is" not in prompt


def test_stack_prompt_prefers_the_changelog_digest_over_a_transcript_abstract():
    """Pins the documented precedence: changelog > transcript > content."""
    article = _article(
        content="preamble",
        transcript_abstract="An abstract of a video.",
        changelog_digest="DELTA vs the previous dated entry: 3 of 120 changed.",
    )

    prompt = build_stack_prompt([article])

    assert "DELTA vs the previous dated entry: 3 of 120 changed." in prompt
    assert "An abstract of a video." not in prompt


def test_stack_prompt_gives_a_changelog_digest_the_2000_char_allowance():
    """Copying the transcript branch's 800 would cut the STACK IMPACT line off."""
    digest = _article(changelog_digest="z" * 3000)
    abstract = _article(transcript_abstract="w" * 3000)

    prompt = build_stack_prompt([digest, abstract])

    assert "z" * 2000 in prompt
    assert "z" * 2001 not in prompt
    assert "w" * 800 in prompt
    assert "w" * 801 not in prompt


# --- the reader stack description -------------------------------------------
# This was a string literal in stack_synth.py, written 2026-05-27 and never edited.
# By 2026-08-27 it claimed six MCP servers against a real 15 and listed no VPS, in a
# pipeline that runs on one. Every "FOR YOUR STACK" recommendation was aimed at it.


def _write_stack(tmp_path, reviewed, profile="- Primary AI tool: Claude Code CLI"):
    p = tmp_path / "reader_stack.yaml"
    p.write_text(f"last_reviewed: {reviewed}\nprofile: |\n  {profile}\n", encoding="utf-8")
    return p


def test_load_reader_stack_returns_the_profile_when_fresh(tmp_path):
    from datetime import date

    from news.stack_synth import load_reader_stack

    path = _write_stack(tmp_path, "2026-08-27")
    out = load_reader_stack(path, today=date(2026, 8, 28))

    assert "Claude Code CLI" in out
    assert "out of date" not in out


def test_load_reader_stack_labels_a_stale_profile_instead_of_dropping_it(tmp_path):
    """Stale is still better than nothing, but the model must be told to discount it."""
    from datetime import date

    from news.stack_synth import STALE_AFTER_DAYS, load_reader_stack

    path = _write_stack(tmp_path, "2026-05-27")
    out = load_reader_stack(path, today=date(2026, 8, 27))

    assert "Claude Code CLI" in out
    assert "92 days ago" in out
    assert STALE_AFTER_DAYS == 30


def test_load_reader_stack_degrades_to_empty_when_the_file_is_missing(tmp_path):
    """A missing file costs the brief its personalisation, never the run."""
    from news.stack_synth import load_reader_stack

    assert load_reader_stack(tmp_path / "nope.yaml") == ""


def test_build_stack_prompt_injects_the_real_stack_and_leaves_no_placeholder():
    from news.stack_synth import build_stack_prompt, load_reader_stack

    prompt = build_stack_prompt([], time_window="last 36 hours")

    assert "{reader_stack}" not in prompt
    assert "THE READER'S CURRENT STACK" in prompt
    # The shipped file must be real enough to name the VPS the pipeline runs on.
    assert "Hetzner" in load_reader_stack()


def test_build_stack_prompt_survives_a_missing_reader_stack(monkeypatch, tmp_path):
    import news.stack_synth as ss

    monkeypatch.setattr(ss, "_READER_STACK_PATH", tmp_path / "absent.yaml")
    prompt = ss.build_stack_prompt([], time_window="last 36 hours")

    assert "{reader_stack}" not in prompt
    assert "THE READER'S CURRENT STACK" not in prompt
    assert "WHAT TO PRIORITIZE" in prompt
