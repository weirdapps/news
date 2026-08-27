"""Every profile's validator must agree with the prompt that profile actually sends.

Written after 27 Aug 2026, when `f7eefe1` promoted a missing `sections` list from a
logged warning to a fatal schema error. Four of the five profiles emit `sections` and
were unaffected. The brand monitor does not: its report is company_mentions / alerts /
competitor_watch. So the first monitor run after the deploy rejected two structurally
sound payloads, exhausted the UNPARSEABLE retry cap, and shipped "synthesis
unavailable" instead of a digest.

Nothing caught it because each profile's schema lived only inside its own prompt
string, and no test had ever fed a profile's advertised shape back through the checker
that judges it. The invariant below is the one that was silently false:

    every key a profile's validator treats as FATAL must appear in that profile's
    prompt, and a payload matching that prompt must pass that validator.
"""

import json

import pytest

from news.market_synth import _SYSTEM_PROMPT as MARKET_PROMPT
from news.monitor_synth import MONITOR_REQUIRED_KEYS, _monitor_validate, _output_format_section
from news.reviewer import _SYSTEM_PROMPT as REVIEWER_PROMPT
from news.reviewer import REVIEW_REQUIRED_KEYS
from news.stack_synth import _SYSTEM_PROMPT as STACK_PROMPT
from news.synthesizer import _SYSTEM_PROMPT as DIGEST_PROMPT
from news.synthesizer import DIGEST_REQUIRED_KEYS, parse_synthesis_output
from news.topic_synth import _topic_output_format


def _digest_validate(text: str) -> bool:
    """The callback the four digest-family profiles pass to invoke_claude."""
    return "error" not in parse_synthesis_output(text)


def _reviewer_validate(text: str) -> bool:
    """Mirrors the inline lambda in reviewer.review_synthesis, keyed off the same set."""
    return isinstance(
        parse_synthesis_output(text, required=REVIEW_REQUIRED_KEYS).get("verdicts"), list
    )


_DIGEST_FAMILY_PAYLOAD = {
    "executive_brief": [{"text": "A bullet", "article_ids": [0]}],
    "what_changed": [{"text": "Something moved", "article_ids": [0]}],
    "sections": [
        {
            "category": "banking",
            "display_name": "Banking",
            "synthesis": "Two paragraphs.",
            "sources": ["Reuters"],
            "article_ids": [0],
            "high_value": True,
        }
    ],
}

_MONITOR_PAYLOAD = {
    "mention_count": 1,
    "new_since_last": 1,
    "sentiment_summary": {"positive": 1, "negative": 0, "neutral": 0, "trend": "improving"},
    "alerts": [{"text": "An alert", "article_ids": [0]}],
    "company_mentions": [
        {
            "title": "A headline",
            "source": "Kathimerini",
            "type": "news",
            "sentiment": "positive",
            "summary": "A mention.",
            "relevance": "high",
            "article_ids": [0],
        }
    ],
    "sector_context": "Sector paragraph.",
    "competitor_watch": {"piraeus": {"summary": "Brief.", "article_ids": [0]}},
    "executive_brief": [{"text": "A bullet", "article_ids": [0]}],
}

_REVIEWER_PAYLOAD = {
    "verdicts": [{"id": 0, "supported": True, "reason": "the cited article says so"}],
    "contradictions": [],
}

# (profile, the prompt text carrying its OUTPUT FORMAT block, its validate callback,
#  the keys that callback treats as fatal, a payload matching that prompt)
PROFILES = [
    ("digest", DIGEST_PROMPT, _digest_validate, DIGEST_REQUIRED_KEYS, _DIGEST_FAMILY_PAYLOAD),
    ("market", MARKET_PROMPT, _digest_validate, DIGEST_REQUIRED_KEYS, _DIGEST_FAMILY_PAYLOAD),
    ("stack", STACK_PROMPT, _digest_validate, DIGEST_REQUIRED_KEYS, _DIGEST_FAMILY_PAYLOAD),
    (
        "topic",
        _topic_output_format(),
        _digest_validate,
        DIGEST_REQUIRED_KEYS,
        _DIGEST_FAMILY_PAYLOAD,
    ),
    (
        "monitor",
        _output_format_section("Acme"),
        _monitor_validate,
        MONITOR_REQUIRED_KEYS,
        _MONITOR_PAYLOAD,
    ),
    (
        "reviewer",
        REVIEWER_PROMPT,
        _reviewer_validate,
        REVIEW_REQUIRED_KEYS,
        _REVIEWER_PAYLOAD,
    ),
]

_IDS = [p[0] for p in PROFILES]


@pytest.mark.parametrize("profile,prompt,validate,required,payload", PROFILES, ids=_IDS)
def test_every_fatal_key_is_a_key_the_prompt_actually_asks_for(
    profile, prompt, validate, required, payload
):
    """The 27 Aug failure, stated as an assertion.

    A validator may only condemn a payload for a key the model was told to produce.
    Requiring `sections` of a monitor that is never asked for one is not strictness,
    it is a guaranteed rejection.
    """
    for key in required:
        assert f'"{key}"' in prompt, (
            f"{profile}: validator treats {key!r} as fatal, but its prompt never asks for it"
        )


@pytest.mark.parametrize("profile,prompt,validate,required,payload", PROFILES, ids=_IDS)
def test_a_prompt_that_asks_for_sections_asks_for_every_key_they_are_kept_by(
    profile, prompt, validate, required, payload
):
    """The same mismatch one level down, and it was live too.

    _validate_synthesis DROPS any section missing category/display_name/synthesis.
    The topic prompt advertised sections without `category`, so every section it ever
    produced was silently discarded, leaving an empty-but-valid brief: no error, no
    retry, no sections. Quieter than the monitor's failure and therefore worse.
    """
    if '"sections"' not in prompt:
        pytest.skip(f"{profile} does not render sections")
    for key in ("category", "display_name", "synthesis"):
        assert f'"{key}"' in prompt, (
            f"{profile}: sections missing {key!r} are dropped, but its prompt never asks for one"
        )


@pytest.mark.parametrize("profile,prompt,validate,required,payload", PROFILES, ids=_IDS)
def test_every_profile_accepts_its_own_advertised_schema(
    profile, prompt, validate, required, payload
):
    assert validate(json.dumps(payload)), f"{profile}: its own schema failed its own validator"


@pytest.mark.parametrize("profile,prompt,validate,required,payload", PROFILES, ids=_IDS)
def test_every_profile_still_rejects_output_it_cannot_render(
    profile, prompt, validate, required, payload
):
    """Loosening a contract must not disarm it: garbage still has to spend a retry."""
    assert not validate("I was unable to complete this request."), f"{profile}: prose accepted"
    assert not validate('["a bare array, not an object"]'), f"{profile}: bare array accepted"
    mistyped = required[0]
    assert not validate(json.dumps({**payload, mistyped: "not a list"})), (
        f"{profile}: a mistyped {mistyped} was accepted"
    )


@pytest.mark.parametrize("profile,prompt,validate,required,payload", PROFILES, ids=_IDS)
def test_an_editorially_empty_run_is_never_a_schema_failure(
    profile, prompt, validate, required, payload
):
    """Quiet day, not broken model. Rejecting it burns a retry and then withholds."""
    empty = {key: [] for key in required}
    assert validate(json.dumps(empty)), f"{profile}: an empty but well-formed payload was rejected"


def test_the_monitor_is_not_silently_given_the_digest_schema_again():
    """A guard on the specific regression, independent of the table above."""
    assert "sections" not in MONITOR_REQUIRED_KEYS
    assert '"sections"' not in _output_format_section("Acme"), (
        "the monitor prompt now asks for sections; add it to MONITOR_REQUIRED_KEYS"
    )
    assert "sections" in DIGEST_REQUIRED_KEYS, "the digest family still renders sections"


def test_the_reviewer_does_not_log_a_schema_error_against_the_digest_keys():
    """The third instance of the same mismatch, found by the log line added with the fix.

    The reviewer validated on `verdicts`, which is correct, but the parse underneath it
    still ran the digest's required set. So every review logged two ERRORs for keys the
    reviewer is never asked for, and stamped a spurious `error` on its own output. It
    worked only because this profile happens not to check for `error`. Two false ERRORs
    per run is how a real one becomes invisible.
    """
    payload = json.dumps(_REVIEWER_PAYLOAD)
    clean = parse_synthesis_output(payload, required=REVIEW_REQUIRED_KEYS)
    assert "error" not in clean, clean.get("error")
    assert isinstance(clean["verdicts"], list)
    # and the default set still condemns it, which is why the reviewer must not use it
    assert "error" in parse_synthesis_output(payload)
