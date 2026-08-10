"""Google Cloud authentication checking and auto-refresh.

Proactive pre-flight (`check_gcloud_auth`) lives here; reactive re-auth
(`refresh_auth`) delegates to the shared policy in `news.llm_policy`.
"""

import logging
import subprocess

from news.llm_policy import ReauthResult, running_on_linux
from news.llm_policy import reauth as shared_reauth

logger = logging.getLogger(__name__)


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


def refresh_auth() -> bool:
    """Force a re-auth via the shared policy. True only on a verified success.

    SKIPPED maps to False deliberately: the script exits 0 on three no-op paths
    (another run holds its lock, a four-failure cooldown, or already authed), and
    reporting those as success spends the caller's one-shot re-auth budget on a
    script that did nothing.
    """
    return shared_reauth() is ReauthResult.SUCCEEDED


def check_gcloud_auth() -> bool:
    """Check if gcloud auth is valid, attempt auto-refresh if expired.

    Returns:
        True if authenticated (possibly after auto-refresh), False otherwise
    """
    if _token_valid() and _adc_valid():
        logger.info("gcloud auth check: OK")
        return True

    if running_on_linux():
        # No pre-flight remedy exists here: the VPS cannot re-authenticate, and
        # waiting for the Mac's 15-minute token push takes longer than some units
        # are allowed to run (news-monitor's TimeoutStartSec is 600s). Fail fast so
        # the pipeline still sends its per-slot alert instead of being SIGKILLed
        # mid-wait.
        #
        # Returning False here ENDS the run's LLM work: main.py:402 gates synthesize()
        # on this result, so on a pre-flight failure the pipeline goes straight to the
        # fallback and the alert email. invoke_claude's reactive WAIT_FOR_PUSH path is
        # never reached, because no model call is ever made. That is the intended
        # trade for the 600s units, and it is a real cost for news-digest at 2400s,
        # which could afford the 1020s wait. Whether digest should be exempted from
        # this fast-fail is an open owner decision, not an oversight here.
        logger.warning("gcloud auth expired and no pre-flight remedy on this host")
        return False

    logger.warning("gcloud auth expired (user token or ADC) — attempting auto-refresh")
    return refresh_auth()
