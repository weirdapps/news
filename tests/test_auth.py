"""Regression guard for the gcloud auto-login script path (news/auth.py).

When the marketplace repos were renamed (trading-marketplace -> plessas-trading),
this path went stale. The auto-refresh then couldn't find the script, so an
expired token silently skipped LLM synthesis and shipped a fallback (unprocessed)
digest instead of failing loudly. Pin the path so a future rename trips a test
rather than degrading mail quality in production.
"""

from news.auth import _AUTO_LOGIN_SCRIPT


def test_auto_login_script_path_is_current():
    path = str(_AUTO_LOGIN_SCRIPT)
    assert "trading-marketplace" not in path, "stale pre-rename repo path"
    assert path.endswith("plessas-trading/scripts/gcloud-auto-login.sh")
