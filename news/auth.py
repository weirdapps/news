"""Google Cloud authentication checking and auto-refresh.

Proactive pre-flight (`check_gcloud_auth`) lives here; reactive re-auth
(`refresh_auth`) delegates to the shared policy in `news.llm_policy`.
"""

import logging
import re
import subprocess
import time
from dataclasses import dataclass

from news.llm_policy import ReauthResult, running_on_linux
from news.llm_policy import reauth as shared_reauth

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_SECONDS = 15

# Waits between pre-flight re-probes, escalating, in seconds. Finite and short on
# purpose: however large the caller's budget, this schedule stops after four re-probes.
# The transient it exists to ride out is a server-side "reauth is required" 400 from
# Google's token endpoint, which on 2026-08-31 took out a run that had spent 17 of its
# ~600 seconds and was gone three minutes later. Tens of seconds is the right order;
# minutes would be spending the synthesis budget on a coin flip.
_PROBE_BACKOFF_SECONDS = (2.0, 5.0, 10.0, 20.0)

# Every second the schedule above can cost: 37s of sleeping PLUS the probe that follows
# each sleep and the first probe that precedes them all. 112s, not 37s. A probe is a
# subprocess with a timeout, and a network outage is both a plausible cause of the
# refusal and exactly the case that burns that timeout in full, so charging the sleeps
# alone under-counts by 75s, which is the same error llm_policy's push wait made before
# it started charging measured wall clock.
#
# Public because ``main._probe_retry_budget_seconds`` must RESERVE this figure before it
# asks whether the same remaining slack can also fund a 1020s token-push wait. Two
# remedies sized from one pool at one instant and then spent one after the other is the
# double count ``_deadline_reserve_seconds`` warns about.
PROBE_RETRY_WORST_CASE_SECONDS = (
    sum(_PROBE_BACKOFF_SECONDS) + (len(_PROBE_BACKOFF_SECONDS) + 1) * _PROBE_TIMEOUT_SECONDS
)

# gcloud names the active account in the first sentence of several of its auth errors,
# and the probe's stderr is interpolated into a log line in a public repo. Truncation
# alone would not remove it, so it is substituted out before the cut. Length is set by
# what has to survive the cut, not by the account: 117 of those characters are gcloud's
# own "there was a problem refreshing your current auth tokens" preamble, and a window
# that clips the payload after it puts us back where 2026-08-31 started.
_ADDRESS_SHAPED = re.compile(r"\S+@\S+")
_STDERR_CHARS = 240


@dataclass(frozen=True)
class _ProbeResult:
    """A probe outcome, why it failed, and whether re-probing could change the answer.

    ``reason`` is empty on success. ``retryable`` defaults to False so that a failure
    shape added later is fast-failed until someone decides it is worth a second sample.
    """

    ok: bool
    reason: str = ""
    retryable: bool = False


def _redacted(stderr: str) -> str:
    """One collapsed, truncated line of gcloud stderr with anything address-shaped gone.

    Truncate FIRST, then substitute. The other order makes this a CPU bomb: the
    address pattern is applied to the whole of stderr, and inside one long
    whitespace-free token it backtracks quadratically. Measured on the previous
    ordering: 50k unbroken characters took 7.4s and 1M did not finish in 180s,
    all of it burned AFTER subprocess.run returned, so neither the probe timeout
    nor the retry budget bounds it. Cutting to _STDERR_CHARS first caps the input
    at 240 characters, which makes the worst case unmeasurable.
    """
    collapsed = " ".join(stderr.split())[:_STDERR_CHARS]
    return _ADDRESS_SHAPED.sub("<redacted>", collapsed) if collapsed else "no stderr"


def _adc_probe() -> _ProbeResult:
    """Check Application Default Credentials, carrying back the reason on failure.

    ADC is the only credential probed, and the user access token deliberately is not.
    Claude Code's Vertex routing authenticates via ADC; ``llm_policy.default_adc_probe``
    already treats ADC as the single source of truth, and ``reauth()`` verifies its own
    success by polling nothing else. A user-token probe alongside this one therefore
    could not turn a red verdict green. What it did do was double the number of live
    exchanges with a remote token endpoint that had to succeed before the run could
    start, on a host whose PATH shim resolves the user form to ADC anyway. Two samples
    of one flaky remote is two chances to be told no.

    The reason travels with the result because the caller logs it: a stale reauth, an
    expired token and a gcloud that is not on PATH all used to reach the operator as
    the same sentence, which is what made the 2026-08-31 failure unreadable.

    ``retryable`` marks the failures a second sample could plausibly answer differently:
    a live exchange that came back with an error, or one that timed out. A gcloud that
    is not on PATH is deterministic, and re-probing it only spends the budget printing
    the same sentence four more times.

    The final catch is bare on purpose, and narrowing it is a regression. ``text=True``
    decodes the child's output, so one non-UTF-8 byte on stderr raises
    UnicodeDecodeError, a ValueError that no subprocess or OS exception tuple contains.
    A pre-flight probe that can raise takes the run's per-slot alert email down with it,
    which is strictly worse than the credential failure it was trying to report, so
    every unforeseen exception becomes a named False instead.
    """
    try:
        result = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return _ProbeResult(False, "gcloud not found on PATH")
    except subprocess.TimeoutExpired:
        return _ProbeResult(
            False, f"probe timed out after {_PROBE_TIMEOUT_SECONDS}s", retryable=True
        )
    except Exception as exc:  # noqa: BLE001 - see the docstring: never fell the caller
        return _ProbeResult(False, f"probe could not run: {type(exc).__name__}")

    if result.returncode == 0 and result.stdout.strip():
        return _ProbeResult(True)
    return _ProbeResult(
        False,
        f"rc={result.returncode}, stderr: {_redacted(result.stderr)}",
        retryable=True,
    )


def _probe_with_retry(budget_seconds: float) -> _ProbeResult:
    """Probe ADC, re-probing on an escalating backoff, inside ``budget_seconds`` of wall time.

    One sample of a remote OAuth endpoint is not a verdict. ADC has no local cache, so
    every probe is a live exchange and every exchange is an independent chance to be
    handed a transient server-side refusal for a credential that is in fact fine. That
    is what happened on 2026-08-31, and re-probing is the only thing that tells such a
    refusal apart from a real expiry.

    The budget bounds TOTAL elapsed wall time, not the sleeping alone. The clock starts
    before the first probe, and the forward-looking test reserves a whole probe timeout
    on top of the sleep it is about to take, mirroring ``decide()``'s
    ``now + sleep_s + max_call_seconds > deadline``. Charging only the sleeps left the
    bound not binding: measured against a probe that burns its 15s timeout, budgets of
    110s and 210s both spent the same 112s, and a 6s budget spent 32s.

    Overrun is bounded by the first probe, which always runs because it IS the
    pre-flight: a zero budget must still take one sample, exactly as before this existed.
    Every probe after it is reserved for before it is started.

    A failure the retry cannot cure ends the loop rather than riding out the schedule.
    """
    deadline = time.monotonic() + budget_seconds
    result = _adc_probe()
    for delay in _PROBE_BACKOFF_SECONDS:
        if result.ok or not result.retryable:
            break
        if time.monotonic() + delay + _PROBE_TIMEOUT_SECONDS > deadline:
            break
        logger.warning(
            "gcloud ADC probe failed (%s); re-probing in %gs, %.0fs of re-probe budget left",
            result.reason,
            delay,
            deadline - time.monotonic(),
        )
        time.sleep(delay)
        result = _adc_probe()
        if result.ok:
            logger.info("gcloud ADC probe recovered on a re-probe; the refusal was transient")
            break
    return result


def refresh_auth() -> bool:
    """Force a re-auth via the shared policy. True only on a verified success.

    SKIPPED maps to False deliberately: the script exits 0 on three no-op paths
    (another run holds its lock, a four-failure cooldown, or already authed), and
    reporting those as success spends the caller's one-shot re-auth budget on a
    script that did nothing.
    """
    return shared_reauth() is ReauthResult.SUCCEEDED


def check_gcloud_auth(
    *,
    may_wait_for_push: bool = False,
    probe_retry_seconds: float = 0.0,
) -> bool:
    """Check if gcloud auth is valid, attempt auto-refresh if expired.

    Returning False ENDS the run's LLM work: main.py gates ``synthesize()`` on this
    result, so a red pre-flight sends the pipeline straight to the fallback and the
    per-slot alert email. ``invoke_claude``'s reactive WAIT_FOR_PUSH path is never
    reached, because no model call is ever made. That makes this function, not the
    policy loop, the only place a pre-flight credential failure can be cured.

    Before any of that, the probe itself gets a bounded retry, because a single sample
    of a remote endpoint is not a verdict: see ``_probe_with_retry``. Only a credential
    that stays bad for the whole of ``probe_retry_seconds`` reaches the branches below.

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
        probe_retry_seconds: Seconds of wall clock, probe execution included, the caller
            is willing to spend re-probing before it accepts a failure as real. Defaults
            to 0.0, the historic single sample, so no caller changes behaviour by
            omission. Derived from the run's own remaining budget and capped at
            PROBE_RETRY_WORST_CASE_SECONDS, never a constant on its own: see
            ``main._probe_retry_budget_seconds``.

    Returns:
        True if authenticated (possibly after a re-probe, a refresh or a wait), False
        otherwise
    """
    probe = _probe_with_retry(probe_retry_seconds)
    if probe.ok:
        logger.info("gcloud auth check: OK")
        return True

    if running_on_linux() and not may_wait_for_push:
        logger.warning(
            "gcloud pre-flight failed on the ADC credential (%s) and this run's budget "
            "cannot fund a token-push wait — failing fast so the per-slot alert still "
            "goes out",
            probe.reason,
        )
        return False

    # Both remaining paths delegate to the shared policy, which picks the host's
    # remedy: the login script on macOS, polling for the token push on Linux. Its
    # ReauthResult decides the answer, and refresh_auth maps SKIPPED to False —
    # a no-op remedy is not a working credential.
    logger.warning(
        "gcloud pre-flight failed on the ADC credential (%s) — attempting auto-refresh",
        probe.reason,
    )
    return refresh_auth()
