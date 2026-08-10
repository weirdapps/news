import json
import logging
import subprocess
from unittest.mock import MagicMock, Mock, patch

from news.tagger import extract_tickers_llm

# The reauth latch is reset by the suite-wide _reset_tagger_reauth_latch fixture in
# conftest.py. It lives there, not here, because the latch is a module global and a
# file-scoped fixture leaves it dirty for tests in any other file.


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
def test_an_auth_failure_triggers_one_reauth_then_returns_empty(mock_run, mock_refresh, mock_linux):
    # Old behaviour: raised TaggerAuthError. New behaviour: attempts one re-auth,
    # logs at ERROR, and returns [] so the run continues. Both an auth failure and a
    # genuine empty answer return [], so the re-auth call — not the return value — is
    # the observable that tells them apart. The name says so.
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


# --- Every give-up path must leave a record -----------------------------------
#
# Task 6 exists to end "the tagger returns [] and you cannot tell why". Its auth
# branch does that for auth errors, but spec §15 measured the CLI taking 200.6 s to
# report invalid_grant, because it retries the credential refresh internally. At the
# 30 s default the subprocess raises TimeoutExpired first, _invoke_once swallowed it
# to None, _is_auth_error(None) was False, and the function returned [] with ZERO log
# records — byte-for-byte the pre-branch behaviour. The timeout, not the auth
# envelope, is the observable shape of a credential outage on this host, so it is the
# one that has to be loud.


@patch("news.tagger.refresh_auth")
@patch("news.tagger.subprocess.run")
def test_a_cli_timeout_is_logged_at_error_and_does_not_look_like_an_auth_error(
    mock_run, mock_refresh, caplog
):
    """A TimeoutExpired must be a distinct, ERROR-level signal — and only a signal.

    It must not be inferred to be an auth error: a slow model produces the same
    exception, and guessing would burn the one-shot re-auth budget on a hunch.
    """
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=30)

    with caplog.at_level(logging.ERROR, logger="news.tagger"):
        result = extract_tickers_llm("some article text")

    assert result == []
    assert any("timed out" in r.message and r.levelno == logging.ERROR for r in caplog.records)
    # The timeout must name its own duration: at 30 s this is the ONLY evidence an
    # operator gets that a 200.6 s auth failure is in progress.
    assert any("30" in r.message for r in caplog.records)
    mock_refresh.assert_not_called()


@patch("news.tagger.subprocess.run")
def test_a_nonzero_exit_is_logged_at_error(mock_run, caplog):
    """Non-auth failures used to give up silently. Closes review finding M3."""
    mock_run.return_value = _mock_proc("boom", returncode=1)

    with caplog.at_level(logging.ERROR, logger="news.tagger"):
        assert extract_tickers_llm("text") == []

    assert any(r.levelno == logging.ERROR for r in caplog.records)


@patch("news.tagger.subprocess.run")
def test_unlaunchable_cli_is_logged_at_error(mock_run, caplog):
    mock_run.side_effect = OSError("command not found")

    with caplog.at_level(logging.ERROR, logger="news.tagger"):
        assert extract_tickers_llm("text") == []

    assert any(r.levelno == logging.ERROR for r in caplog.records)


@patch("news.tagger.subprocess.run")
def test_non_json_stdout_is_logged_at_error(mock_run, caplog):
    mock_run.return_value = _mock_proc("I'm afraid I can't do that")

    with caplog.at_level(logging.ERROR, logger="news.tagger"):
        assert extract_tickers_llm("text") == []

    assert any(r.levelno == logging.ERROR for r in caplog.records)


@patch("news.tagger.subprocess.run")
def test_the_default_timeout_is_the_documented_thirty_seconds(mock_run):
    """Pins the ruling, so a future raise is a deliberate edit and not a drift.

    30 s is kept on purpose. Raising it past spec §15's measured 200.6 s would make
    the auth branch reachable, but all five news pipelines run only on the VPS, where
    that branch's entire effect is one log line — running_on_linux() fast-fails
    without re-authing. The cost is paid per article: 20 LLM-tagged articles in one
    run is ordinary (measured 10-81/day across runs), so an auth outage would go from
    ~600 s of timeouts to ~4000 s of them, guaranteeing SIGTERM before the alert
    email on every profile including the 2400 s digest. The distinct ERROR log above
    delivers Task 6's "stop degrading silently" mandate at 1/7th the wall clock.
    """
    mock_run.return_value = _mock_proc(json.dumps({"result": json.dumps({"tickers": []})}))
    extract_tickers_llm("text")
    assert mock_run.call_args[1]["timeout"] == 30
