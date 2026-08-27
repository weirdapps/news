"""Tests for the optional LLM upgrade of a deterministic changelog delta.

No claude CLI is ever invoked: ``subprocess.run`` is patched in every test that
reaches it. The module's whole contract is that it degrades instead of failing,
so most of what follows asserts that a failure path leaves the deterministic
delta exactly where ``news.changelog_delta`` put it.

Articles are ``SimpleNamespace`` stubs rather than real ``Article`` instances:
only three attributes are touched here, and the stub keeps this file
independent of the dataclass.
"""

import logging
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

from news.changelog_digest import DIGEST_CAP, digest_prose, enrich_changelog_digests

_SYSTEM_PROMPT_TITLE = "Claude Opus 5 system prompt (July 24, 2026)"
_PLATFORM_TITLE = "Claude Platform Release Notes — August 11, 2026"

_DELTA = (
    "MODEL IDS +: claude-opus-5-20260714\n"
    "KNOWLEDGE CUTOFF: May 2026 (was May 2025)\n"
    "DELTA vs Claude Opus 4.5 (November 24, 2025): 3 of 210 sentences/tags changed (1%); "
    "+1 added, -1 removed, ~1 edited.\n"
    "+ [product_information] Claude Opus 5 is available on the Max plan.\n"
)


def _article(title: str = _SYSTEM_PROMPT_TITLE, digest: str = _DELTA) -> SimpleNamespace:
    return SimpleNamespace(
        title=title,
        changelog_digest=digest,
        changelog_digest_source="deterministic",
    )


def _completed(returncode: int = 0, stdout: str = "prose", stderr: str = "") -> Mock:
    return Mock(returncode=returncode, stdout=stdout, stderr=stderr)


class _FakeClock:
    """A ``monotonic()`` that advances a fixed step on every read.

    Real elapsed time across mocked calls is microseconds, so the only way to
    exercise the wall-clock guard is to make the clock expensive, not the work.
    """

    def __init__(self, step: float):
        self.step = step
        self.now = 0.0

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


# --- digest_prose: the CLI contract -------------------------------------------


def test_digest_prose_calls_the_claude_cli_with_the_house_argv(monkeypatch):
    """Owner policy: every LLM call goes through the local CLI, never an SDK.

    Also pins the flags that are deliberately absent: ``--output-format json``
    returns RC=1 alongside an ``is_error`` envelope and prefixes stdout with an
    ANSI clear when TERM is set.

    ``env`` IS now passed, which the previous contract forbade. The reason it was
    forbidden was that a hand-built env would strip the Vertex configuration the
    CLI reads from the caller's environment; ``vertex_cli_model_and_env`` returns a
    superset of ``os.environ`` instead of a fresh mapping, so that concern is met
    by construction and asserted below.
    """
    monkeypatch.setenv("VERTEX_MODEL_LIGHT", "claude-sonnet-4-6")
    monkeypatch.setenv("VERTEX_REGION_LIGHT", "europe-west1")
    monkeypatch.setenv("A_CALLER_ENV_VAR", "must-survive")
    completed = _completed(stdout="  Opus 5 replaces Opus 4.5 on claude.ai.  ")
    with patch("news.changelog_digest.subprocess.run", return_value=completed) as run:
        prose = digest_prose(_DELTA, _SYSTEM_PROMPT_TITLE)

    assert prose == "Opus 5 replaces Opus 4.5 on claude.ai."
    # The exact provisioned id, not the bare alias: this call runs on the VPS, whose
    # parent env carries CLOUD_ML_REGION=eu, and sonnet in eu is a 429.
    assert run.call_args[0][0] == ["claude", "--model", "claude-sonnet-4-6", "--print"]
    kwargs = run.call_args[1]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert "check" not in kwargs
    assert kwargs["env"]["CLOUD_ML_REGION"] == "europe-west1"
    assert kwargs["env"]["A_CALLER_ENV_VAR"] == "must-survive"
    prompt = kwargs["input"]
    assert _DELTA in prompt
    assert _SYSTEM_PROMPT_TITLE in prompt


def test_digest_prose_pins_the_cli_timeout():
    """45s is arithmetic, not taste.

    The stack unit is 600s; synthesis reserves 150s and shutdown grace 90s,
    leaving 360s pre-synthesis that the tagger already claims up to 90s of. A
    call starting just inside the 90s budget and running its full 45s is 135s,
    so 135 + 90 = 225 <= 360. Raising this spends someone else's margin.
    """
    with patch("news.changelog_digest.subprocess.run", return_value=_completed()) as run:
        digest_prose(_DELTA, _SYSTEM_PROMPT_TITLE)

    assert run.call_args[1]["timeout"] == 45


def test_digest_prose_honours_an_explicit_timeout():
    with patch("news.changelog_digest.subprocess.run", return_value=_completed()) as run:
        digest_prose(_DELTA, _SYSTEM_PROMPT_TITLE, timeout=10)

    assert run.call_args[1]["timeout"] == 10


def test_digest_prompt_survives_braces_in_the_delta():
    """The delta legitimately contains braces, so the prompt cannot be str.format()ed.

    ``{+added+}`` is our own inline-diff marker and ``{{currentDateTime}}`` is
    the vendor's own template variable; both raise or corrupt under .format().
    """
    delta = "~ [tools] The date is [-{{currentDate}}-]{+{{currentDateTime}}+} today."
    with patch("news.changelog_digest.subprocess.run", return_value=_completed()) as run:
        digest_prose(delta, _SYSTEM_PROMPT_TITLE)

    assert delta in run.call_args[1]["input"]


def test_digest_prose_scopes_a_system_prompt_entry_to_claude_ai():
    """The system-prompt page governs claude.ai, not the API the reader actually calls."""
    with patch("news.changelog_digest.subprocess.run", return_value=_completed()) as run:
        digest_prose(_DELTA, _SYSTEM_PROMPT_TITLE)

    prompt = run.call_args[1]["input"]
    assert "does NOT govern the Claude API" in prompt
    assert "Claude Platform release notes page" not in prompt


def test_digest_prose_scopes_a_platform_entry_to_the_api():
    with patch("news.changelog_digest.subprocess.run", return_value=_completed()) as run:
        digest_prose(_DELTA, _PLATFORM_TITLE)

    prompt = run.call_args[1]["input"]
    assert "Claude Platform release notes page" in prompt
    assert "does NOT govern the Claude API" not in prompt


def test_digest_prose_passes_an_explicit_scope_through_unchanged():
    with patch("news.changelog_digest.subprocess.run", return_value=_completed()) as run:
        digest_prose(_DELTA, _SYSTEM_PROMPT_TITLE, "A caller-supplied scope note.")

    prompt = run.call_args[1]["input"]
    assert "A caller-supplied scope note." in prompt
    assert "does NOT govern the Claude API" not in prompt


# --- digest_prose: every failure path returns "" ------------------------------


def test_digest_prose_returns_empty_when_the_cli_times_out(caplog):
    boom = subprocess.TimeoutExpired(cmd=["claude"], timeout=45)
    with patch("news.changelog_digest.subprocess.run", side_effect=boom):
        with caplog.at_level(logging.WARNING, logger="news.changelog_digest"):
            prose = digest_prose(_DELTA, _SYSTEM_PROMPT_TITLE)

    assert prose == ""
    assert "TimeoutExpired" in caplog.text


def test_digest_prose_returns_empty_when_the_cli_is_missing(caplog):
    """A CLI that is not on PATH must degrade, not crash the stack pipeline."""
    with patch("news.changelog_digest.subprocess.run", side_effect=OSError("command not found")):
        with caplog.at_level(logging.WARNING, logger="news.changelog_digest"):
            prose = digest_prose(_DELTA, _SYSTEM_PROMPT_TITLE)

    assert prose == ""
    assert "command not found" in caplog.text


def test_digest_prose_logs_stderr_when_the_cli_exits_nonzero(caplog):
    completed = _completed(returncode=2, stdout="", stderr="usage: claude [options]")
    with patch("news.changelog_digest.subprocess.run", return_value=completed):
        with caplog.at_level(logging.WARNING, logger="news.changelog_digest"):
            prose = digest_prose(_DELTA, _SYSTEM_PROMPT_TITLE)

    assert prose == ""
    assert "usage: claude [options]" in caplog.text


def test_digest_prose_logs_stdout_when_the_cli_exits_nonzero(caplog):
    """A Vertex 429 arrives on stdout with stderr empty; stderr alone logs nothing."""
    completed = _completed(
        returncode=1,
        stdout="API Error: 429 quota exceeded for claude-opus-5 in europe-west1",
        stderr="",
    )
    with patch("news.changelog_digest.subprocess.run", return_value=completed):
        with caplog.at_level(logging.WARNING, logger="news.changelog_digest"):
            prose = digest_prose(_DELTA, _SYSTEM_PROMPT_TITLE)

    assert prose == ""
    assert "429 quota exceeded" in caplog.text


def test_digest_prose_returns_empty_when_the_cli_prints_nothing():
    """RC=0 with no output is still a failure; the caller must keep the delta."""
    with patch("news.changelog_digest.subprocess.run", return_value=_completed(stdout="  \n ")):
        assert digest_prose(_DELTA, _SYSTEM_PROMPT_TITLE) == ""


# --- enrich_changelog_digests: upgrade in place, or leave well alone ----------


def test_enrich_changelog_digests_marks_an_upgraded_entry_as_llm():
    """The source marker is what lets the backfill tell an upgraded row from a fallback."""
    article = _article()
    prose = "Opus 5 replaces Opus 4.5 on claude.ai.\nSTACK IMPACT: none for this stack."
    with patch(
        "news.changelog_digest.subprocess.run", return_value=_completed(stdout=prose)
    ) as run:
        result = enrich_changelog_digests([article])

    assert result == (1, 1)
    assert article.changelog_digest == prose
    assert article.changelog_digest_source == "llm"
    assert run.call_args[1]["input"].endswith(_DELTA)
    assert run.call_args[1]["timeout"] == 45


def test_enrich_changelog_digests_keeps_the_deterministic_delta_on_timeout():
    article = _article()
    boom = subprocess.TimeoutExpired(cmd=["claude"], timeout=45)
    with patch("news.changelog_digest.subprocess.run", side_effect=boom):
        result = enrich_changelog_digests([article])

    assert result == (0, 1)
    assert article.changelog_digest == _DELTA
    assert article.changelog_digest_source == "deterministic"


def test_enrich_changelog_digests_keeps_the_deterministic_delta_on_oserror():
    """A CLI that is not installed degrades the digest; it does not fail the pipeline."""
    article = _article()
    with patch("news.changelog_digest.subprocess.run", side_effect=OSError("command not found")):
        result = enrich_changelog_digests([article])

    assert result == (0, 1)
    assert article.changelog_digest == _DELTA
    assert article.changelog_digest_source == "deterministic"


def test_enrich_changelog_digests_upgrades_what_it_can_when_one_call_fails():
    """One failure must not abandon the entries behind it in the list."""
    first, second = _article(title="First entry"), _article(title="Second entry")
    outcomes = [
        subprocess.TimeoutExpired(cmd=["claude"], timeout=45),
        _completed(stdout="What the second entry changed."),
    ]
    with patch("news.changelog_digest.subprocess.run", side_effect=outcomes):
        result = enrich_changelog_digests([first, second])

    assert result == (1, 2)
    assert first.changelog_digest_source == "deterministic"
    assert second.changelog_digest_source == "llm"


def test_enrich_changelog_digests_ignores_articles_without_a_delta():
    """Only changelog entries carry a delta; every other article in the run is skipped."""
    plain = SimpleNamespace(title="Some RSS item", changelog_digest="", changelog_digest_source="")
    with patch("news.changelog_digest.subprocess.run") as run:
        result = enrich_changelog_digests([plain])

    assert result == (0, 0)
    assert run.call_count == 0


def test_enrich_changelog_digests_stops_at_the_wall_clock_budget(caplog):
    """An upstream reformat minting a burst of entries must not SIGKILL the unit."""
    articles = [_article(title=f"Entry {i} system prompt (July 24, 2026)") for i in range(20)]
    with (
        patch("news.changelog_digest.time.monotonic", _FakeClock(step=40)),
        patch("news.changelog_digest.subprocess.run", return_value=_completed()) as run,
        caplog.at_level(logging.INFO, logger="news.changelog_digest"),
    ):
        result = enrich_changelog_digests(articles, budget_seconds=90)

    assert run.call_count == 2
    assert result == (2, 20)
    assert all(a.changelog_digest == _DELTA for a in articles[2:])
    assert all(a.changelog_digest_source == "deterministic" for a in articles[2:])
    assert "budget 90s reached" in caplog.text


def test_enrich_changelog_digests_caps_a_runaway_prose_response():
    """The prose lands in the field the delta occupies, so it lives under the same ceiling."""
    article = _article()
    with patch("news.changelog_digest.subprocess.run", return_value=_completed(stdout="x" * 5000)):
        result = enrich_changelog_digests([article])

    assert result == (1, 1)
    assert len(article.changelog_digest) == DIGEST_CAP
