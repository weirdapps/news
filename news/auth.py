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


def check_gcloud_auth(*, may_wait_for_push: bool = False) -> bool:
    """Check if gcloud auth is valid, attempt auto-refresh if expired.

    Returning False ENDS the run's LLM work: main.py gates ``synthesize()`` on this
    result, so a red pre-flight sends the pipeline straight to the fallback and the
    per-slot alert email. ``invoke_claude``'s reactive WAIT_FOR_PUSH path is never
    reached, because no model call is ever made. That makes this function, not the
    policy loop, the only place a pre-flight credential failure can be cured.

    On Linux the cure is slow and there is only one: the VPS cannot re-authenticate
    itself — its ADC holds only a refresh token — so the remedy is to wait up to 1020s
    for the Mac's 15-minute token push. Whether a run may spend that is a question
    about its remaining budget, so the caller answers it and passes the answer in.
    news-digest's 2400s unit affords it; news-monitor, news-market and news-stack at
    600s do not, and for them waiting would mean being SIGKILLed mid-wait with the
    alert email unsent — strictly worse than failing fast and letting the next slot
    retry. The distinction is arithmetic, not a list of profile names: see
    ``main._may_wait_for_token_push``, which applies the same test ``decide()`` uses
    before it returns WAIT_FOR_PUSH.

    macOS is unaffected by the permission. It has a local remedy that costs seconds
    rather than a quarter of an hour, so it always takes it.

    Args:
        may_wait_for_push: Permission to spend up to PUSH_WAIT_SECONDS waiting for the
            Mac's token push on Linux. Defaults to False, the historic fast-fail, so
            no caller changes behaviour by omission.

    Returns:
        True if authenticated (possibly after a refresh or a wait), False otherwise
    """
    if _token_valid() and _adc_valid():
        logger.info("gcloud auth check: OK")
        return True

    if running_on_linux() and not may_wait_for_push:
        logger.warning(
            "gcloud auth expired and this run's budget cannot fund a token-push wait "
            "— failing fast so the per-slot alert still goes out"
        )
        return False

    # Both remaining paths delegate to the shared policy, which picks the host's
    # remedy: the login script on macOS, polling for the token push on Linux. Its
    # ReauthResult decides the answer, and refresh_auth maps SKIPPED to False —
    # a no-op remedy is not a working credential.
    logger.warning("gcloud auth expired (user token or ADC) — attempting auto-refresh")
    return refresh_auth()
