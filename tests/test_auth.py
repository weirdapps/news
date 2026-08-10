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
@patch("news.auth.subprocess.run")
def test_check_gcloud_auth_refreshes_when_adc_stale(mock_run, mock_refresh):
    """A valid user token but a stale ADC must still trigger a refresh."""

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
