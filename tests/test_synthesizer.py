"""Tests for AI synthesis layer."""

import json
import subprocess
from datetime import datetime, timezone
from unittest.mock import Mock, patch


from news.models import Article
from news.synthesizer import (
    build_fallback_digest,
    build_prompt,
    invoke_claude,
    parse_synthesis_output,
)


def _make_articles():
    """Helper to create test articles."""
    now = datetime.now(timezone.utc)
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

    assert result["executive_brief"] == [
        "Item 1",
        "Item 2",
        "Item 3",
        "Item 4",
        "Item 5",
    ]
    assert result["what_changed"] == "Major changes occurred."
    assert len(result["sections"]) == 1
    assert result["sections"][0]["category"] == "banking"
    assert result["sections"][0]["display_name"] == "Banking"


def test_parse_synthesis_output_extracts_json_from_prose():
    """Parse JSON embedded in markdown code block."""
    data = {
        "executive_brief": ["Brief 1", "Brief 2", "Brief 3", "Brief 4", "Brief 5"],
        "what_changed": "Changes noted.",
        "sections": [],
    }
    raw = f"Here is the analysis:\n```json\n{json.dumps(data)}\n```\nEnd of response."

    result = parse_synthesis_output(raw)

    assert result["executive_brief"] == [
        "Brief 1",
        "Brief 2",
        "Brief 3",
        "Brief 4",
        "Brief 5",
    ]
    assert result["what_changed"] == "Changes noted."


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
    return json.dumps(
        {"result": result, "stop_reason": stop_reason, "is_error": is_error}
    )


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
def test_invoke_claude_resolves_sonnet_alias_to_vertex_light_model(
    mock_run, monkeypatch
):
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
def test_invoke_claude_opus_falls_back_to_correct_default_when_env_absent(
    mock_run, monkeypatch
):
    """With no central env present, opus must still resolve to the working id.

    Guards against ever emitting the broken bare 'opus' alias.
    """
    monkeypatch.delenv("VERTEX_MODEL_HEAVY", raising=False)
    monkeypatch.delenv("VERTEX_REGION_HEAVY", raising=False)
    mock_run.return_value = Mock(stdout=_envelope(), returncode=0)

    invoke_claude("p", claude_args=["--model", "opus"])

    cmd = mock_run.call_args[0][0]
    run_env = mock_run.call_args[1]["env"]
    assert "claude-opus-4-8[1m]" in cmd
    assert "opus" not in cmd
    assert run_env["CLOUD_ML_REGION"] == "eu"


@patch("news.synthesizer.subprocess.run")
def test_invoke_claude_downgrades_to_fallback_on_policy_refusal(mock_run, monkeypatch):
    """A spurious 'anthropic policy' refusal must auto-retry on the Opus 4.6 /
    europe-west1 fallback tier (model AND region together)."""
    monkeypatch.setenv("VERTEX_MODEL_HEAVY", "claude-opus-4-8[1m]")
    monkeypatch.setenv("VERTEX_REGION_HEAVY", "eu")
    monkeypatch.setenv("VERTEX_MODEL_FALLBACK", "claude-opus-4-6[1m]")
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
    assert "claude-opus-4-6[1m]" in second_cmd
    assert second_env["CLOUD_ML_REGION"] == "europe-west1"
    assert result == "SYNTH_OK"


@patch("news.synthesizer.subprocess.run")
def test_invoke_claude_downgrades_to_fallback_on_api_error(mock_run, monkeypatch):
    """An is_error envelope (e.g. a 429) on the primary must also retry on the fallback."""
    monkeypatch.setenv("VERTEX_MODEL_HEAVY", "claude-opus-4-8[1m]")
    monkeypatch.setenv("VERTEX_REGION_HEAVY", "eu")
    monkeypatch.setenv("VERTEX_MODEL_FALLBACK", "claude-opus-4-6[1m]")
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
    assert "claude-opus-4-6[1m]" in mock_run.call_args_list[1][0][0]
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


@patch("news.synthesizer.refresh_auth")
@patch("news.synthesizer.subprocess.run")
def test_invoke_claude_reauths_on_invalid_rapt_then_retries_same_tier(
    mock_run, mock_reauth, monkeypatch
):
    """An invalid_rapt auth error must trigger a gcloud re-auth and retry the
    SAME model/region — NOT a model downgrade (creds, not quota)."""
    monkeypatch.setenv("VERTEX_MODEL_HEAVY", "claude-opus-4-8[1m]")
    monkeypatch.setenv("VERTEX_REGION_HEAVY", "eu")
    monkeypatch.setenv("VERTEX_MODEL_FALLBACK", "claude-opus-4-6[1m]")
    monkeypatch.setenv("VERTEX_REGION_FALLBACK", "europe-west1")
    mock_run.side_effect = [
        Mock(stdout=_envelope(result=_RAPT_ERR, is_error=True), returncode=0),
        Mock(stdout=_envelope(result="SYNTH_OK"), returncode=0),
    ]
    mock_reauth.return_value = True

    result = invoke_claude("p", claude_args=["--print", "--model", "opus"])

    mock_reauth.assert_called_once()
    assert mock_run.call_count == 2
    second_cmd = mock_run.call_args_list[1][0][0]
    second_env = mock_run.call_args_list[1][1]["env"]
    assert "claude-opus-4-8[1m]" in second_cmd  # retried same tier
    assert "claude-opus-4-6[1m]" not in second_cmd  # NOT downgraded
    assert second_env["CLOUD_ML_REGION"] == "eu"
    assert result == "SYNTH_OK"


@patch("news.synthesizer.refresh_auth")
@patch("news.synthesizer.subprocess.run")
def test_invoke_claude_returns_none_when_reauth_fails(
    mock_run, mock_reauth, monkeypatch
):
    """If re-auth itself fails, synthesis must return None (so the pipeline can
    alert) — and must NOT waste a model downgrade on an auth failure."""
    monkeypatch.setenv("VERTEX_MODEL_HEAVY", "claude-opus-4-8[1m]")
    monkeypatch.setenv("VERTEX_REGION_HEAVY", "eu")
    mock_run.return_value = Mock(
        stdout=_envelope(result=_RAPT_ERR, is_error=True), returncode=0
    )
    mock_reauth.return_value = False

    result = invoke_claude("p", claude_args=["--print", "--model", "opus"])

    assert result is None
    mock_reauth.assert_called_once()
    assert mock_run.call_count == 1  # no retry, no downgrade


@patch("news.synthesizer.refresh_auth")
@patch("news.synthesizer.subprocess.run")
def test_invoke_claude_429_does_not_trigger_reauth(mock_run, mock_reauth, monkeypatch):
    """A 429/quota error is NOT auth-class: it must downgrade, never re-auth."""
    monkeypatch.setenv("VERTEX_MODEL_HEAVY", "claude-opus-4-8[1m]")
    monkeypatch.setenv("VERTEX_REGION_HEAVY", "eu")
    monkeypatch.setenv("VERTEX_MODEL_FALLBACK", "claude-opus-4-6[1m]")
    monkeypatch.setenv("VERTEX_REGION_FALLBACK", "europe-west1")
    mock_run.side_effect = [
        Mock(
            stdout=_envelope(result="API Error: 429 quota", is_error=True), returncode=0
        ),
        Mock(stdout=_envelope(result="RECOVERED"), returncode=0),
    ]

    result = invoke_claude("p", claude_args=["--print", "--model", "opus"])

    mock_reauth.assert_not_called()
    assert "claude-opus-4-6[1m]" in mock_run.call_args_list[1][0][0]  # downgraded
    assert result == "RECOVERED"
