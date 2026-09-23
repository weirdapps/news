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
def test_llm_returns_tickers(mock_run, monkeypatch):
    """The call is --bare, with the tier alias resolved to an exact id and region.

    Without --bare each per-article call cold-started a full Claude Code with the
    host's plugins, hooks and MCP servers. Without the resolution, the bare "sonnet"
    alias meant whatever the host's alias mapping said, in the parent's region.
    """
    monkeypatch.setenv("VERTEX_MODEL_LIGHT", "claude-sonnet-4-6")
    monkeypatch.setenv("VERTEX_REGION_LIGHT", "europe-west1")
    mock_run.return_value = _mock_proc(
        json.dumps({"result": json.dumps({"tickers": ["AAPL", "MSFT"]})})
    )
    out = extract_tickers_llm("Apple and Microsoft beat estimates")
    assert out == ["AAPL", "MSFT"]
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "claude"
    assert "--bare" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-6"
    assert "sonnet" not in cmd  # the bare alias must not survive
    assert "--output-format" in cmd
    assert "json" in cmd
    assert mock_run.call_args[1]["env"]["CLOUD_ML_REGION"] == "europe-west1"


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
def test_llm_passes_custom_model(mock_run, monkeypatch):
    monkeypatch.setenv("VERTEX_MODEL_HEAVY", "claude-opus-5-5[1m]")
    monkeypatch.setenv("VERTEX_REGION_HEAVY", "eu")
    mock_run.return_value = _mock_proc(json.dumps({"result": json.dumps({"tickers": []})}))
    extract_tickers_llm("text", model="opus")
    cmd = mock_run.call_args[0][0]
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5-5[1m]"
    assert mock_run.call_args[1]["env"]["CLOUD_ML_REGION"] == "eu"


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


# --- A non-zero exit must say why -----------------------------------------------
#
# With --output-format json a Vertex error arrives as an is_error envelope on STDOUT
# with stderr empty. The non-zero branch logged stderr alone, so digest.err read
# "stderr: (none)" for every such failure, and a credential envelope never reached
# the auth branch.

_QUOTA_ENVELOPE = json.dumps(
    {"type": "result", "is_error": True, "result": "API Error: 429 RESOURCE_EXHAUSTED"}
)
_AUTH_ENVELOPE = json.dumps(
    {"type": "result", "is_error": True, "result": "API Error: invalid_grant"}
)


@patch("news.tagger.subprocess.run")
def test_a_nonzero_exit_logs_the_vertex_error_from_stdout(mock_run, caplog):
    mock_run.return_value = _mock_proc(_QUOTA_ENVELOPE, returncode=1)

    with caplog.at_level(logging.ERROR, logger="news.tagger"):
        assert extract_tickers_llm("text") == []

    assert any("429" in r.message and r.levelno == logging.ERROR for r in caplog.records)


@patch("news.tagger.running_on_linux", return_value=True)
@patch("news.tagger.refresh_auth")
@patch("news.tagger.subprocess.run")
def test_a_credential_envelope_on_a_nonzero_exit_reaches_the_auth_branch(
    mock_run, mock_refresh, mock_linux, caplog
):
    mock_run.return_value = _mock_proc(_AUTH_ENVELOPE, returncode=1)

    with caplog.at_level(logging.ERROR, logger="news.tagger"):
        assert extract_tickers_llm("text") == []

    assert any("credential error on Linux" in r.message for r in caplog.records)
    mock_refresh.assert_not_called()


@patch("news.tagger.subprocess.run")
def test_nonzero_exits_carrying_a_non_auth_envelope_still_trip_the_shutoff(mock_run):
    """Parsing the envelope must not turn a failure into a normal response.

    Mutation-resistance: if _invoke_once returned every parsed envelope, a quota
    error would reach the success branch, reset the counter, and never trip.
    """
    from news.tagger import _LLM_SHUTOFF_THRESHOLD

    mock_run.return_value = _mock_proc(_QUOTA_ENVELOPE, returncode=1)
    for i in range(_LLM_SHUTOFF_THRESHOLD):
        extract_tickers_llm(f"article {i}")

    calls = mock_run.call_count
    extract_tickers_llm("article after shutoff")
    assert mock_run.call_count == calls


@patch("news.tagger.running_on_linux", return_value=True)
@patch("news.tagger.refresh_auth")
@patch("news.tagger.subprocess.run")
def test_credential_errors_on_linux_trip_the_shutoff(mock_run, mock_refresh, mock_linux):
    """A credential envelope on a non-zero exit used to count as a plain failure.

    Routing it to the auth branch instead must not stop it tripping the breaker.
    Mutation-resistance: drop the count from the Linux give-up and every article
    keeps paying for a CLI call.
    """
    from news.tagger import _LLM_SHUTOFF_THRESHOLD

    mock_run.return_value = _mock_proc(_AUTH_ENVELOPE, returncode=1)
    for i in range(_LLM_SHUTOFF_THRESHOLD):
        extract_tickers_llm(f"article {i}")

    calls = mock_run.call_count
    extract_tickers_llm("article after shutoff")
    assert mock_run.call_count == calls


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


# --- FIX 3: auth-outage circuit breaker -------------------------------------------
#
# During an auth outage, every LLM-tagged article costs a full 30 s timeout because
# the CLI takes ~200 s to surface invalid_grant and times out first. With ~20 such
# articles per run on news-monitor/market (600 s budget, and news-stack's too until it
# went to 1800 s on 2026-09-15), the unit SIGKILL s
# before synthesis or its alert email. After N=3 consecutive failures the shutoff
# engages, remaining articles pass through with rules-based tags only, and the unit
# reaches synthesis as intended.
#
# N=3 chosen because: 3 x 30 s = 90 s burned before cutoff, well inside any unit's
# budget; three consecutive failures are unlikely to be random (low false-positive);
# matches the pattern of the existing one-shot reauth latch.


@patch("news.tagger.subprocess.run")
def test_n_consecutive_failures_trigger_shutoff(mock_run):
    """After exactly N consecutive CLI failures the shutoff activates.

    Mutation-resistance: if the shutoff threshold were removed (never activates),
    mock_run would still be called on the 4th invocation, and call_count would be
    >= 4 rather than exactly 3, failing the equality check.
    """
    from news.tagger import _LLM_SHUTOFF_THRESHOLD

    mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=30)

    for _ in range(_LLM_SHUTOFF_THRESHOLD):
        extract_tickers_llm(f"article {_}")

    # Shutoff is now active. A further call must NOT hit subprocess.
    extra_call_count = mock_run.call_count
    extract_tickers_llm("article after shutoff")
    assert mock_run.call_count == extra_call_count, (
        "subprocess.run was called after the shutoff engaged — "
        "the circuit breaker did not prevent the call"
    )


@patch("news.tagger.subprocess.run")
def test_success_resets_consecutive_failure_counter(mock_run):
    """A successful CLI call after N-1 failures resets the counter; shutoff does not trip.

    Mutation-resistance: if the counter is never reset, N-1 failures followed by a
    success followed by one more failure would total N, tripping the shutoff. With a
    proper reset the counter after the success is 1, so the shutoff stays off and the
    next article reaches subprocess. The call_count would then differ.
    """
    from news.tagger import _LLM_SHUTOFF_THRESHOLD

    timeout_resp = subprocess.TimeoutExpired(cmd="claude", timeout=30)
    success_resp = _mock_proc(json.dumps({"result": json.dumps({"tickers": []})}))

    # N-1 failures, then a success, then one more failure
    calls = [timeout_resp] * (_LLM_SHUTOFF_THRESHOLD - 1) + [success_resp, timeout_resp]
    mock_run.side_effect = calls

    for i in range(_LLM_SHUTOFF_THRESHOLD - 1):
        extract_tickers_llm(f"fail {i}")
    extract_tickers_llm("success resets the counter")
    extract_tickers_llm("one failure after reset")

    # Shutoff must NOT be active: subsequent call still hits subprocess.
    pre = mock_run.call_count
    mock_run.side_effect = [timeout_resp]
    extract_tickers_llm("another failure")
    assert mock_run.call_count == pre + 1


@patch("news.tagger.subprocess.run")
def test_shutoff_logs_at_error_when_tripped(mock_run, caplog):
    """When the shutoff trips it must log at ERROR naming the failure count.

    Mutation-resistance: removing the shutoff ERROR log makes this test fail on the
    'not any(ERROR)' check; removing only the failure count from the message makes
    it fail on the count assertion.
    """
    from news.tagger import _LLM_SHUTOFF_THRESHOLD

    mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=30)

    with caplog.at_level(logging.ERROR, logger="news.tagger"):
        for _ in range(_LLM_SHUTOFF_THRESHOLD):
            extract_tickers_llm(f"article {_}")

    shutoff_records = [
        r for r in caplog.records if r.levelno == logging.ERROR and "disabling" in r.message.lower()
    ]
    assert shutoff_records, "expected an ERROR log when the shutoff trips"
    # The message must name the count so an operator knows how many articles were affected.
    assert str(_LLM_SHUTOFF_THRESHOLD) in shutoff_records[-1].message


@patch("news.tagger.subprocess.run")
def test_shutoff_active_skips_subprocess_entirely(mock_run):
    """Once the shutoff is active, subprocess.run is never called.

    Mutation-resistance: if the early-return guard were removed, subprocess.run would
    be called on every post-shutoff article, incrementing call_count.
    """
    from news.tagger import _LLM_SHUTOFF_THRESHOLD

    mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=30)
    for _ in range(_LLM_SHUTOFF_THRESHOLD):
        extract_tickers_llm(f"fail {_}")

    call_count_at_shutoff = mock_run.call_count
    mock_run.side_effect = None  # would succeed if called
    mock_run.return_value = _mock_proc(json.dumps({"result": json.dumps({"tickers": ["AAPL"]})}))

    for _ in range(5):
        result = extract_tickers_llm("post-shutoff article")
        assert result == []  # shutoff returns empty, not the mocked tickers

    assert mock_run.call_count == call_count_at_shutoff, "subprocess called after shutoff"


@patch("news.tagger.running_on_linux", return_value=False)
@patch("news.tagger.refresh_auth", return_value=True)
@patch("news.tagger.subprocess.run")
def test_successful_reauth_resets_consecutive_failure_counter(mock_run, mock_refresh, mock_linux):
    """A successful re-auth must reset the consecutive-failure counter to zero.

    Without the reset, N-1 timeout failures followed by an auth error + successful
    re-auth leave the counter at N-1. One further timeout then reaches N, tripping
    the shutoff. With the reset the counter is 0 after re-auth, so the next failure
    only brings it to 1 and the shutoff stays off.

    Mutation-resistance: removing ``_llm_consecutive_failures = 0`` on the re-auth
    success path leaves the counter at N-1 after re-auth. The next timeout reaches N
    and trips the shutoff. The final assertion then fails because a subsequent call
    does NOT reach subprocess.run (the shutoff returns [] immediately).
    """
    import json
    from unittest.mock import Mock

    from news.tagger import _LLM_SHUTOFF_THRESHOLD

    auth_envelope = json.dumps({"is_error": True, "result": "API Error: invalid_grant"})
    success_after_reauth = _mock_proc(json.dumps({"result": json.dumps({"tickers": []})}))
    timeout_resp = subprocess.TimeoutExpired(cmd="claude", timeout=30)

    # Build the call sequence:
    #   N-1 timeouts → counter = N-1
    #   auth error → re-auth → second CLI call succeeds → counter resets to 0
    #   one more timeout → counter = 1 (shutoff must NOT trip)
    calls = (
        [timeout_resp] * (_LLM_SHUTOFF_THRESHOLD - 1)
        + [Mock(stdout=auth_envelope, returncode=0), success_after_reauth]
        + [timeout_resp]
    )
    mock_run.side_effect = calls

    for i in range(_LLM_SHUTOFF_THRESHOLD - 1):
        extract_tickers_llm(f"timeout fail {i}")
    extract_tickers_llm("auth error triggers reauth")
    extract_tickers_llm("one timeout after reauth — counter should be 1, not N")

    # Shutoff must NOT be active — another call must still reach subprocess.run.
    pre = mock_run.call_count
    mock_run.side_effect = [timeout_resp]
    extract_tickers_llm("check shutoff is not active")
    assert mock_run.call_count == pre + 1, (
        "subprocess.run was NOT called — shutoff is active after re-auth success, "
        "which means the consecutive-failure counter was not reset on that path."
    )


def test_threshold_times_timeout_leaves_room_for_synthesis_on_smallest_unit():
    """Pins the ARITHMETIC BOUND that motivated N=3, not the constant itself.

    The four tests above loop with _LLM_SHUTOFF_THRESHOLD, making them structurally
    blind to the value: setting it to 9999 leaves them green while the breaker becomes
    useless (9999 x 30s = 8.3 h on a 600s unit). This test catches that by asserting
    the consequence directly.

    Bound derived from the deadline arithmetic in main.py:
      available = smallest_unit - synthesis_timeout - shutdown_grace
    Worst-case tagger burn must not exceed that available window:
      _LLM_SHUTOFF_THRESHOLD * tagger_default_timeout <= available

    Current values: 3 x 30 = 90s <= (600 - 150 - 90) = 360s.  Pass.
    Mutant N=9999:  9999 x 30 = 299,970s >> 360s.  Fail.
    Mutant timeout=300s:  3 x 300 = 900s >> 360s.  Fail.
    """
    import inspect

    from main import _SHUTDOWN_GRACE_SECONDS, _UNIT_TIMEOUT_SECONDS
    from news.config import get_settings
    from news.tagger import _LLM_SHUTOFF_THRESHOLD, extract_tickers_llm

    # Read the tagger's own default from its signature — not hardcoded here.
    tagger_timeout = inspect.signature(extract_tickers_llm).parameters["timeout"].default

    # Smallest unit is the binding constraint (monitor/market at 600s; stack left the
    # group on 2026-09-15 and no longer sets it).
    smallest_unit = min(_UNIT_TIMEOUT_SECONDS.values())
    smallest_profiles = [p for p, v in _UNIT_TIMEOUT_SECONDS.items() if v == smallest_unit]
    # Pessimistic: largest synthesis timeout among those profiles.
    synthesis_timeout = max(
        get_settings(profile=p).get("synthesis", {}).get("timeout", 300) for p in smallest_profiles
    )

    available = smallest_unit - synthesis_timeout - _SHUTDOWN_GRACE_SECONDS
    worst_case_burn = _LLM_SHUTOFF_THRESHOLD * tagger_timeout

    assert worst_case_burn <= available, (
        f"Breaker too permissive: {_LLM_SHUTOFF_THRESHOLD} failures x {tagger_timeout}s"
        f" = {worst_case_burn}s, but the smallest unit ({smallest_unit}s) leaves only"
        f" {available}s before synthesis ({synthesis_timeout}s) + grace"
        f" ({_SHUTDOWN_GRACE_SECONDS}s). Lower _LLM_SHUTOFF_THRESHOLD or"
        f" synthesis.timeout, or raise the unit's TimeoutStartSec."
    )
