"""Google Cloud authentication checking and auto-refresh.

Delegates browser automation to the centralized gcloud-auto-login.sh script
shared across all Claude Code projects and the news pipeline.
"""

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_AUTO_LOGIN_SCRIPT = Path.home() / "SourceCode/plessas-trading/scripts/gcloud-auto-login.sh"


def _token_valid() -> bool:
    """Check if the gcloud user access token is currently valid."""
    try:
        result = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        return False


def _adc_valid() -> bool:
    """Check if Application Default Credentials (ADC) are currently valid.

    Claude Code's Vertex routing authenticates via ADC, so a stale ADC fails the
    call even when the user access token looks fine. Probing both closes the
    false-green gap that let an invalid_rapt run ship an unsynthesized digest.
    """
    try:
        result = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        return False


def _refresh_auth(timeout: int = 120) -> bool:
    """Refresh gcloud auth using the centralized auto-login script."""
    logger.info("Attempting gcloud auth refresh via auto-login script")

    if not _AUTO_LOGIN_SCRIPT.exists():
        logger.error(f"Auto-login script not found: {_AUTO_LOGIN_SCRIPT}")
        return False

    try:
        result = subprocess.run(
            [str(_AUTO_LOGIN_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

        for line in result.stdout.strip().splitlines():
            logger.info(line)

        if result.returncode == 0 and _token_valid():
            logger.info("gcloud auth refresh: OK")
            return True

        logger.warning(f"gcloud auth refresh failed (rc={result.returncode})")
        if result.stderr:
            logger.warning(result.stderr[:300])
        return False

    except subprocess.TimeoutExpired:
        logger.warning(f"gcloud auth refresh timed out after {timeout}s")
        return False
    except Exception as e:
        logger.error(f"gcloud auth refresh failed: {e}")
        return False


def refresh_auth() -> bool:
    """Force a gcloud re-auth (user creds + ADC) via the centralized auto-login script.

    Used reactively when a Vertex call fails with an auth-class error (e.g.
    invalid_rapt) — the access token can look valid while the RAPT reauth proof
    has lapsed, which only the live call reveals.

    Returns:
        True if re-auth succeeded, False otherwise
    """
    return _refresh_auth()


def check_gcloud_auth() -> bool:
    """Check if gcloud auth is valid, attempt auto-refresh if expired.

    Returns:
        True if authenticated (possibly after auto-refresh), False otherwise
    """
    if _token_valid() and _adc_valid():
        logger.info("gcloud auth check: OK")
        return True

    logger.warning("gcloud auth expired (user token or ADC) — attempting auto-refresh")
    return _refresh_auth()
