"""Regression guard for the gcloud auto-login script path (news/auth.py).

When the marketplace repos were renamed (trading-marketplace -> plessas-trading),
this path went stale. The auto-refresh then couldn't find the script, so an
expired token silently skipped LLM synthesis and shipped a fallback (unprocessed)
digest instead of failing loudly. Pin the path so a future rename trips a test
rather than degrading mail quality in production.
"""

from unittest.mock import Mock, patch

from news.auth import _AUTO_LOGIN_SCRIPT, check_gcloud_auth


def test_auto_login_script_path_is_current():
    path = str(_AUTO_LOGIN_SCRIPT)
    assert "trading-marketplace" not in path, "stale pre-rename repo path"
    assert path.endswith("plessas-trading/scripts/gcloud-auto-login.sh")


@patch("news.auth._refresh_auth")
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


@patch("news.auth._refresh_auth")
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
