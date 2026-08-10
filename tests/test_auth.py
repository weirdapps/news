"""Tests for news/auth.py — proactive pre-flight and reactive re-auth delegation."""

from unittest.mock import Mock, patch

from news.auth import check_gcloud_auth, refresh_auth
from news.llm_policy import ReauthResult


@patch("news.auth.refresh_auth")
@patch("news.auth.subprocess.run")
def test_check_gcloud_auth_also_probes_adc(mock_run, mock_refresh):
    """Pre-flight must verify BOTH the user token AND ADC, not just the user token.

    The 21:00 failure showed a valid user access token while the ADC reauth had
    lapsed — so a user-token-only check is a false green.
    """
    mock_run.return_value = Mock(returncode=0, stdout="ya29.token")

    assert check_gcloud_auth() is True
    mock_refresh.assert_not_called()
    cmds = [call.args[0] for call in mock_run.call_args_list]
    assert any("application-default" in cmd for cmd in cmds), (
        "ADC (application-default) was never probed"
    )


@patch("news.auth.refresh_auth")
@patch("news.auth.running_on_linux", return_value=False)
@patch("news.auth.subprocess.run")
def test_check_gcloud_auth_refreshes_when_adc_stale(mock_run, mock_linux, mock_refresh):
    """A valid user token but a stale ADC must still trigger a refresh.

    Pins the macOS path: on Linux the same credential failure hits the fast-fail
    branch and returns False without calling refresh_auth.
    """

    def _side(cmd, **_kwargs):
        is_adc = "application-default" in cmd
        return Mock(
            returncode=1 if is_adc else 0,
            stdout="" if is_adc else "ya29.token",
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
    mock_run.return_value = Mock(returncode=1, stdout="")
    mock_linux.return_value = True

    result = check_gcloud_auth()

    assert result is False
    mock_reauth.assert_not_called()


@patch("news.auth.refresh_auth")
@patch("news.auth.running_on_linux")
@patch("news.auth.subprocess.run")
def test_check_gcloud_auth_macos_attempts_refresh_when_expired(mock_run, mock_linux, mock_refresh):
    """On macOS, a failed pre-flight probe must attempt a refresh."""
    mock_run.return_value = Mock(returncode=1, stdout="")
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
    mock_run.return_value = Mock(returncode=1, stdout="")
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
    mock_run.return_value = Mock(returncode=1, stdout="")

    assert check_gcloud_auth(may_wait_for_push=False) is False
    mock_reauth.assert_not_called()


@patch("news.auth.shared_reauth")
@patch("news.auth.running_on_linux", return_value=True)
@patch("news.auth.subprocess.run")
def test_the_wait_is_off_by_default(mock_run, mock_linux, mock_reauth):
    """The parameter defaults to the old fast-fail, so no caller changes by accident."""
    mock_run.return_value = Mock(returncode=1, stdout="")

    assert check_gcloud_auth() is False
    mock_reauth.assert_not_called()


@patch("news.auth.shared_reauth")
@patch("news.auth.running_on_linux", return_value=True)
@patch("news.auth.subprocess.run")
def test_a_skipped_reauth_during_the_wait_is_not_a_green_pre_flight(
    mock_run, mock_linux, mock_reauth
):
    """SKIPPED means the remedy did nothing. Treating it as success ships a dead run."""
    mock_run.return_value = Mock(returncode=1, stdout="")
    mock_reauth.return_value = ReauthResult.SKIPPED

    assert check_gcloud_auth(may_wait_for_push=True) is False


@patch("news.auth.shared_reauth")
@patch("news.auth.running_on_linux", return_value=True)
@patch("news.auth.subprocess.run")
def test_a_failed_wait_is_a_failed_pre_flight(mock_run, mock_linux, mock_reauth):
    mock_run.return_value = Mock(returncode=1, stdout="")
    mock_reauth.return_value = ReauthResult.FAILED

    assert check_gcloud_auth(may_wait_for_push=True) is False


@patch("news.auth.refresh_auth")
@patch("news.auth.running_on_linux", return_value=False)
@patch("news.auth.subprocess.run")
def test_macos_still_refreshes_regardless_of_the_wait_permission(
    mock_run, mock_linux, mock_refresh
):
    """The permission gates the Linux branch only. macOS has a local remedy either way."""
    mock_run.return_value = Mock(returncode=1, stdout="")
    mock_refresh.return_value = True

    assert check_gcloud_auth(may_wait_for_push=False) is True
    mock_refresh.assert_called_once()
