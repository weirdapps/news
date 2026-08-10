import json
import logging
from unittest.mock import MagicMock, Mock, patch

import pytest

from news.tagger import _reset_reauth_latch, extract_tickers_llm


@pytest.fixture(autouse=True)
def _reset_latch():
    """Reset the per-process reauth latch before every test."""
    _reset_reauth_latch()


def _mock_proc(stdout_text, returncode=0):
    proc = MagicMock()
    proc.stdout = stdout_text
    proc.stderr = ""
    proc.returncode = returncode
    return proc


@patch("news.tagger.subprocess.run")
def test_llm_returns_tickers(mock_run):
    mock_run.return_value = _mock_proc(
        json.dumps({"result": json.dumps({"tickers": ["AAPL", "MSFT"]})})
    )
    out = extract_tickers_llm("Apple and Microsoft beat estimates")
    assert out == ["AAPL", "MSFT"]
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "claude"
    assert "--model" in cmd
    assert "sonnet" in cmd
    assert "--output-format" in cmd
    assert "json" in cmd


@patch("news.tagger.subprocess.run")
def test_llm_returns_empty_when_no_tickers(mock_run):
    mock_run.return_value = _mock_proc(json.dumps({"result": json.dumps({"tickers": []})}))
    out = extract_tickers_llm("Weather report for Athens")
    assert out == []


@patch("news.tagger.subprocess.run")
def test_llm_handles_malformed_response(mock_run):
    mock_run.return_value = _mock_proc(json.dumps({"result": '{"oops": "no tickers key"}'}))
    out = extract_tickers_llm("Some article")
    assert out == []


@patch("news.tagger.subprocess.run")
def test_llm_handles_markdown_fenced_json(mock_run):
    mock_run.return_value = _mock_proc(
        json.dumps({"result": '```json\n{"tickers": ["AAPL"]}\n```'})
    )
    out = extract_tickers_llm("Apple news")
    assert out == ["AAPL"]


@patch("news.tagger.subprocess.run")
def test_llm_uppercases_and_dedups(mock_run):
    mock_run.return_value = _mock_proc(
        json.dumps({"result": json.dumps({"tickers": ["aapl", "AAPL", "msft"]})})
    )
    out = extract_tickers_llm("text")
    assert out == ["AAPL", "MSFT"]


@patch("news.tagger.subprocess.run")
def test_llm_returns_empty_on_subprocess_failure(mock_run):
    mock_run.side_effect = Exception("command not found")
    out = extract_tickers_llm("text")
    assert out == []


@patch("news.tagger.subprocess.run")
def test_llm_returns_empty_on_nonzero_exit(mock_run):
    mock_run.return_value = _mock_proc("error", returncode=1)
    out = extract_tickers_llm("text")
    assert out == []


@patch("news.tagger.subprocess.run")
def test_llm_passes_custom_model(mock_run):
    mock_run.return_value = _mock_proc(json.dumps({"result": json.dumps({"tickers": []})}))
    extract_tickers_llm("text", model="opus")
    cmd = mock_run.call_args[0][0]
    assert "opus" in cmd


@patch("news.tagger.running_on_linux", return_value=False)
@patch("news.tagger.refresh_auth", return_value=False)
@patch("news.tagger.subprocess.run")
def test_an_auth_failure_is_distinguishable_from_no_tickers(mock_run, mock_refresh, mock_linux):
    # Old behaviour: raised TaggerAuthError. New behaviour: attempts one re-auth,
    # logs at ERROR, and returns [] so the run continues. The re-auth attempt (not
    # silent absorption) is what distinguishes auth failure from a genuine empty result.
    mock_run.return_value = Mock(
        stdout='{"is_error": true, "result": "API Error: invalid_grant"}',
        returncode=0,
    )
    result = extract_tickers_llm("some article text")
    assert result == []
    mock_refresh.assert_called_once()


@patch("news.tagger.subprocess.run")
def test_a_genuine_empty_answer_still_returns_an_empty_list(mock_run):
    mock_run.return_value = Mock(stdout='{"result": "{\\"tickers\\": []}"}', returncode=0)
    assert extract_tickers_llm("some article text") == []
    cmd = mock_run.call_args[0][0]
    assert "--output-format" in cmd


@patch("news.tagger.subprocess.run")
def test_tickers_are_uppercased_deduped_and_sorted(mock_run):
    mock_run.return_value = Mock(
        stdout='{"result": "{\\"tickers\\": [\\"msft\\", \\"AAPL\\", \\"aapl\\"]}"}',
        returncode=0,
    )
    assert extract_tickers_llm("text") == ["AAPL", "MSFT"]


@patch("news.tagger.running_on_linux", return_value=False)
@patch("news.tagger.refresh_auth", return_value=True)
@patch("news.tagger.subprocess.run")
def test_reauth_success_on_macos_retries_and_returns_tickers(mock_run, mock_refresh, mock_linux):
    auth_envelope = '{"is_error": true, "result": "API Error: invalid_grant"}'
    success_envelope = json.dumps({"result": json.dumps({"tickers": ["AAPL"]})})
    mock_run.side_effect = [
        Mock(stdout=auth_envelope, returncode=0),
        Mock(stdout=success_envelope, returncode=0),
    ]
    assert extract_tickers_llm("Apple news") == ["AAPL"]
    mock_refresh.assert_called_once()


@patch("news.tagger.running_on_linux", return_value=True)
@patch("news.tagger.refresh_auth")
@patch("news.tagger.subprocess.run")
def test_auth_error_on_linux_skips_reauth_and_returns_empty(mock_run, mock_refresh, mock_linux):
    """On Linux, waiting for the Mac's token push can exceed TimeoutStartSec (600 s)
    and SIGKILL the service. The tagger must not call refresh_auth on this host."""
    mock_run.return_value = Mock(
        stdout='{"is_error": true, "result": "API Error: invalid_grant"}',
        returncode=0,
    )
    assert extract_tickers_llm("Apple news") == []
    mock_refresh.assert_not_called()


@patch("news.tagger.running_on_linux", return_value=False)
@patch("news.tagger.refresh_auth", return_value=False)
@patch("news.tagger.subprocess.run")
def test_reauth_attempted_at_most_once_per_process(mock_run, mock_refresh, mock_linux):
    auth_envelope = '{"is_error": true, "result": "API Error: invalid_grant"}'
    mock_run.return_value = Mock(stdout=auth_envelope, returncode=0)
    extract_tickers_llm("first article")
    extract_tickers_llm("second article")
    mock_refresh.assert_called_once()


@patch("news.tagger.running_on_linux", return_value=False)
@patch("news.tagger.refresh_auth", return_value=False)
@patch("news.tagger.subprocess.run")
def test_auth_give_up_logs_at_error_level(mock_run, mock_refresh, mock_linux, caplog):
    mock_run.return_value = Mock(
        stdout='{"is_error": true, "result": "API Error: invalid_grant"}',
        returncode=0,
    )
    with caplog.at_level(logging.ERROR, logger="news.tagger"):
        extract_tickers_llm("Apple news")
    assert any(r.levelno == logging.ERROR for r in caplog.records)
