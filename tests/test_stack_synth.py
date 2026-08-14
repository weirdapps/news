"""Tests for stack synthesis prompt construction."""

from news.models import Article
from news.stack_synth import build_stack_prompt


def _article(
    url: str = "https://example.com/story",
    source: str = "TechCrunch",
    content: str = "",
    transcript_abstract: str = "",
) -> Article:
    return Article(
        url=url,
        title="A Story",
        source=source,
        content=content,
        categories=["tech"],
        language="en",
        transcript_abstract=transcript_abstract,
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
