"""Tests for AI synthesis layer."""

import json
import subprocess
from datetime import UTC, datetime
from unittest.mock import Mock, patch

from news.llm_policy import Outcome, ReauthResult
from news.models import Article
from news.synthesizer import (
    _classify,
    build_fallback_digest,
    build_prompt,
    invoke_claude,
    parse_synthesis_output,
)


def _make_articles():
    """Helper to create test articles."""
    now = datetime.now(UTC)
    return [
        Article(
            url="https://example.com/1",
            title="NBG Record Profits",
            source="Reuters",
            content="National Bank of Greece reported record profits.",
            categories=["banking"],
            language="en",
            relevance_score=70,
            fetched_at=now,
            published_at=now,
        ),
        Article(
            url="https://example.com/2",
            title="Claude Code Update",
            source="TechCrunch",
            content="Anthropic released a major Claude Code update.",
            categories=["ai"],
            language="en",
            relevance_score=45,
            fetched_at=now,
            published_at=now,
        ),
    ]


def test_build_prompt_includes_articles():
    """Verify prompt contains article titles, time window, previous highlights."""
    articles = _make_articles()
    previous_highlights = ["Previous highlight 1", "Previous highlight 2"]
    time_window = "last 24 hours"

    prompt = build_prompt(articles, previous_highlights, time_window)

    assert "NBG Record Profits" in prompt
    assert "Claude Code Update" in prompt
    assert "last 24 hours" in prompt
    assert "Previous highlight 1" in prompt
    assert "Previous highlight 2" in prompt


def test_build_prompt_includes_name_handling_rules():
    """Digest prompt must include the brand-neutral name-handling rules.

    The digest pipeline has no brand context (it's broad news, not brand
    monitoring), so it gets the generic NAME_HANDLING_RULES only — no
    EXECUTIVE NAME ROSTER section.
    """
    prompt = build_prompt(_make_articles(), [], "24h")

    assert "NEVER invent first names" in prompt
    assert "NAME HANDLING RULES" in prompt
    # Digest has no brand-specific roster
    assert "EXECUTIVE NAME ROSTER" not in prompt


def test_build_prompt_requests_json_output():
    """Verify 'JSON' appears in prompt."""
    articles = _make_articles()

    prompt = build_prompt(articles, [], "24h")

    assert "JSON" in prompt or "json" in prompt


def test_parse_synthesis_output_valid_json():
    """Parse a clean JSON string with all required fields."""
    raw = json.dumps(
        {
            "executive_brief": ["Item 1", "Item 2", "Item 3", "Item 4", "Item 5"],
            "what_changed": "Major changes occurred.",
            "sections": [
                {
                    "category": "banking",
                    "display_name": "Banking",
                    "synthesis": "Banking sector analysis.",
                    "opposing_views": "Some disagree.",
                    "fact_check": "All facts verified.",
                    "sources": ["Reuters"],
                }
            ],
        }
    )

    result = parse_synthesis_output(raw)

    # Bare strings are coerced to {"text": ..., "article_ids": []} dicts
    assert len(result["executive_brief"]) == 5
    assert result["executive_brief"][0] == {"text": "Item 1", "article_ids": []}
    assert result["executive_brief"][4] == {"text": "Item 5", "article_ids": []}
    # Bare string what_changed coerced to list of dicts
    assert result["what_changed"] == [{"text": "Major changes occurred.", "article_ids": []}]
    assert len(result["sections"]) == 1
    assert result["sections"][0]["category"] == "banking"
    assert result["sections"][0]["display_name"] == "Banking"
    # Missing fields get defaults
    assert result["sections"][0]["high_value"] is False
    assert result["sections"][0]["article_ids"] == []


def test_parse_synthesis_output_extracts_json_from_prose():
    """Parse JSON embedded in markdown code block."""
    data = {
        "executive_brief": ["Brief 1", "Brief 2", "Brief 3", "Brief 4", "Brief 5"],
        "what_changed": "Changes noted.",
        "sections": [],
    }
    raw = f"Here is the analysis:\n```json\n{json.dumps(data)}\n```\nEnd of response."

    result = parse_synthesis_output(raw)

    assert len(result["executive_brief"]) == 5
    assert result["executive_brief"][0] == {"text": "Brief 1", "article_ids": []}
    assert result["what_changed"] == [{"text": "Changes noted.", "article_ids": []}]


def test_build_fallback_digest():
    """Verify fallback contains article titles from sources."""
    articles = _make_articles()

    fallback = build_fallback_digest(articles)

    assert "SYNTHESIS UNAVAILABLE" in fallback
    assert "NBG Record Profits" in fallback
    assert "Claude Code Update" in fallback
    assert "Reuters" in fallback
    assert "TechCrunch" in fallback
    assert "https://example.com/1" in fallback
    assert "https://example.com/2" in fallback


def _envelope(result="{}", stop_reason="end_turn", is_error=False):
    """Build a `claude --output-format json` stdout envelope (what invoke_claude parses)."""
    return json.dumps({"result": result, "stop_reason": stop_reason, "is_error": is_error})


@patch("news.synthesizer.subprocess.run")
def test_invoke_claude_success(mock_run):
    """invoke_claude returns the envelope's `result` text on a clean turn."""
    expected_output = '{"executive_brief": [], "sections": []}'
    mock_run.return_value = Mock(stdout=_envelope(result=expected_output), returncode=0)

    result = invoke_claude("test prompt", timeout=60)

    assert result == expected_output
    mock_run.assert_called_once()
    args = mock_run.call_args
    assert args[1]["input"] == "test prompt"
    assert args[1]["capture_output"] is True
    assert args[1]["text"] is True
    assert args[1]["timeout"] == 60
    # Must request the JSON envelope so stop_reason (refusal detection) is available.
    assert "--output-format" in args[0][0] and "json" in args[0][0]


@patch("news.synthesizer.subprocess.run")
def test_invoke_claude_timeout(mock_run):
    """Mock subprocess.run raising TimeoutError."""
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=60)

    result = invoke_claude("test prompt", timeout=60)

    assert result is None


@patch("news.synthesizer.subprocess.run")
def test_invoke_claude_resolves_opus_alias_to_vertex_heavy_model(mock_run, monkeypatch):
    """opus tier must resolve to the exact provisioned model id + eu region.

    The bare 'opus' alias hits an unprovisioned eu quota bucket (429 → unprocessed
    fallback). Synthesis must use the central-env heavy-tier id instead.
    """
    monkeypatch.setenv("VERTEX_MODEL_HEAVY", "claude-opus-4-8[1m]")
    monkeypatch.setenv("VERTEX_REGION_HEAVY", "eu")
    mock_run.return_value = Mock(stdout=_envelope(), returncode=0)

    invoke_claude("p", claude_args=["--print", "--model", "opus"])

    cmd = mock_run.call_args[0][0]
    run_env = mock_run.call_args[1]["env"]
    assert "claude-opus-4-8[1m]" in cmd
    assert "opus" not in cmd  # the bare alias must never reach the CLI
    assert run_env["CLOUD_ML_REGION"] == "eu"


@patch("news.synthesizer.subprocess.run")
def test_invoke_claude_resolves_sonnet_alias_to_vertex_light_model(mock_run, monkeypatch):
    """sonnet tier must resolve to the exact light-tier id + europe-west1 region."""
    monkeypatch.setenv("VERTEX_MODEL_LIGHT", "claude-sonnet-4-6")
    monkeypatch.setenv("VERTEX_REGION_LIGHT", "europe-west1")
    mock_run.return_value = Mock(stdout=_envelope(), returncode=0)

    invoke_claude("p", claude_args=["--print", "--model", "sonnet"])

    cmd = mock_run.call_args[0][0]
    run_env = mock_run.call_args[1]["env"]
    assert "claude-sonnet-4-6" in cmd
    assert "sonnet" not in cmd
    assert run_env["CLOUD_ML_REGION"] == "europe-west1"


@patch("news.synthesizer.subprocess.run")
def test_invoke_claude_opus_falls_back_to_correct_default_when_env_absent(mock_run, monkeypatch):
    """With no central env present, opus must still resolve to the working id.

    Guards against ever emitting the broken bare 'opus' alias.
    """
    monkeypatch.delenv("VERTEX_MODEL_HEAVY", raising=False)
    monkeypatch.delenv("VERTEX_REGION_HEAVY", raising=False)
    mock_run.return_value = Mock(stdout=_envelope(), returncode=0)

    invoke_claude("p", claude_args=["--model", "opus"])

    cmd = mock_run.call_args[0][0]
    run_env = mock_run.call_args[1]["env"]
    assert "claude-opus-5[1m]" in cmd
    assert "opus" not in cmd
    assert run_env["CLOUD_ML_REGION"] == "eu"


@patch("news.synthesizer.subprocess.run")
def test_invoke_claude_retries_refusal_on_same_tier(mock_run, monkeypatch):
    """A policy refusal triggers PLAIN_RETRY on the same tier — no model downgrade.

    Before the policy port, a refusal caused a hard retry on VERTEX_MODEL_FALLBACK
    (which was byte-identical to VERTEX_MODEL_HEAVY: both claude-opus-5[1m] at eu,
    per the 2026-08-09 owner decision). The shared policy's REFUSAL row (cap=2)
    now owns the retry; VERTEX_MODEL_FALLBACK is no longer consulted.

    Old behaviour: second call used ``VERTEX_MODEL_FALLBACK`` / ``VERTEX_REGION_FALLBACK``.
    New behaviour: second call uses the same tier as the first (PLAIN_RETRY, cap=2).
    """
    monkeypatch.setenv("VERTEX_MODEL_HEAVY", "claude-opus-4-8[1m]")
    monkeypatch.setenv("VERTEX_REGION_HEAVY", "eu")
    monkeypatch.setenv("VERTEX_MODEL_FALLBACK", "claude-opus-4-6[1m]")  # no longer consulted
    monkeypatch.setenv("VERTEX_REGION_FALLBACK", "europe-west1")
    mock_run.side_effect = [
        Mock(
            stdout=_envelope(result="I can't help with that.", stop_reason="refusal"),
            returncode=0,
        ),
        Mock(stdout=_envelope(result="SYNTH_OK"), returncode=0),
    ]

    result = invoke_claude("p", claude_args=["--print", "--model", "opus"])

    assert mock_run.call_count == 2
    second_cmd = mock_run.call_args_list[1][0][0]
    second_env = mock_run.call_args_list[1][1]["env"]
    assert "claude-opus-4-8[1m]" in second_cmd  # same tier, not fallback
    assert "claude-opus-4-6[1m]" not in second_cmd
    assert second_env["CLOUD_ML_REGION"] == "eu"
    assert result == "SYNTH_OK"


@patch("news.synthesizer.subprocess.run")
def test_invoke_claude_retries_rate_limit_on_same_tier(mock_run, monkeypatch):
    """A 429/rate-limit error triggers PLAIN_RETRY on the same tier — no model downgrade.

    Before the policy port, an is_error envelope caused a hard retry on
    VERTEX_MODEL_FALLBACK. The shared policy's RATE_LIMIT row (cap=3) now owns
    the retry; VERTEX_MODEL_FALLBACK is no longer consulted.

    Old behaviour: second call used ``VERTEX_MODEL_FALLBACK`` / ``VERTEX_REGION_FALLBACK``.
    New behaviour: second call uses the same tier as the first (PLAIN_RETRY, cap=3).
    """
    monkeypatch.setenv("VERTEX_MODEL_HEAVY", "claude-opus-4-8[1m]")
    monkeypatch.setenv("VERTEX_REGION_HEAVY", "eu")
    monkeypatch.setenv("VERTEX_MODEL_FALLBACK", "claude-opus-4-6[1m]")  # no longer consulted
    monkeypatch.setenv("VERTEX_REGION_FALLBACK", "europe-west1")
    mock_run.side_effect = [
        Mock(
            stdout=_envelope(
                result="API Error: 429 quota",
                stop_reason="stop_sequence",
                is_error=True,
            ),
            returncode=0,
        ),
        Mock(stdout=_envelope(result="RECOVERED"), returncode=0),
    ]

    result = invoke_claude("p", claude_args=["--print", "--model", "opus"])

    assert mock_run.call_count == 2
    second_cmd = mock_run.call_args_list[1][0][0]
    assert "claude-opus-4-8[1m]" in second_cmd  # same tier, not fallback
    assert "claude-opus-4-6[1m]" not in second_cmd
    assert result == "RECOVERED"


def test_build_prompt_requires_article_ids_citations():
    """Digest prompt must require article_ids per bullet and section."""
    prompt = build_prompt(_make_articles(), [], "24h")

    assert "article_ids" in prompt
    assert "CITATION REQUIREMENT" in prompt
    # The schema must show article_ids on bullets and sections
    assert '"text"' in prompt


# --- Auth self-heal (invalid_rapt / reauth) -------------------------------

_RAPT_ERR = (
    '{"error":"invalid_grant","error_description":'
    '"reauth related error (invalid_rapt)","error_subtype":"invalid_rapt"}'
)


@patch("news.synthesizer.reauth")
@patch("news.synthesizer.subprocess.run")
def test_invoke_claude_reauths_on_invalid_rapt_then_retries_same_tier(
    mock_run, mock_reauth, monkeypatch
):
    """An invalid_rapt auth error must trigger reauth() and retry the SAME
    model/region — NOT a model downgrade (credential failure, not quota).

    Old behaviour: called ``news.auth.refresh_auth()`` returning bool.
    New behaviour: calls ``news.llm_policy.reauth()`` returning ReauthResult.
    """
    monkeypatch.setenv("VERTEX_MODEL_HEAVY", "claude-opus-4-8[1m]")
    monkeypatch.setenv("VERTEX_REGION_HEAVY", "eu")
    monkeypatch.setenv("VERTEX_MODEL_FALLBACK", "claude-opus-4-6[1m]")  # no longer consulted
    monkeypatch.setenv("VERTEX_REGION_FALLBACK", "europe-west1")
    mock_run.side_effect = [
        Mock(stdout=_envelope(result=_RAPT_ERR, is_error=True), returncode=0),
        Mock(stdout=_envelope(result="SYNTH_OK"), returncode=0),
    ]
    mock_reauth.return_value = ReauthResult.SUCCEEDED

    result = invoke_claude("p", claude_args=["--print", "--model", "opus"])

    mock_reauth.assert_called_once()
    assert mock_run.call_count == 2
    second_cmd = mock_run.call_args_list[1][0][0]
    second_env = mock_run.call_args_list[1][1]["env"]
    assert "claude-opus-4-8[1m]" in second_cmd  # retried same tier
    assert "claude-opus-4-6[1m]" not in second_cmd  # NOT downgraded
    assert second_env["CLOUD_ML_REGION"] == "eu"
    assert result == "SYNTH_OK"


@patch("news.synthesizer.reauth")
@patch("news.synthesizer.subprocess.run")
def test_invoke_claude_returns_none_when_reauth_fails(mock_run, mock_reauth, monkeypatch):
    """If reauth() fails the one-shot budget is still spent, so the next auth
    error produces UNRECOVERABLE_AUTH and synthesis returns None.

    Old behaviour: called ``refresh_auth()`` (bool); 1 subprocess call, returned None.
    New behaviour: calls ``reauth()`` (ReauthResult); 2 subprocess calls (one before
    reauth, one after the failed reauth), then UNRECOVERABLE_AUTH → None.
    """
    monkeypatch.setenv("VERTEX_MODEL_HEAVY", "claude-opus-4-8[1m]")
    monkeypatch.setenv("VERTEX_REGION_HEAVY", "eu")
    mock_run.return_value = Mock(stdout=_envelope(result=_RAPT_ERR, is_error=True), returncode=0)
    mock_reauth.return_value = ReauthResult.FAILED

    result = invoke_claude("p", claude_args=["--print", "--model", "opus"])

    assert result is None
    mock_reauth.assert_called_once()
    # 2 calls: auth error → reauth (FAILED, budget spent) → retry → auth error again →
    # reauth_used=True → UNRECOVERABLE_AUTH → give up
    assert mock_run.call_count == 2


@patch("news.synthesizer.reauth")
@patch("news.synthesizer.subprocess.run")
def test_invoke_claude_429_does_not_trigger_reauth(mock_run, mock_reauth, monkeypatch):
    """A 429/quota error is NOT auth-class: it must PLAIN_RETRY, never re-auth.

    This test guards ``_is_auth_error`` staying narrow: if "429" ever matched an
    auth marker, rate-limit errors would trigger a pointless gcloud reauth instead
    of the RATE_LIMIT retry row.

    Old behaviour: patched ``refresh_auth``; asserted fallback-model downgrade.
    New behaviour: patches ``reauth``; asserts same-tier retry (no downgrade).
    """
    monkeypatch.setenv("VERTEX_MODEL_HEAVY", "claude-opus-4-8[1m]")
    monkeypatch.setenv("VERTEX_REGION_HEAVY", "eu")
    monkeypatch.setenv("VERTEX_MODEL_FALLBACK", "claude-opus-4-6[1m]")  # no longer consulted
    monkeypatch.setenv("VERTEX_REGION_FALLBACK", "europe-west1")
    mock_run.side_effect = [
        Mock(stdout=_envelope(result="API Error: 429 quota", is_error=True), returncode=0),
        Mock(stdout=_envelope(result="RECOVERED"), returncode=0),
    ]

    result = invoke_claude("p", claude_args=["--print", "--model", "opus"])

    mock_reauth.assert_not_called()
    second_cmd = mock_run.call_args_list[1][0][0]
    assert "claude-opus-4-8[1m]" in second_cmd  # same tier, not fallback
    assert "claude-opus-4-6[1m]" not in second_cmd
    assert result == "RECOVERED"


# --- _classify: nine failure paths → Outcome -----------------------------------


def test_timeout_is_its_own_outcome():
    assert _classify(None, None, subprocess.TimeoutExpired("claude", 300)) is Outcome.TIMEOUT


def test_other_subprocess_exceptions_are_api_errors():
    assert _classify(None, None, OSError("claude not found")) is Outcome.API_ERROR


def test_empty_stdout_is_empty_not_unparseable():
    assert _classify(None, "   ", None) is Outcome.EMPTY


def test_non_json_stdout_is_unparseable():
    assert _classify(None, "API Error: invalid_grant", None) is Outcome.UNPARSEABLE


def test_an_auth_marker_in_the_envelope_is_auth():
    env = {"is_error": True, "result": "API Error: invalid_grant"}
    assert _classify(env, None, None) is Outcome.AUTH_REAUTH_REQUIRED


def test_a_quota_error_is_not_auth():
    # _is_auth_error is deliberately narrow: a 429 must not trigger a re-auth.
    env = {"is_error": True, "result": "429 RESOURCE_EXHAUSTED quota exceeded"}
    assert _classify(env, None, None) is Outcome.RATE_LIMIT


def test_a_refusal_is_a_refusal():
    env = {"stop_reason": "refusal", "result": "I cannot help with that"}
    assert _classify(env, None, None) is Outcome.REFUSAL


def test_a_generic_error_envelope_is_an_api_error():
    env = {"is_error": True, "result": "500 internal"}
    assert _classify(env, None, None) is Outcome.API_ERROR


def test_an_empty_result_field_is_empty():
    env = {"result": "   "}
    assert _classify(env, None, None) is Outcome.EMPTY


def test_a_good_envelope_is_ok():
    env = {"result": "the synthesis"}
    assert _classify(env, None, None) is Outcome.OK


# --- Reachability: each _run_once failure path must reach the policy loop ----------
#
# These tests drive the full invoke_claude path. Before Task 3, _run_once collapsed
# all four failure conditions into a bare None; _classify(None, None, None) returned
# EMPTY, and EMPTY's tighter cap meant a timeout got fewer paid retries than the
# decision table specified. The tests below assert on subprocess call counts that
# DIFFER between outcome caps, proving each path reaches the policy independently.


@patch("news.synthesizer.time.sleep")
@patch("news.synthesizer.subprocess.run")
def test_a_good_first_call_costs_exactly_one_invocation(mock_run, mock_sleep):
    """Outcome.OK short-circuits the policy loop immediately."""
    mock_run.return_value = Mock(stdout=_envelope(result="the synthesis"), returncode=0)
    out = invoke_claude("prompt", timeout=300)
    assert out == "the synthesis"
    assert mock_run.call_count == 1
    assert mock_sleep.call_count == 0


@patch("news.synthesizer.time.sleep")
@patch("news.synthesizer.subprocess.run")
def test_repeated_timeouts_stop_at_the_global_cap(mock_run, mock_sleep):
    """TimeoutExpired must use Outcome.TIMEOUT's own cap (2), not EMPTY's tighter cap (1).

    Before Task 3, _run_once returned bare None for all failure paths, so
    _classify(None, None, None) always returned EMPTY (cap=1 → 2 calls). With the
    triple return, TimeoutExpired carries its exception to _classify → TIMEOUT
    (cap=2 → 3 calls). The exact assertion == 3 proves TIMEOUT uses its own row cap:
    if the regression is reintroduced (all paths collapse to None), _classify returns
    EMPTY and call_count drops to 2, failing this test.
    """
    mock_run.side_effect = subprocess.TimeoutExpired("claude", 300)
    out = invoke_claude("prompt", timeout=300)
    assert out is None
    # TIMEOUT cap=2 → 3 subprocess calls (initial + 2 retries, then GIVE_UP).
    assert mock_run.call_count == 3


@patch("news.synthesizer.reauth")
@patch("news.synthesizer.time.sleep")
@patch("news.synthesizer.subprocess.run")
def test_an_auth_error_triggers_exactly_one_reauth_then_succeeds(mock_run, mock_sleep, mock_reauth):
    """AUTH_REAUTH_REQUIRED must trigger reauth() and retry the same tier."""
    mock_reauth.return_value = ReauthResult.SUCCEEDED
    mock_run.side_effect = [
        Mock(stdout=_envelope(result=_RAPT_ERR, is_error=True), returncode=0),
        Mock(stdout=_envelope(result="the synthesis"), returncode=0),
    ]
    assert invoke_claude("prompt", timeout=300) == "the synthesis"
    assert mock_reauth.call_count == 1


@patch("news.synthesizer.reauth")
@patch("news.synthesizer.time.sleep")
@patch("news.synthesizer.subprocess.run")
def test_a_second_auth_error_does_not_reauth_again(mock_run, mock_sleep, mock_reauth):
    """The one-shot re-auth latch must not reset between retries."""
    mock_reauth.return_value = ReauthResult.SUCCEEDED
    mock_run.return_value = Mock(stdout=_envelope(result=_RAPT_ERR, is_error=True), returncode=0)
    assert invoke_claude("prompt", timeout=300) is None
    assert mock_reauth.call_count == 1, "the one-shot re-auth budget must not reset"


@patch("news.synthesizer.subprocess.run")
def test_api_error_path_uses_its_own_cap(mock_run):
    """OSError maps to Outcome.API_ERROR (cap=2) → 3 calls, not EMPTY's cap=1 → 2.

    Before Task 3, _run_once returning bare None for all failure paths meant
    _classify(None, None, None) = EMPTY for other-exception too, giving only 2 calls.
    The triple return carries the OSError to _classify → API_ERROR (cap=2 → 3 calls).
    Reverting _run_once to return (None, None, None) drops call_count to 2, failing
    the == 3 assertion.
    """
    mock_run.side_effect = OSError("claude not found")
    out = invoke_claude("prompt", timeout=300)
    assert out is None
    # API_ERROR cap=2 → 3 subprocess calls (initial + 2 retries, then GIVE_UP).
    assert mock_run.call_count == 3


@patch("news.synthesizer.subprocess.run")
def test_empty_stdout_path_uses_its_own_cap(mock_run, caplog):
    """Empty stdout maps to Outcome.EMPTY (cap=1) → 2 calls.

    The caplog assertion on 'empty' discriminates this path from UNPARSEABLE, which
    shares the same call count (cap=1 → 2) but emits 'unparseable' in the give-up log.
    """
    import logging

    mock_run.return_value = Mock(stdout="", stderr="", returncode=0)
    with caplog.at_level(logging.ERROR, logger="news.synthesizer"):
        out = invoke_claude("prompt", timeout=300)
    assert out is None
    # EMPTY cap=1 → 2 subprocess calls (initial + 1 retry, then GIVE_UP).
    assert mock_run.call_count == 2
    assert any("empty" in msg for msg in caplog.messages)


@patch("news.synthesizer.subprocess.run")
def test_non_json_stdout_path_uses_its_own_cap(mock_run, caplog):
    """Non-JSON stdout maps to Outcome.UNPARSEABLE (cap=1) → 2 calls with 'unparseable' log.

    Before Task 3, non-JSON stdout collapsed to bare None → EMPTY → 2 calls but
    with 'empty' in the give-up log. The triple return carries raw to _classify →
    UNPARSEABLE. The caplog assertion on 'unparseable' distinguishes the path:
    reverting _run_once to (None, None, None) drops the outcome to EMPTY and emits
    'empty' instead, failing this assertion.
    """
    import logging

    mock_run.return_value = Mock(stdout="this is not json", returncode=0)
    with caplog.at_level(logging.ERROR, logger="news.synthesizer"):
        out = invoke_claude("prompt", timeout=300)
    assert out is None
    # UNPARSEABLE cap=1 → 2 subprocess calls (initial + 1 retry, then GIVE_UP).
    assert mock_run.call_count == 2
    assert any("unparseable" in msg for msg in caplog.messages)


@patch("news.synthesizer.subprocess.run")
def test_content_validate_failure_triggers_unparseable_retry(mock_run):
    """A result that passes _classify but fails the caller's validate callback is
    reclassified as UNPARSEABLE, giving it a retry under UNPARSEABLE's cap (1).

    This restores the content-level retry the old outer per-profile loops provided:
    when parse_synthesis_output returned a dict with 'error', the loop would continue
    and bill a second model call. With the loops gone, only the validate callback
    path provides that retry. Without it, a chatty preamble wrapping unparseable
    synthesis JSON returns immediately from invoke_claude, sending the caller straight
    to the fallback digest — the exact failure class behind the 2026-08-01 incident.
    """
    bad_result = '{"error": "could not parse synthesis"}'
    good_result = '{"executive_brief": [], "sections": []}'
    mock_run.side_effect = [
        Mock(stdout=_envelope(result=bad_result), returncode=0),
        Mock(stdout=_envelope(result=good_result), returncode=0),
    ]

    result = invoke_claude(
        "prompt",
        timeout=300,
        validate=lambda text: '"error"' not in text,
    )

    assert mock_run.call_count == 2
    assert result == good_result
