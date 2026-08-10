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

import pytest

from main import _llm_budget_seconds, install_llm_deadline
from news.synthesizer import invoke_claude

# Production values, restated here so a change to either side of the comparison is
# visible in the diff. Unit timeouts read live from the VPS on 2026-08-10; the call
# timeout is `synthesis.timeout` in every profile's settings.yaml.
_CALL_TIMEOUT = 300
_GRACE = 90
_DIGEST_BUDGET = 2400 - _CALL_TIMEOUT - _GRACE  # 2010
_TEN_MINUTE_BUDGET = 600 - _CALL_TIMEOUT - _GRACE  # 210


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
    """Guards the whole point of F1: one flat number for every profile was the bug."""
    now = 1_700_000_000.0
    assert install_llm_deadline("digest", now=now) != install_llm_deadline("monitor", now=now)


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
    for profile, unit_timeout in (
        ("digest", 2400),
        ("monitor", 600),
        ("market", 600),
        ("stack", 600),
    ):
        budget = _llm_budget_seconds(
            profile, max_call_seconds=300, unit_timeout_seconds=unit_timeout
        )
        assert budget > 0


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
    """A 429 envelope that costs `latency` seconds of wall clock to produce."""

    def _run(*_args, **_kwargs):
        from unittest.mock import Mock

        clock.now += latency
        return Mock(
            stdout=json.dumps({"is_error": True, "result": "API Error: 429 RESOURCE_EXHAUSTED"}),
            returncode=0,
        )

    return _run


@pytest.mark.parametrize("latency", [2, 60, 200])
def test_a_429_storm_on_a_600s_unit_gives_up_with_time_left_for_the_email(latency, monkeypatch):
    """The merge gate, end to end: real decide(), real invoke_claude, fake clock.

    200s is spec §15's measured worst case for a single CLI call. The assertion is not
    "it terminates" — it already did — but that it terminates early enough that
    rendering and send_email still fit before systemd's SIGTERM at TimeoutStartSec.
    """
    t0 = 1_700_000_000.0
    clock = _FakeClock(t0)
    monkeypatch.setattr("news.synthesizer.time", clock)
    monkeypatch.setattr("news.synthesizer.subprocess.run", _rate_limited(clock, latency))

    deadline = install_llm_deadline("monitor", now=t0)
    assert deadline == t0 + _TEN_MINUTE_BUDGET

    result = invoke_claude("prompt", timeout=_CALL_TIMEOUT)

    assert result is None
    elapsed = clock.now - t0
    assert elapsed + _GRACE <= 600, f"loop ran {elapsed}s, leaving no room before SIGTERM"


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
    """
    t0 = 1_700_000_000.0
    clock = _FakeClock(t0)
    monkeypatch.setattr("news.synthesizer.time", clock)
    monkeypatch.setattr("news.synthesizer.subprocess.run", _rate_limited(clock, latency))

    assert invoke_claude("prompt", timeout=_CALL_TIMEOUT) is None

    assert clock.now - t0 == pre_fix_cost


def test_the_real_clock_is_what_the_deadline_is_measured_against():
    """PTS_LLM_DEADLINE is absolute POSIX time, so `now` must be wall clock.

    A monotonic `now` (uptime, ~1e5) against an epoch deadline (~1.8e9) makes the
    budget test permanently false and switches the mechanism off silently.
    """
    deadline = install_llm_deadline("monitor")
    assert deadline is not None
    assert abs(deadline - (real_time.time() + _TEN_MINUTE_BUDGET)) < 5
