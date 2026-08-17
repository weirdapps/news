"""Tests for the deterministic changelog delta extractor.

The module is pure and stdlib-only, so every test here is an exact-output test:
no mocks, no fixtures on disk, no network.
"""

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from news.changelog_delta import DIGEST_CAP, changelog_delta, select_predecessor

# Two dated entries for one model, in the shape the system-prompts page ships:
# tagged sections, one sentence per line. Between them exactly three changes --
# one reworded sentence, one removed, one added -- so the rendered marks can be
# counted rather than eyeballed.
_PROMPT_PREV = """<product_information>
The currently selected version of Claude is Claude Opus 4.8.
Claude is accessible via an API and Claude Platform.
Claude is accessible via this web-based chat interface.
There are no other Anthropic products.
</product_information>
<tone_and_formatting>
Claude keeps its responses concise.
Claude avoids unnecessary preamble.
Claude uses markdown only where it helps.
</tone_and_formatting>"""

_PROMPT_CUR = """<product_information>
The currently selected version of Claude is Claude Opus 5.
Claude is accessible via an API and Claude Platform.
Claude is accessible via this web-based chat interface.
</product_information>
<tone_and_formatting>
Claude keeps its responses concise.
Claude avoids unnecessary preamble.
Claude uses markdown only where it helps.
Claude can illustrate its explanations with examples.
</tone_and_formatting>"""

_LABEL = "Claude Opus 4.8 / November 24, 2025"


def _marked(digest: str, mark: str) -> list[str]:
    return [line for line in digest.splitlines() if line.startswith(f"{mark} ")]


def test_changelog_delta_headings_passes_the_body_through_whole():
    """A release-notes body already IS the delta, so nothing is diffed or ranked."""
    body = (
        "* The Compliance API now returns transcripts of Cowork sessions.\n"
        "* We've added the `anthropic-workspace-id` response header."
    )

    digest = changelog_delta(body, None, "headings")

    assert digest.splitlines()[0] == "CHANGES ANNOUNCED (2 items):"
    assert "The Compliance API now returns transcripts of Cowork sessions." in digest
    assert "We've added the `anthropic-workspace-id` response header." in digest
    assert "…" not in digest


def test_changelog_delta_headings_ignores_a_predecessor_body():
    """Consecutive release-notes sections are unrelated announcements."""
    body = "* We launched `claude-opus-5`."

    assert changelog_delta(body, "* Something else entirely.", "headings") == changelog_delta(
        body, None, "headings"
    )


def test_changelog_delta_headings_collapses_markdown_links_to_anchor_text():
    """Link targets eat the budget; the sentence around them is the news."""
    body = (
        "* The [Compliance API](https://platform.claude.com/docs/en/manage-claude/compliance-api) "
        "now returns local sessions. "
        "See [Sessions on users' machines](https://platform.claude.com/docs/en/x#retrieve)."
    )

    digest = changelog_delta(body, None, "headings")

    assert "The Compliance API now returns local sessions." in digest
    # Over-eager stripping deletes the pointer sentence along with the target.
    assert "See Sessions on users' machines." in digest
    assert "https://" not in digest


def test_changelog_delta_same_model_emits_added_removed_and_edited_lines():
    digest = changelog_delta(_PROMPT_CUR, _PROMPT_PREV, "accordions", predecessor_label=_LABEL)

    added, removed, edited = _marked(digest, "+"), _marked(digest, "-"), _marked(digest, "~")
    assert len(added) == 1
    assert len(removed) == 1
    assert len(edited) == 1
    assert added[0] == (
        "+ [tone_and_formatting] Claude can illustrate its explanations with examples."
    )
    assert removed[0] == "- [product_information] There are no other Anthropic products."
    assert edited[0].startswith("~ [product_information] ")
    assert "[-4.8.-]" in edited[0]
    assert "{+5.+}" in edited[0]


def test_changelog_delta_same_model_reports_the_change_count_in_the_header():
    digest = changelog_delta(_PROMPT_CUR, _PROMPT_PREV, "accordions", predecessor_label=_LABEL)

    assert re.fullmatch(
        rf"DELTA vs {re.escape(_LABEL)}: \d+ of \d+ sentences/tags changed \(\d+%\); "
        r"\+1 added, -1 removed, ~1 edited\.",
        digest.splitlines()[0],
    )


def test_changelog_delta_major_rewrite_is_a_label_and_not_a_gate():
    """A high change ratio is a fact to report, never a reason to drop the diff."""
    prev = "<a>\nClaude was helpful.\nClaude was concise.\n</a>"
    cur = "<a>\nClaude answers in French.\nPricing moved to USD5 per MTok.\n</a>"

    digest = changelog_delta(cur, prev, "accordions", predecessor_label=_LABEL)

    assert digest.splitlines()[0].startswith("MAJOR REWRITE vs ")
    assert _marked(digest, "+") or _marked(digest, "~")


def test_changelog_delta_identical_bodies_report_no_textual_change():
    """Two real entries are byte-identical to their predecessor; a 17 KB
    duplicate snapshot is the wrong thing to ship for them."""
    digest = changelog_delta(_PROMPT_CUR, _PROMPT_CUR, "accordions", predecessor_label=_LABEL)

    assert f"NO TEXTUAL CHANGE vs {_LABEL}:" in digest
    assert not _marked(digest, "+")
    assert not _marked(digest, "-")
    assert not _marked(digest, "~")


def test_changelog_delta_without_a_predecessor_returns_a_labelled_profile():
    body = (
        "<product_information>\n"
        "The currently selected version of Claude is Claude Opus 5.\n"
        "They use the API model strings 'claude-opus-5' and 'claude-haiku-4-5-20251001'.\n"
        "Claude's reliable knowledge cutoff is the end of May 2026.\n"
        "</product_information>"
    )

    digest = changelog_delta(body, None, "accordions")
    lines = digest.splitlines()

    assert lines[0].startswith("NEW MODEL ENTRY:")
    assert "MODEL IDS: claude-haiku-4-5-20251001, claude-opus-5" in lines
    assert "KNOWLEDGE CUTOFF: May 2026" in lines
    assert "Sections: <product_information>" in lines
    assert "Opening of the prompt, verbatim:" in lines
    # Units follow in document order, verbatim, with no marker prefix.
    opening = lines.index("Opening of the prompt, verbatim:")
    assert lines[opening + 1] == "<product_information>"
    assert lines[opening + 2] == "The currently selected version of Claude is Claude Opus 5."


def test_changelog_delta_cross_model_hedges_the_lineup_line_and_emits_no_content_diff():
    """A different model's entry is a lineup reference, never a content baseline."""
    prev = (
        "<product_information>\nThe API model string is 'claude-opus-4-8'.\n</product_information>"
    )
    cur = "<product_information>\nThe API model string is 'claude-opus-5'.\n</product_information>"
    label = "Claude Opus 4.8 / December 1, 2025"

    digest = changelog_delta(cur, prev, "accordions", predecessor_label=label, cross_model=True)

    assert (
        f"MODEL LINEUP vs {label} (a DIFFERENT model - absence is not a retirement): "
        "+claude-opus-5 / -claude-opus-4-8" in digest
    )
    assert not _marked(digest, "+")
    assert not _marked(digest, "-")
    assert not _marked(digest, "~")


def test_changelog_delta_does_not_mine_model_ids_out_of_urls():
    """Real doc slugs mint phantom model strings the moment stripping is reordered."""
    body = (
        "<product_information>\n"
        "The API model string is 'claude-opus-5'.\n"
        "See [the tag overview](https://claude.com/docs/claude-tag/overview) and "
        "https://www.anthropic.com/news/claude-fable-5-mythos-5 for details.\n"
        "</product_information>"
    )

    digest = changelog_delta(body, None, "accordions")
    ids_line = next(line for line in digest.splitlines() if line.startswith("MODEL IDS:"))

    assert ids_line == "MODEL IDS: claude-opus-5"


def test_changelog_delta_same_model_reports_added_and_removed_sections():
    prev = "<alpha>\nClaude is helpful.\n</alpha>\n<gone>\nOld rule.\n</gone>"
    cur = "<alpha>\nClaude is helpful.\n</alpha>\n<fresh>\nNew rule.\n</fresh>"

    digest = changelog_delta(cur, prev, "accordions", predecessor_label=_LABEL)

    assert "Sections: +<fresh>, -<gone>" in digest.splitlines()


def test_select_predecessor_finds_the_next_entry_for_the_same_model():
    entries = [
        {"model": "Claude Opus 4.5", "date": "January 18, 2026", "body": "a"},
        {"model": "Claude Opus 4.5", "date": "November 24, 2025", "body": "b"},
        {"model": "Claude Haiku 4.5", "date": "October 15, 2025", "body": "c"},
    ]

    assert select_predecessor(entries, 0) == (1, False)


def test_select_predecessor_falls_back_across_models_by_parsed_date():
    """Models are not globally sorted, so the fallback ranks on the date."""
    entries = [
        {"model": "Claude Haiku 4.5", "date": "October 15, 2025", "body": "c"},
        {"model": "Claude Opus 4.5", "date": "January 18, 2026", "body": "a"},
        {"model": "Claude Sonnet 4.6", "date": "December 1, 2025", "body": "d"},
    ]

    assert select_predecessor(entries, 1) == (2, True)


def test_select_predecessor_returns_nothing_for_the_oldest_entry():
    entries = [
        {"model": "Claude Opus 4.5", "date": "January 18, 2026", "body": "a"},
        {"model": "Claude Haiku 4.5", "date": "October 15, 2025", "body": "c"},
    ]

    assert select_predecessor(entries, 1) == (None, False)


def test_select_predecessor_falls_back_to_the_profile_on_a_reordered_chunk(caplog):
    """An inverted diff would confidently report additions as removals."""
    entries = [
        {"model": "Claude Opus 4.5", "date": "November 24, 2025", "body": "b"},
        {"model": "Claude Opus 4.5", "date": "January 18, 2026", "body": "a"},
    ]

    with caplog.at_level(logging.WARNING, logger="news.changelog_delta"):
        assert select_predecessor(entries, 0) == (None, False)

    assert "Claude Opus 4.5" in caplog.text


def _bulk_prompt(section: str, prefix: str, count: int) -> str:
    body = "\n".join(
        f"{prefix} sentence number {i} carries some prompt text." for i in range(count)
    )
    return f"<{section}>\n{body}\n</{section}>"


def test_changelog_delta_never_exceeds_the_cap_and_never_chops_a_tail_marker():
    prev = _bulk_prompt("alpha", "Earlier", 320) + "\n" + _bulk_prompt("beta", "Shared", 100)
    cur = (
        _bulk_prompt("alpha", "Current", 320)
        + "\n"
        + _bulk_prompt("beta", "Shared", 100)
        + "\n"
        + _bulk_prompt("gamma", "Extra", 75)
    )
    assert len(prev) > 22_000
    assert len(cur) > 26_000

    digest = changelog_delta(cur, prev, "accordions", predecessor_label=_LABEL)

    assert len(digest) <= DIGEST_CAP
    tail = [line for line in digest.splitlines() if line.startswith("…")]
    assert len(tail) == 1
    assert re.fullmatch(r"… \+\d+ more changed passage\(s\) not shown", tail[0])
    also = next(line for line in digest.splitlines() if line.startswith("Also changed, not shown:"))
    assert "alpha (" in also


def test_changelog_delta_headings_never_exceeds_the_cap():
    body = "\n".join(f"* Release item {i}: " + "detail " * 40 for i in range(60))

    digest = changelog_delta(body, None, "headings")

    assert len(digest) <= DIGEST_CAP
    assert digest.splitlines()[0] == "CHANGES ANNOUNCED (60 items):"
    assert digest.splitlines()[-1].startswith("… +")
    assert digest.splitlines()[-1].endswith("more item(s) not shown")


def test_changelog_delta_profile_never_exceeds_the_cap():
    digest = changelog_delta(_bulk_prompt("alpha", "Fresh", 300), None, "accordions")

    assert len(digest) <= DIGEST_CAP
    assert digest.splitlines()[-1].endswith("more sentence(s) of the prompt not shown")


def test_changelog_delta_is_deterministic_across_hash_seeds():
    """Set or dict iteration order leaking into the greedy pairing would make
    every run re-report the same entry differently.

    Five calls inside ONE interpreter cannot show this: PYTHONHASHSEED is fixed
    for the life of a process, so str hashing is already stable and the test
    passes whether or not the pairing iterates a set. Only separate interpreters
    with different seeds exercise the property the name claims.
    """
    script = (
        "from news.changelog_delta import changelog_delta\n"
        "prev = 'A. ' + ' '.join(f'Line {i} was here.' for i in range(40))\n"
        "cur = 'A. ' + ' '.join(f'Line {i} is here now.' for i in range(40))\n"
        "print(changelog_delta(cur, prev, 'accordions', predecessor_label='M / Jan 1, 2026'))"
    )
    outputs = set()
    for seed in ("0", "1", "42"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            cwd=Path(__file__).resolve().parent.parent,
        )
        assert result.returncode == 0, result.stderr
        outputs.add(result.stdout)

    assert len(outputs) == 1


def test_changelog_delta_never_raises_on_junk_input():
    """Parse time has no error path: a malformed body must degrade, not throw."""
    for body in ("", "   ", "</unopened>\n&#x49;f Claude \\* is here.", "\\n\\n"):
        assert isinstance(changelog_delta(body, None, "accordions"), str)
        assert isinstance(changelog_delta(body, body, "headings"), str)


# --- Regressions found by adversarial review of the first cut -----------------


def test_headings_spends_its_budget_instead_of_gutting_every_bullet():
    """A launch note is the only headings entry that ever reaches the email, and
    it was the one being cut hardest: shortening EVERY bullet to its lead
    sentence and stopping there dropped the breaking changes a pinned-model
    reader needs while leaving a third of the cap unspent."""
    long_bullet = (
        "* We've launched a model at introductory pricing. It supports a 1M token "
        "context window and 128k max output tokens. Manual extended thinking is "
        "removed and returns a 400 error. A new tokenizer produces 30% more tokens."
    )
    filler = "\n".join(f"* Minor item {i}. It has a second sentence too." for i in range(40))
    digest = changelog_delta(f"{long_bullet}\n{filler}", None, "headings")

    assert "400 error" in digest, "the breaking change was dropped without a marker"
    assert "1M token" in digest
    # The lead-sentence pass must not leave a large part of the cap unused.
    assert len(digest) > DIGEST_CAP * 0.9
    assert len(digest) <= DIGEST_CAP


def test_headings_marks_a_bullet_it_shortened():
    """Silent first-sentence truncation reads as a complete list of changes."""
    bullets = "\n".join(
        f"* Item {i} headline. Item {i} detail sentence that will not survive the cap."
        for i in range(60)
    )
    digest = changelog_delta(bullets, None, "headings")

    assert "…" in digest, "a shortened bullet must show that it was cut"


def test_headings_does_not_weld_a_standalone_paragraph_onto_the_bullet_above():
    """The page writes lead-in paragraphs between bullet groups. Treating every
    non-bullet line as a wrapped continuation misattributed them to the bullet
    above, which is a factual error in the digest, not a formatting one."""
    body = "* We shipped PDF support.\n\nWe also released new official SDKs:\n\n* Java SDK.\n"
    digest = changelog_delta(body, None, "headings")

    assert "PDF support. We also released" not in digest
    assert digest.startswith("CHANGES ANNOUNCED (3 items):")


def test_knowledge_cutoff_reads_the_phrasings_the_page_actually_ships():
    """Three of 29 live entries had no cutoff line: the newer 'Jan 2026' short
    form was unmatched, and 'is the beginning of' pushed one entry six chars
    past the old 80-char window. A missing line looks like an absent fact."""
    cases = {
        "Claude's reliable knowledge cutoff is the end of May 2026.": "May 2026",
        "Claude's reliable knowledge cutoff is the end of Jan 2026.": "January 2026",
        (
            "Claude's reliable knowledge cutoff date - the date past which it cannot "
            "answer questions reliably - is the beginning of August 2025."
        ): "August 2025",
    }
    for sentence, expected in cases.items():
        digest = changelog_delta(f"<product_information>\n{sentence}", None, "accordions")
        assert f"KNOWLEDGE CUTOFF: {expected}" in digest, sentence


def test_normalize_survives_an_out_of_range_character_reference():
    """chr() raises above U+10FFFF, and the fetcher's bare except would turn that
    into a source that silently yields zero articles for the whole page."""
    digest = changelog_delta("Body with &#x110000; and &#99999999999; refs.", None, "headings")

    assert "&#x110000;" in digest
