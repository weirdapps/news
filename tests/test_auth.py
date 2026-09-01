"""Tests for news/auth.py — proactive pre-flight and reactive re-auth delegation."""

import logging
from unittest.mock import Mock, patch

import pytest

from news.auth import (
    _PROBE_TIMEOUT_SECONDS,
    PROBE_RETRY_WORST_CASE_SECONDS,
    check_gcloud_auth,
    refresh_auth,
)
from news.llm_policy import ReauthResult


@patch("news.auth.refresh_auth")
@patch("news.auth.subprocess.run")
def test_the_preflight_probes_adc_and_only_adc(mock_run, mock_refresh):
    """Pre-flight must verify ADC, the credential the model call actually uses.

    The 21:00 failure showed a valid user access token while the ADC reauth had
    lapsed, so a user-token check is a false green. The user-token probe that once
    ran alongside this one was dropped: it can only fail a run ADC would have carried,
    and it doubled the live token-endpoint exchanges a run had to win before starting.
    """
    mock_run.return_value = Mock(returncode=0, stdout="ya29.token", stderr="")

    assert check_gcloud_auth() is True
    mock_refresh.assert_not_called()
    cmds = [call.args[0] for call in mock_run.call_args_list]
    assert cmds, "no probe ran at all"
    assert all("application-default" in cmd for cmd in cmds), (
        f"the pre-flight probed something other than ADC: {cmds}"
    )


@patch("news.auth.refresh_auth")
@patch("news.auth.running_on_linux", return_value=False)
@patch("news.auth.subprocess.run")
def test_check_gcloud_auth_refreshes_when_adc_stale(mock_run, mock_linux, mock_refresh):
    """A stale ADC must trigger a refresh.

    Pins the macOS path: on Linux the same credential failure hits the fast-fail
    branch and returns False without calling refresh_auth. The side effect still keys
    on the ADC form so that re-adding a green user-token probe cannot make this pass.
    """

    def _side(cmd, **_kwargs):
        is_adc = "application-default" in cmd
        return Mock(
            returncode=1 if is_adc else 0,
            stdout="" if is_adc else "ya29.token",
            stderr="",
        )

    mock_run.side_effect = _side
    mock_refresh.return_value = True

    assert check_gcloud_auth() is True
    mock_refresh.assert_called_once()


@patch("news.auth.shared_reauth")
def test_refresh_auth_delegates_and_maps_succeeded_to_true(mock_reauth):
    mock_reauth.return_value = ReauthResult.SUCCEEDED
    assert refresh_auth() is True


@patch("news.auth.shared_reauth")
def test_a_skipped_reauth_is_not_a_success(mock_reauth):
    # SKIPPED means the script took a no-op path: lock held, cooldown, or already
    # authed. Reporting it as success burns the caller's one-shot budget on nothing.
    mock_reauth.return_value = ReauthResult.SKIPPED
    assert refresh_auth() is False


@patch("news.auth.shared_reauth")
def test_a_failed_reauth_is_false(mock_reauth):
    mock_reauth.return_value = ReauthResult.FAILED
    assert refresh_auth() is False


@patch("news.auth.shared_reauth")
@patch("news.auth.running_on_linux")
@patch("news.auth.subprocess.run")
def test_check_gcloud_auth_linux_fails_fast_when_expired(mock_run, mock_linux, mock_reauth):
    """On Linux, a failed pre-flight probe must return False immediately.

    The VPS cannot re-authenticate itself; waiting for a Mac token push takes
    up to 1020 seconds — longer than news-monitor's TimeoutStartSec=600. Failing
    fast lets the pipeline still send its per-slot alert instead of being SIGKILLed
    mid-wait.
    """
    mock_run.return_value = Mock(returncode=1, stdout="", stderr="")
    mock_linux.return_value = True

    result = check_gcloud_auth()

    assert result is False
    mock_reauth.assert_not_called()


@patch("news.auth.refresh_auth")
@patch("news.auth.running_on_linux")
@patch("news.auth.subprocess.run")
def test_check_gcloud_auth_macos_attempts_refresh_when_expired(mock_run, mock_linux, mock_refresh):
    """On macOS, a failed pre-flight probe must attempt a refresh."""
    mock_run.return_value = Mock(returncode=1, stdout="", stderr="")
    mock_linux.return_value = False
    mock_refresh.return_value = True

    result = check_gcloud_auth()

    assert result is True
    mock_refresh.assert_called_once()


# --- F6: the fast-fail is budget-derived, not unconditional ----------------------
#
# The Linux fast-fail was right for the three 600s units and wrong for news-digest.
# At 2400s the digest is squarely in the group spec §6 says qualifies for a token-push
# wait, and its next slot is four hours away, so a RAPT lapse at 08:55 used to cost the
# whole morning. The permission is derived from the run's remaining budget — the same
# test decide() applies before WAIT_FOR_PUSH — never from the profile's name.


@patch("news.auth.shared_reauth")
@patch("news.auth.running_on_linux", return_value=True)
@patch("news.auth.subprocess.run")
def test_a_budget_that_funds_the_wait_reaches_the_shared_reauth(mock_run, mock_linux, mock_reauth):
    """With budget, Linux delegates to reauth(), which owns the waiting."""
    mock_run.return_value = Mock(returncode=1, stdout="", stderr="")
    mock_reauth.return_value = ReauthResult.SUCCEEDED

    assert check_gcloud_auth(may_wait_for_push=True) is True
    mock_reauth.assert_called_once()


@patch("news.auth.shared_reauth")
@patch("news.auth.running_on_linux", return_value=True)
@patch("news.auth.subprocess.run")
def test_a_budget_that_cannot_fund_the_wait_never_calls_reauth(mock_run, mock_linux, mock_reauth):
    """Task 5's anti-SIGKILL property, which must survive F6.

    A 600s unit cannot afford a 1020s wait, so the pre-flight must return without
    ever reaching reauth() — otherwise the unit is SIGKILLed mid-wait and its
    per-slot alert email never goes out.
    """
    mock_run.return_value = Mock(returncode=1, stdout="", stderr="")

    assert check_gcloud_auth(may_wait_for_push=False) is False
    mock_reauth.assert_not_called()


@patch("news.auth.shared_reauth")
@patch("news.auth.running_on_linux", return_value=True)
@patch("news.auth.subprocess.run")
def test_the_wait_is_off_by_default(mock_run, mock_linux, mock_reauth):
    """The parameter defaults to the old fast-fail, so no caller changes by accident."""
    mock_run.return_value = Mock(returncode=1, stdout="", stderr="")

    assert check_gcloud_auth() is False
    mock_reauth.assert_not_called()


@patch("news.auth.shared_reauth")
@patch("news.auth.running_on_linux", return_value=True)
@patch("news.auth.subprocess.run")
def test_a_skipped_reauth_during_the_wait_is_not_a_green_pre_flight(
    mock_run, mock_linux, mock_reauth
):
    """SKIPPED means the remedy did nothing. Treating it as success ships a dead run."""
    mock_run.return_value = Mock(returncode=1, stdout="", stderr="")
    mock_reauth.return_value = ReauthResult.SKIPPED

    assert check_gcloud_auth(may_wait_for_push=True) is False


@patch("news.auth.shared_reauth")
@patch("news.auth.running_on_linux", return_value=True)
@patch("news.auth.subprocess.run")
def test_a_failed_wait_is_a_failed_pre_flight(mock_run, mock_linux, mock_reauth):
    mock_run.return_value = Mock(returncode=1, stdout="", stderr="")
    mock_reauth.return_value = ReauthResult.FAILED

    assert check_gcloud_auth(may_wait_for_push=True) is False


@patch("news.auth.refresh_auth")
@patch("news.auth.running_on_linux", return_value=False)
@patch("news.auth.subprocess.run")
def test_macos_still_refreshes_regardless_of_the_wait_permission(
    mock_run, mock_linux, mock_refresh
):
    """The permission gates the Linux branch only. macOS has a local remedy either way."""
    mock_run.return_value = Mock(returncode=1, stdout="", stderr="")
    mock_refresh.return_value = True

    assert check_gcloud_auth(may_wait_for_push=False) is True
    mock_refresh.assert_called_once()


# --- One sample of a remote endpoint is not a verdict -----------------------------
#
# 2026-08-31 19:36: news-market degraded with no digest. The credential was fine; the
# token endpoint returned a transient "reauth is required" and the pre-flight, having
# taken exactly one sample, surrendered 17 seconds into a run with ~580 to spare. The
# incident was also unreadable, because one sentence covered every way the probe could
# fail and the short-circuited second probe left no second fact to log.


@patch("news.auth.time.sleep")
@patch("news.auth.refresh_auth")
@patch("news.auth.running_on_linux", return_value=True)
@patch("news.auth.subprocess.run")
def test_a_transient_refusal_is_re_probed_and_never_reaches_the_remedy(
    mock_run, mock_linux, mock_refresh, mock_sleep
):
    """The incident, replayed: one refusal then success, on the branch that surrendered.

    Linux without wait permission is the fast-fail branch, so a True here can only come
    from the re-probe. refresh_auth staying uncalled is the other half: the run must not
    spend its one-shot remedy on a credential that was never broken.
    """
    mock_run.side_effect = [
        Mock(returncode=1, stdout="", stderr="ReauthRequiredError: reauth is required"),
        Mock(returncode=0, stdout="ya29.token", stderr=""),
    ]

    assert check_gcloud_auth(probe_retry_seconds=30.0) is True
    assert mock_run.call_count == 2
    mock_refresh.assert_not_called()
    mock_sleep.assert_called_once_with(2.0)


@patch("news.auth.shared_reauth")
@patch("news.auth.running_on_linux", return_value=True)
@patch("news.auth.subprocess.run")
def test_re_probing_is_off_by_default(mock_run, mock_linux, mock_reauth):
    """Zero budget means one sample, exactly as before, so no caller changes by omission."""
    mock_run.return_value = Mock(returncode=1, stdout="", stderr="")

    assert check_gcloud_auth() is False
    assert mock_run.call_count == 1


@patch("news.auth.shared_reauth")
@patch("news.auth.running_on_linux", return_value=True)
@patch("news.auth.subprocess.run")
def test_the_failure_log_names_the_credential_and_the_underlying_reason(
    mock_run, mock_linux, mock_reauth, caplog
):
    """Without the reason, "reauth required" and "gcloud not found" read identically."""
    mock_run.return_value = Mock(
        returncode=1, stdout="", stderr="ReauthRequiredError: reauth is required"
    )

    with caplog.at_level(logging.WARNING, logger="news.auth"):
        assert check_gcloud_auth() is False

    messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert messages, "the fast-fail logged nothing"
    assert any("ADC" in m for m in messages), f"no message names the credential: {messages}"
    assert any("rc=1" in m and "reauth is required" in m for m in messages), (
        f"no message carries the underlying reason: {messages}"
    )


@patch("news.auth.shared_reauth")
@patch("news.auth.running_on_linux", return_value=True)
@patch("news.auth.subprocess.run")
def test_an_account_in_the_probe_stderr_never_reaches_the_log(
    mock_run, mock_linux, mock_reauth, caplog
):
    """This repo is public and gcloud names the account in its reauth errors."""
    mock_run.return_value = Mock(
        returncode=1,
        stdout="",
        stderr="Reauthentication required for account someone@example.org",
    )

    with caplog.at_level(logging.WARNING, logger="news.auth"):
        check_gcloud_auth()

    messages = [r.message for r in caplog.records]
    assert not any("@" in m for m in messages), f"an address reached the log: {messages}"
    assert any("<redacted>" in m for m in messages), f"nothing was redacted: {messages}"


# --- What the re-probe may cost, and what it must never cost ----------------------


class _ProbeClock:
    """Stands in for news.auth's `time` module, plus the subprocess the probe spawns.

    Advances only when a probe runs or a sleep is taken, and every probe burns the full
    _PROBE_TIMEOUT_SECONDS: a network outage is both a plausible cause of the refusal
    and the case that makes probe time dominate sleep time. Replaces the module
    attribute rather than patching time.monotonic globally, so pytest's own timing is
    untouched.
    """

    def __init__(self, response):
        self.now = 0.0
        self.probes = 0
        self.slept = []
        self._response = response

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def run(self, *_args, **_kwargs):
        self.probes += 1
        self.now += _PROBE_TIMEOUT_SECONDS
        return self._response


@pytest.mark.parametrize(
    ("budget", "probes", "wall_spent"),
    [(0.0, 1, 15.0), (6.0, 1, 15.0), (110.0, 4, 77.0), (210.0, 5, 112.0)],
)
@patch("news.auth.shared_reauth")
@patch("news.auth.running_on_linux", return_value=True)
def test_the_budget_bounds_wall_time_and_not_merely_the_sleeping(
    mock_linux, mock_reauth, budget, probes, wall_spent, monkeypatch
):
    """The bound must bind. Counting sleep alone, it did not.

    Measured before this test existed, with the same 15s-timeout probe: 6s bought 32s,
    and 110s and 210s both bought 112s, because each probe burned budget it was never
    charged for. 110s buying strictly less than 210s is the whole property.

    6s is also the guard's own test: without the forward-looking break the schedule
    runs to its end regardless of the budget, so this case reads 5 probes and 112s.
    """
    clock = _ProbeClock(Mock(returncode=1, stdout="", stderr="reauth is required"))
    monkeypatch.setattr("news.auth.time", clock)
    monkeypatch.setattr("news.auth.subprocess.run", clock.run)

    assert check_gcloud_auth(probe_retry_seconds=budget) is False

    assert (clock.probes, clock.now) == (probes, wall_spent)
    assert clock.now <= max(budget, _PROBE_TIMEOUT_SECONDS), (
        "overrun must be bounded by the one mandatory first probe"
    )


def test_the_advertised_worst_case_is_the_one_the_schedule_can_actually_reach():
    """112s, not the 37s of sleeping. main.py reserves this figure, so it must be real.

    Pinned against the sum rather than the literal so that editing the backoff schedule
    moves the constant, and pinned to 112 so that editing it silently cannot.
    """
    assert PROBE_RETRY_WORST_CASE_SECONDS == 112.0


@patch("news.auth.shared_reauth")
@patch("news.auth.running_on_linux", return_value=True)
@patch("news.auth.time.sleep")
@patch("news.auth.subprocess.run", side_effect=FileNotFoundError)
def test_a_gcloud_that_is_not_on_path_is_never_re_probed(
    mock_run, mock_sleep, mock_linux, mock_reauth
):
    """Waiting does not put a binary on PATH. Measured before this test: 5 attempts, 37s.

    The budget exists for a remote endpoint that might answer differently next time. A
    deterministic local failure spends it printing the same sentence four more times,
    and on the 600s units those are seconds the alert email needs.
    """
    assert check_gcloud_auth(probe_retry_seconds=210.0) is False

    assert mock_run.call_count == 1
    mock_sleep.assert_not_called()


@patch("news.auth.shared_reauth")
@patch("news.auth.running_on_linux", return_value=True)
@patch("news.auth.subprocess.run")
def test_an_undecodable_byte_from_the_child_is_a_red_pre_flight_not_a_crash(
    mock_run, mock_linux, mock_reauth, caplog
):
    """A pre-flight probe must never be able to fell its caller.

    ``text=True`` decodes the child's output, so a single non-UTF-8 byte on stderr
    raises UnicodeDecodeError, which is a ValueError and therefore in neither OSError
    nor subprocess.SubprocessError. Propagating it unwinds check_gcloud_auth and
    _preflight_auth_ok and kills the run, so the per-slot alert this whole path exists
    to protect never goes out: strictly worse than the failure being reported.
    """
    mock_run.side_effect = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    with caplog.at_level(logging.WARNING, logger="news.auth"):
        assert check_gcloud_auth() is False

    messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("UnicodeDecodeError" in m for m in messages), (
        f"the log must still name what went wrong: {messages}"
    )
