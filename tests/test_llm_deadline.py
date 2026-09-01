"""The runner's PTS_LLM_DEADLINE contract (spec §8).

Nothing in the estate exports PTS_LLM_DEADLINE — not this repo, not ~/.config/systemd
or ~/scripts on the VPS. So resolve_deadline() falls back to now + 900 for every
profile, which is 1.5x the ENTIRE 600s budget of three of the four scheduled news
units. Spec §5 rule 2, whose whole job is keeping the loop inside its unit, can
therefore only fire after the unit is already dead.

That stopped being academic when the policy port introduced real backoff sleeps where
the old nested loops had none. Measured against the 900s default with the real
decide(): a persistent 429 costs 428s of in-loop time at 2s call latency, 660s at
60s and 780s at 200s, against roughly 8s pre-port. On a 600s unit that is SIGTERM,
which main.py's `except Exception` does not catch, so send_email never runs and the
one-email-per-slot contract silently breaks.
"""

import json
import os
import time as real_time
from types import SimpleNamespace

import pytest

from main import (
    _MAX_REACHABLE_BACKOFF_SECONDS,
    _deadline_reserve_seconds,
    _llm_budget_seconds,
    _may_wait_for_token_push,
    _preflight_auth_ok,
    _probe_retry_budget_seconds,
    install_llm_deadline,
)
from news.auth import PROBE_RETRY_WORST_CASE_SECONDS
from news.config import get_settings
from news.llm_policy import PUSH_WAIT_SECONDS
from news.synthesizer import invoke_claude

# Production values, restated here so a change to either side of the comparison is
# visible in the diff rather than silently agreeing with itself. Unit timeouts read
# live from the VPS on 2026-08-10; call timeouts are `synthesis.timeout` in each
# profile's settings.yaml.
#
# The three 600s profiles were lowered 300 -> 150 on 2026-08-10 (owner ruling). At 300
# their budget was 210s and decide() could never fund a retry on any failure shape; at
# 150 it is 360s and a fast error gets its second attempt. digest is unchanged at 300:
# its 2400s unit never had the problem.
_CALL_TIMEOUT = {"digest": 300, "monitor": 150, "market": 150, "stack": 150}
_GRACE = 90
_UNIT = {"digest": 2400, "monitor": 600, "market": 600, "stack": 600}
_DIGEST_BUDGET = 2400 - 300 - _GRACE  # 2010, unchanged by the 2026-08-10 ruling
_TEN_MINUTE_BUDGET = 600 - 150 - _GRACE  # 360, was 210 before the ruling


@pytest.fixture(autouse=True)
def _no_inherited_deadline():
    """Every test starts with the variable genuinely unset, whatever the shell had.

    Cleans up on the way out too. install_llm_deadline writes to os.environ directly,
    which monkeypatch does not know about and therefore will not restore, so without
    the second pop the first test here would set a real deadline for every test that
    runs after it in the same process.
    """
    os.environ.pop("PTS_LLM_DEADLINE", None)
    yield
    os.environ.pop("PTS_LLM_DEADLINE", None)


@pytest.mark.parametrize(
    ("profile", "expected_budget"),
    [
        ("digest", _DIGEST_BUDGET),
        ("monitor", _TEN_MINUTE_BUDGET),
        ("market", _TEN_MINUTE_BUDGET),
        ("stack", _TEN_MINUTE_BUDGET),
    ],
)
def test_each_scheduled_profile_gets_its_own_units_deadline(profile, expected_budget, monkeypatch):
    """The deadline is per unit, not one number for all four.

    Reads the profile's real config for max_call_seconds, so these are the numbers
    production will use, not fixtures.
    """
    now = 1_700_000_000.0
    deadline = install_llm_deadline(profile, now=now)

    assert deadline == now + expected_budget
    assert float(os.environ["PTS_LLM_DEADLINE"]) == deadline


def test_the_digest_and_the_ten_minute_units_do_not_share_a_deadline():
    """Guards the whole point of F1: one flat number for every profile was the bug.

    The pop between the two calls is load-bearing, not tidiness. install_llm_deadline
    installs with ``setdefault``, so a second call in the same process keeps the first
    profile's value and returns it. Production never does that, one profile per
    process, so clearing between the calls is what makes this model a real run instead
    of an impossible one.
    """
    now = 1_700_000_000.0
    digest = install_llm_deadline("digest", now=now)
    os.environ.pop("PTS_LLM_DEADLINE", None)
    monitor = install_llm_deadline("monitor", now=now)
    assert digest != monitor


def test_an_ad_hoc_profile_gets_no_deadline_at_all():
    """`topic` is run by hand from a shell and has no systemd unit.

    There is no TimeoutStartSec to derive a deadline from, and inventing one would be
    a guess. Leaving the variable unset lets resolve_deadline apply its own documented
    900s default, which is the right behaviour for an interactive run.
    """
    assert install_llm_deadline("topic", now=1_700_000_000.0) is None
    assert "PTS_LLM_DEADLINE" not in os.environ


def test_an_operator_override_wins_over_the_computed_deadline(monkeypatch):
    """setdefault, not assignment. An operator must still be able to override."""
    monkeypatch.setenv("PTS_LLM_DEADLINE", "1234567890")

    install_llm_deadline("monitor", now=1_700_000_000.0)

    assert os.environ["PTS_LLM_DEADLINE"] == "1234567890"


# --- the refuse-to-run assertion ------------------------------------------------


def test_a_unit_that_cannot_fund_one_call_refuses_to_run():
    """Spec §8: assert the margin is positive at startup, refuse to run if it is not.

    300s is the shape spec §8 found in sb-calendar-sync and cured by raising the unit
    to 15min. A unit that cannot fund a single worst-case call plus its shutdown grace
    cannot produce output, and the honest failure is an immediate loud one — which
    exits nonzero and fires OnFailure=hc-fail@ — rather than a SIGTERM twenty minutes
    later with no email.
    """
    with pytest.raises(RuntimeError) as excinfo:
        _llm_budget_seconds("monitor", max_call_seconds=600, unit_timeout_seconds=300)

    message = str(excinfo.value)
    # The message must carry the arithmetic, or the operator cannot act on it.
    assert "300" in message and "600" in message and "90" in message


def test_the_assertion_is_not_tripped_by_the_real_units():
    """The four production units must all pass. A fix that bricks them is not a fix."""
    for profile, unit_timeout in _UNIT.items():
        budget = _llm_budget_seconds(
            profile,
            max_call_seconds=_CALL_TIMEOUT[profile],
            unit_timeout_seconds=unit_timeout,
        )
        assert budget > 0


def test_the_reserve_is_max_call_plus_grace_and_nothing_else():
    """The reserve tracks each profile's own call timeout, and omits max_backoff.

    Two profiles' worth of arithmetic, so the function cannot be a constant in
    disguise: 240s for the three 600s profiles at a 150s call, 390s for digest at 300s.

    The omission of max_backoff is the load-bearing part. decide()'s budget test is
    forward-looking, so it already refuses any backoff whose sleep plus the following
    call would not fit; reserving the maximum again in the deadline charges for it
    twice. The consequence is asserted as BEHAVIOUR rather than as arithmetic, because
    the arithmetic alone stopped being damning once the call timeout dropped to 150:
    §8's literal reserve of 480 leaves a positive 120s margin, so the refuse-to-run
    assertion would no longer catch it, and the only visible symptom would be the
    quiet loss of the retry the 2026-08-10 ruling was made to buy.
    """
    assert _MAX_REACHABLE_BACKOFF_SECONDS == 240

    for profile in ("monitor", "market", "stack"):
        max_call = _CALL_TIMEOUT[profile]
        assert _deadline_reserve_seconds(max_call) == max_call + _GRACE == 240
    assert _deadline_reserve_seconds(_CALL_TIMEOUT["digest"]) == 390

    # A fast 429 nine seconds into a monitor run, which is decide()'s own test.
    ours = _UNIT["monitor"] - _deadline_reserve_seconds(150)
    literal_s8 = _UNIT["monitor"] - (150 + _MAX_REACHABLE_BACKOFF_SECONDS + _GRACE)
    assert literal_s8 > 0, "the §8 reserve no longer trips the startup assertion at 150"
    needed = 9 + 60 + 150  # now + backoff(1, RATE_LIMIT) + one more call
    assert needed <= ours, "our reserve must fund the retry"
    assert needed > literal_s8, "restoring max_backoff must visibly lose that retry"


def test_a_margin_of_exactly_zero_is_refused():
    """`> 0`, not `>= 0`: a budget of zero funds nothing."""
    with pytest.raises(RuntimeError):
        _llm_budget_seconds("monitor", max_call_seconds=300, unit_timeout_seconds=390)


# --- the consequence the deadline exists to produce -----------------------------


class _FakeClock:
    """Stands in for news.synthesizer's `time` module. Advanced explicitly, never real.

    Replaces the module attribute rather than patching time.time globally, so pytest's
    own timing is untouched.
    """

    def __init__(self, start):
        self.now = start

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _rate_limited(clock, latency):
    """A 429 envelope that costs `latency` seconds of wall clock to produce.

    A Mock so callers can read ``.call_count``; the side effect advances the clock.
    """
    from unittest.mock import Mock

    def _run(*_args, **_kwargs):
        clock.now += latency
        return Mock(
            stdout=json.dumps({"is_error": True, "result": "API Error: 429 RESOURCE_EXHAUSTED"}),
            returncode=0,
        )

    return Mock(side_effect=_run)


@pytest.mark.parametrize("latency", [2, 60, 150])
def test_a_429_storm_on_a_600s_unit_gives_up_with_time_left_for_the_email(latency, monkeypatch):
    """The merge gate, end to end: real decide(), real invoke_claude, fake clock.

    150s is the ceiling a single call can now reach on these profiles, so it is the
    worst case rather than §15's 200.6s, which the 300s timeout used to permit. The
    assertion is not "it terminates" — it already did — but that it terminates early
    enough that rendering and send_email still fit before SIGTERM at TimeoutStartSec.
    """
    t0 = 1_700_000_000.0
    clock = _FakeClock(t0)
    monkeypatch.setattr("news.synthesizer.time", clock)
    monkeypatch.setattr("news.synthesizer.subprocess.run", _rate_limited(clock, latency))

    deadline = install_llm_deadline("monitor", now=t0)
    assert deadline == t0 + _TEN_MINUTE_BUDGET

    result = invoke_claude("prompt", timeout=_CALL_TIMEOUT["monitor"])

    assert result is None
    elapsed = clock.now - t0
    assert elapsed + _GRACE <= 600, f"loop ran {elapsed}s, leaving no room before SIGTERM"


# --- what lowering synthesis.timeout to 150 actually bought ----------------------


@pytest.mark.parametrize(
    ("profile", "pre_synthesis_seconds"),
    [("monitor", 4), ("market", 9), ("stack", 91)],
)
def test_the_600s_profiles_now_fund_a_second_attempt(profile, pre_synthesis_seconds, monkeypatch):
    """The entire point of the 2026-08-10 ruling, and nothing else asserted it.

    ``pre_synthesis_seconds`` is each profile's MEASURED median time from pipeline
    start to the synthesis call — fetch, dedupe, score and LLM tagging — taken from
    ~7 weeks of VPS logs (monitor 4s, market 9s, stack 91s). It matters because the
    deadline is absolute from process start, so that phase is spent budget by the time
    invoke_claude runs, and stack in particular has little left.

    A fast 429 is the retry worth having. A timeout retried under the same ceiling
    rarely behaves differently, which is why 150 was chosen over 120.
    """
    # The pinned timeout must be the one production actually passes, or this test
    # would keep passing against a config that no longer funds the retry: the deadline
    # comes from the config while the call ceiling comes from the pin, and a drift
    # between them is exactly the case worth catching here.
    assert (
        get_settings(profile=profile).get("synthesis", {}).get("timeout") == _CALL_TIMEOUT[profile]
    )

    t0 = 1_700_000_000.0
    clock = _FakeClock(t0)
    run = _rate_limited(clock, latency=5)
    monkeypatch.setattr("news.synthesizer.time", clock)
    monkeypatch.setattr("news.synthesizer.subprocess.run", run)

    install_llm_deadline(profile, now=t0)
    clock.now = t0 + pre_synthesis_seconds  # fetch, dedupe, score, tag

    assert invoke_claude("prompt", timeout=_CALL_TIMEOUT[profile]) is None

    assert run.call_count >= 2, "the 429 retry the lowered timeout was meant to fund"
    elapsed = clock.now - t0
    assert elapsed + _GRACE <= _UNIT[profile], f"ran {elapsed}s, no room before SIGTERM"


def test_the_old_300s_timeout_funded_no_retry_at_all(monkeypatch):
    """Why the ruling was needed. Reproduces the old budget directly.

    At `synthesis.timeout: 300` on a 600s unit the budget was 600 - 300 - 90 = 210,
    and decide()'s forward-looking test (`now + backoff + 300 > 210`) refused every
    retry from the very first failure — one paid call, then straight to the fallback
    and the alert email. Asserted so the config cannot drift back without this going
    red and saying why.
    """
    t0 = 1_700_000_000.0
    clock = _FakeClock(t0)
    run = _rate_limited(clock, latency=5)
    monkeypatch.setattr("news.synthesizer.time", clock)
    monkeypatch.setattr("news.synthesizer.subprocess.run", run)
    monkeypatch.setenv("PTS_LLM_DEADLINE", repr(t0 + (600 - 300 - _GRACE)))

    assert invoke_claude("prompt", timeout=300) is None

    assert run.call_count == 1


@pytest.mark.parametrize(
    ("latency", "pre_fix_cost"),
    [(2, 428), (60, 660), (200, 780)],
)
def test_without_a_deadline_the_same_storm_overruns_the_unit(latency, pre_fix_cost, monkeypatch):
    """Characterises the bug, so the fix cannot be quietly reverted.

    With PTS_LLM_DEADLINE unset, resolve_deadline returns a flat now + 900 and the
    loop spends the review's measured 428/660/780s. Two of the three exceed a 600s
    unit outright; all three exceed it once fetching and tagging are counted. This is
    the number the fix has to beat, and it is asserted exactly so that a change to
    MAX_ATTEMPTS, ROW_CAPS or backoff() shows up here rather than in production.

    Held at a 300s call timeout deliberately. That is what every profile used when the
    reviewer measured these three numbers, and it is still digest's value today, so
    they remain live rather than historical.
    """
    t0 = 1_700_000_000.0
    clock = _FakeClock(t0)
    monkeypatch.setattr("news.synthesizer.time", clock)
    monkeypatch.setattr("news.synthesizer.subprocess.run", _rate_limited(clock, latency))

    assert invoke_claude("prompt", timeout=_CALL_TIMEOUT["digest"]) is None

    assert clock.now - t0 == pre_fix_cost


def test_the_real_clock_is_what_the_deadline_is_measured_against():
    """PTS_LLM_DEADLINE is absolute POSIX time, so `now` must be wall clock.

    A monotonic `now` (uptime, ~1e5) against an epoch deadline (~1.8e9) makes the
    budget test permanently false and switches the mechanism off silently.
    """
    deadline = install_llm_deadline("monitor")
    assert deadline is not None
    assert abs(deadline - (real_time.time() + _TEN_MINUTE_BUDGET)) < 5


# --- F6: who may wait for the Mac's token push, and why ---------------------------


@pytest.mark.parametrize(
    ("profile", "permitted"),
    [("digest", True), ("monitor", False), ("market", False), ("stack", False)],
)
def test_only_a_unit_whose_budget_affords_the_wait_may_take_it(profile, permitted):
    """Derived from the budget, never from the profile name.

    Only news-digest passes: 2010s of budget against the 1020 + 300 the wait needs.
    The three 600s profiles have 360s against a 1020 + 150 requirement, so lowering
    their call timeout moved both sides and left them still — correctly — unable to
    afford it. Their Task 5 anti-SIGKILL property survives the 2026-08-10 ruling.
    """
    t0 = 1_700_000_000.0
    install_llm_deadline(profile, now=t0)

    assert _may_wait_for_token_push(_CALL_TIMEOUT[profile], now=t0) is permitted


def test_the_wait_permission_flips_exactly_at_the_budget_boundary(monkeypatch):
    """Same arithmetic decide() uses, so pre-flight and reactive path cannot disagree.

    Boundary pinned in both directions: an off-by-one here either strands the digest
    or SIGKILLs a unit mid-wait.
    """
    t0 = 1_700_000_000.0
    max_call = _CALL_TIMEOUT["digest"]
    need = PUSH_WAIT_SECONDS + max_call

    monkeypatch.setenv("PTS_LLM_DEADLINE", repr(t0 + need))
    assert _may_wait_for_token_push(max_call, now=t0) is True

    monkeypatch.setenv("PTS_LLM_DEADLINE", repr(t0 + need - 1))
    assert _may_wait_for_token_push(max_call, now=t0) is False


def test_the_digest_stops_permitting_the_wait_once_the_run_has_spent_its_budget():
    """The permission is evaluated against the clock, not granted once at startup.

    Fetching and tagging run before the pre-flight, so a slow fetch can consume the
    room the wait needed. 2010 - (1020 + 300) = 690s of prior work is the limit.
    """
    t0 = 1_700_000_000.0
    install_llm_deadline("digest", now=t0)

    assert _may_wait_for_token_push(_CALL_TIMEOUT["digest"], now=t0 + 690) is True
    assert _may_wait_for_token_push(_CALL_TIMEOUT["digest"], now=t0 + 691) is False


def test_a_full_push_wait_still_leaves_the_digest_room_to_synthesise_and_to_alert():
    """The owner's question: does the wait solve the missed-email problem or move it?

    Walked at the worst instant the wait can be granted, so every later grant has
    strictly more room. It solves it — the reserve that makes the answer yes is the
    same max_call + grace F1 already holds back from the deadline.
    """
    t0 = 1_700_000_000.0
    sigterm = t0 + 2400  # news-digest TimeoutStartSec
    deadline = install_llm_deadline("digest", now=t0)
    assert deadline == t0 + _DIGEST_BUDGET

    latest_grant = t0 + 690
    assert _may_wait_for_token_push(_CALL_TIMEOUT["digest"], now=latest_grant) is True
    after_wait = latest_grant + PUSH_WAIT_SECONDS

    # Branch 1 — the wait FAILS. Pre-flight returns False, synthesis is skipped, and
    # the run goes straight to the alert email with 690s to spare.
    assert sigterm - after_wait == 690

    # Branch 2 — the wait SUCCEEDS. One worst-case synthesis call still fits inside
    # the deadline exactly, and the deadline then refuses a retry.
    assert after_wait + _CALL_TIMEOUT["digest"] == deadline
    # Whatever happens next, render + send_email own the whole F1 reserve: one
    # max_call plus the systemd stop grace.
    assert sigterm - deadline == _CALL_TIMEOUT["digest"] + _GRACE == 390


# --- The re-probe budget: derived the same way, spent on a far cheaper remedy -----


@pytest.mark.parametrize("profile", ["digest", "monitor", "market", "stack"])
def test_every_profile_can_fund_a_re_probe_even_when_it_cannot_fund_the_wait(profile):
    """A zero for the three 600s units would be a bug: they are the ones this is for.

    They are also exactly the units _may_wait_for_token_push correctly refuses, which is
    the point of having a second remedy costing 112s rather than 1020.

    All four arrive at the cap rather than at their own slack, because every one of them
    starts with more room than the retry schedule can spend: 1710s for digest, 210s for
    the rest. Handing that raw figure over would reserve seconds no re-probe can use.
    """
    t0 = 1_700_000_000.0
    install_llm_deadline(profile, now=t0)

    budget = _probe_retry_budget_seconds(_CALL_TIMEOUT[profile], now=t0)
    assert budget == PROBE_RETRY_WORST_CASE_SECONDS == 112


def test_a_run_that_has_spent_its_budget_re_probes_zero_times():
    """Clock-evaluated, like the wait permission: a slow fetch spends the room.

    Below the cap the raw slack is what is left, so the taper down to the historic
    single sample is unaffected by capping the top.
    """
    t0 = 1_700_000_000.0
    install_llm_deadline("market", now=t0)

    assert _probe_retry_budget_seconds(_CALL_TIMEOUT["market"], now=t0 + 209) == 1.0
    assert _probe_retry_budget_seconds(_CALL_TIMEOUT["market"], now=t0 + 210) == 0.0
    assert _probe_retry_budget_seconds(_CALL_TIMEOUT["market"], now=t0 + 999) == 0.0


def test_the_two_remedies_cannot_both_spend_the_same_slack():
    """They run in sequence, so pricing both at the same instant charges one pool twice.

    Walked at the latest instant the wait can be granted, where the digest has exactly
    1020 + 300 left. Spend the re-probe budget out of that and the wait no longer fits,
    which is the honest answer: 690 + 112 + 1020 + 300 overruns the deadline by 112s,
    and on the far side of that deadline is the render and the alert email.
    """
    t0 = 1_700_000_000.0
    install_llm_deadline("digest", now=t0)
    latest_grant = t0 + 690

    assert _may_wait_for_token_push(_CALL_TIMEOUT["digest"], now=latest_grant) is True
    spent = _probe_retry_budget_seconds(_CALL_TIMEOUT["digest"], now=latest_grant)
    assert spent == PROBE_RETRY_WORST_CASE_SECONDS
    assert _may_wait_for_token_push(_CALL_TIMEOUT["digest"], now=latest_grant + spent) is False


def test_the_pre_flight_reserves_the_re_probe_budget_before_it_prices_the_wait(monkeypatch):
    """The double count lives at the call site, so pin what the call site actually passes.

    Same instant as the test above. Reading the clock twice would be its own bug, so
    only one reading is offered.
    """
    t0 = 1_700_000_000.0
    install_llm_deadline("digest", now=t0)
    monkeypatch.setattr("main.time", SimpleNamespace(time=lambda: t0 + 690))

    passed = {}
    monkeypatch.setattr("main.check_gcloud_auth", lambda **kwargs: passed.update(kwargs) or True)

    assert _preflight_auth_ok({"timeout": _CALL_TIMEOUT["digest"]}) is True
    assert passed == {
        "may_wait_for_push": False,
        "probe_retry_seconds": PROBE_RETRY_WORST_CASE_SECONDS,
    }


def test_a_digest_with_room_for_both_still_gets_its_wait():
    """The reservation must not cost the one profile the wait was built for.

    At the top of the run the digest holds 2010s: 112 of re-probing and then the full
    1020 + 300 still fit, so capping and reserving the re-probe budget leaves the
    2026-08-10 wait ruling exactly where it was.
    """
    t0 = 1_700_000_000.0
    install_llm_deadline("digest", now=t0)

    spent = _probe_retry_budget_seconds(_CALL_TIMEOUT["digest"], now=t0)
    assert _may_wait_for_token_push(_CALL_TIMEOUT["digest"], now=t0 + spent) is True


# --- FIX 2: unit-timeout table cross-check against live systemd -------------------


def test_systemd_unavailable_falls_back_to_table_silently(monkeypatch):
    """On macOS/CI where systemctl doesn't exist, the table value is used unchanged.

    Mutation-resistance: if the fallback were removed and FileNotFoundError propagated,
    install_llm_deadline would raise, causing this test to fail rather than return a
    budget.
    """
    import subprocess

    from main import _query_systemd_timeout

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no systemctl"))
    )
    # Must return None (degrade silently) rather than raising.
    result = _query_systemd_timeout("monitor")
    assert result is None


def test_systemd_nonzero_exit_falls_back_silently(monkeypatch):
    """A unit not found or a non-user-session query returns non-zero; must not raise."""
    import subprocess
    from unittest.mock import MagicMock

    from main import _query_systemd_timeout

    proc = MagicMock()
    proc.returncode = 1
    proc.stdout = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: proc)
    assert _query_systemd_timeout("monitor") is None


def test_an_unknown_unit_is_ignored_even_though_systemctl_succeeds(monkeypatch):
    """`systemctl show` does NOT fail on an unknown unit: it exits 0 with the defaults.

    This is what turned CI red. On the ubuntu runner systemd exists but the news units do
    not, so the query returned the manager default of 1min 30s with exit 0. The code took
    min(table, 90), the margin went negative, and the startup assertion refused to run
    every profile. Verified on the VPS: an invented unit name returns "1min 30s" with
    LoadState=not-found, while news-digest returns "40min" with LoadState=loaded. Only a
    loaded unit's timeout means anything, so LoadState is the gate.
    """
    import subprocess
    from unittest.mock import MagicMock

    from main import _query_systemd_timeout

    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = "LoadState=not-found\nTimeoutStartUSec=1min 30s\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: proc)
    assert _query_systemd_timeout("digest") is None


def test_systemd_timeout_microseconds_parsed_correctly(monkeypatch):
    """TimeoutStartUSec returns raw microseconds; the parser must convert to seconds.

    Mutation-resistance: if the parser returned microseconds raw (not dividing by 1e6),
    the returned value would be 600000000 rather than 600, failing the equality check.
    """
    import subprocess
    from unittest.mock import MagicMock

    from main import _query_systemd_timeout

    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = "LoadState=loaded\nTimeoutStartUSec=600000000\n"  # 600s in microseconds
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: proc)
    assert _query_systemd_timeout("monitor") == 600


def test_systemd_timeout_minute_string_parsed_correctly(monkeypatch):
    """Duration strings like '10min' must be parsed to seconds."""
    import subprocess
    from unittest.mock import MagicMock

    from main import _query_systemd_timeout

    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = "LoadState=loaded\nTimeoutStartUSec=10min\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: proc)
    assert _query_systemd_timeout("monitor") == 600


def test_systemd_below_table_uses_smaller_and_warns(monkeypatch, caplog):
    """When systemd reports a LOWER timeout than the table, the smaller wins.

    This is the dangerous direction: the table over-budgets, can grant a wait the
    unit cannot survive, and the run is SIGKILLed with no alert email.

    Mutation-resistance: if install_llm_deadline ignored the systemd value and always
    used the table, the deadline would be t0 + (2400 - 300 - 90) = t0 + 2010, not
    the smaller systemd-derived value, and the assertion would fail.
    """
    import logging
    import subprocess
    from unittest.mock import MagicMock

    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = "LoadState=loaded\nTimeoutStartUSec=1800000000\n"  # 1800s vs table 2400s
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: proc)

    now = 1_700_000_000.0
    with caplog.at_level(logging.WARNING, logger="main"):
        deadline = install_llm_deadline("digest", now=now)

    # Budget must use the smaller systemd value (1800), not the table (2400).
    expected_budget = 1800 - _CALL_TIMEOUT["digest"] - _GRACE  # 1800 - 300 - 90 = 1410
    assert deadline == now + expected_budget
    # Operator must be warned with both values.
    assert any("1800" in r.message and "2400" in r.message for r in caplog.records)


def test_systemd_above_table_uses_table_and_warns(monkeypatch, caplog):
    """When systemd reports a HIGHER timeout than the table, the table wins.

    This direction is conservative (safe): using the table under-budgets relative to
    reality, but that is never dangerous.

    Mutation-resistance: if the larger systemd value were used, the deadline would be
    t0 + (3600 - 300 - 90) = t0 + 3210, not t0 + 2010, failing the equality check.
    """
    import logging
    import subprocess
    from unittest.mock import MagicMock

    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = "LoadState=loaded\nTimeoutStartUSec=3600000000\n"  # 3600s vs table 2400s
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: proc)

    now = 1_700_000_000.0
    with caplog.at_level(logging.WARNING, logger="main"):
        deadline = install_llm_deadline("digest", now=now)

    # Budget must use the table value (2400), not the larger systemd value.
    expected_budget = 2400 - _CALL_TIMEOUT["digest"] - _GRACE  # 2010
    assert deadline == now + expected_budget
    assert any("3600" in r.message and "2400" in r.message for r in caplog.records)


def test_systemd_agrees_with_table_no_warning(monkeypatch, caplog):
    """When systemd matches the table exactly, no warning is logged."""
    import logging
    import subprocess
    from unittest.mock import MagicMock

    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = "LoadState=loaded\nTimeoutStartUSec=2400000000\n"  # matches the table
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: proc)

    now = 1_700_000_000.0
    with caplog.at_level(logging.WARNING, logger="main"):
        install_llm_deadline("digest", now=now)

    assert not any(r.levelno == logging.WARNING for r in caplog.records)
