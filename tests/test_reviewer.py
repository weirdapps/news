"""Veracity review: the second pass that checks claims against their citations.

The behaviour that matters most here is not the happy path. It is that this stage
can only ever remove individual unsupported claims, and can never take the digest
down with it. Most of these tests pin failure modes.
"""

import json
from unittest.mock import Mock, patch

from news.models import Article
from news.reviewer import (
    STRIKE_CEILING,
    build_review_prompt,
    collect_claims,
    review_synthesis,
)


def _articles(n=4):
    return [
        Article(
            url=f"https://example.test/{i}",
            title=f"Article {i}",
            source="Src",
            content=f"body of article {i} " * 10,
            categories=["releases"],
            language="en",
        )
        for i in range(n)
    ]


def _synthesis():
    return {
        "executive_brief": [
            {"text": "claim A", "article_ids": [0]},
            {"text": "claim B", "article_ids": [1]},
        ],
        "try_this": [{"text": "do C", "article_ids": [2]}],
        "sections": [
            {
                "category": "releases",
                "display_name": "Sec",
                "synthesis": "section prose",
                "article_ids": [3],
            }
        ],
    }


def _envelope(payload):
    return json.dumps({"result": json.dumps(payload), "stop_reason": "end_turn", "is_error": False})


def _verdicts(**supported):
    """supported maps claim id -> bool; omitted ids are reported supported."""
    return {
        "verdicts": [
            {"id": i, "supported": supported.get(str(i), True), "reason": "r"} for i in range(4)
        ],
        "contradictions": [],
    }


# --- claim collection --------------------------------------------------------


def test_collect_claims_finds_bullets_and_sections():
    claims = collect_claims(_synthesis())
    assert [c["field"] for c in claims] == [
        "executive_brief",
        "executive_brief",
        "try_this",
        "sections",
    ]
    assert [c["text"] for c in claims][-1] == "section prose"


def test_collect_claims_skips_uncited_entries():
    """Nothing to judge against means nothing to judge; citation_filter drops these."""
    s = {
        "executive_brief": [
            {"text": "cited", "article_ids": [0]},
            {"text": "uncited", "article_ids": []},
            {"text": "malformed ids", "article_ids": "nope"},
            "a bare string",
        ],
        "sections": [],
    }
    assert [c["text"] for c in collect_claims(s)] == ["cited"]


def test_build_review_prompt_includes_only_cited_articles():
    """Handing over the whole corpus would let the reviewer justify a claim from
    something the writer never read."""
    s = {"executive_brief": [{"text": "claim", "article_ids": [1]}], "sections": []}
    prompt = build_review_prompt(collect_claims(s), _articles(4))
    assert "Article 1" in prompt
    for missing in ("Article 0", "Article 2", "Article 3"):
        assert missing not in prompt


# --- striking ----------------------------------------------------------------


@patch("news.synthesizer.subprocess.run")
def test_unsupported_claims_are_struck(mock_run):
    mock_run.return_value = Mock(stdout=_envelope(_verdicts(**{"1": False})), returncode=0)
    s = _synthesis()

    out, stats = review_synthesis(s, _articles(4), job="stack")

    assert stats["reviewed"] is True
    assert stats["struck"] == 1
    assert [b["text"] for b in out["executive_brief"]] == ["claim A"]


@patch("news.synthesizer.subprocess.run")
def test_all_supported_leaves_everything_intact(mock_run):
    mock_run.return_value = Mock(stdout=_envelope(_verdicts()), returncode=0)
    s = _synthesis()

    out, stats = review_synthesis(s, _articles(4), job="stack")

    assert stats["struck"] == 0
    assert len(out["executive_brief"]) == 2
    assert len(out["sections"]) == 1


@patch("news.synthesizer.subprocess.run")
def test_striking_multiple_indices_removes_the_right_ones(mock_run):
    """Removal is by descending index; off-by-one here silently deletes good claims."""
    payload = {
        "verdicts": [
            {"id": 0, "supported": False, "reason": "r"},
            {"id": 1, "supported": True, "reason": "r"},
            {"id": 2, "supported": False, "reason": "r"},
            {"id": 3, "supported": True, "reason": "r"},
        ],
        "contradictions": [],
    }
    mock_run.return_value = Mock(stdout=_envelope(payload), returncode=0)
    s = _synthesis()

    out, stats = review_synthesis(s, _articles(4), job="stack")

    assert stats["struck"] == 2
    assert [b["text"] for b in out["executive_brief"]] == ["claim B"]
    assert out["try_this"] == []
    assert len(out["sections"]) == 1


# --- the guards --------------------------------------------------------------


@patch("news.synthesizer.subprocess.run")
def test_a_reviewer_above_the_strike_ceiling_is_disbelieved(mock_run):
    """A reviewer that rejects most of a digest is far likelier to be wrong than the
    digest is. Keep everything and make the anomaly visible."""
    payload = {
        "verdicts": [{"id": i, "supported": False, "reason": "r"} for i in range(4)],
        "contradictions": [],
    }
    mock_run.return_value = Mock(stdout=_envelope(payload), returncode=0)
    s = _synthesis()

    out, stats = review_synthesis(s, _articles(4), job="stack")

    assert stats["struck"] == 0
    assert len(out["executive_brief"]) == 2
    assert "ceiling" in stats["reason"]
    assert STRIKE_CEILING == 0.5


@patch("news.synthesizer.subprocess.run")
def test_a_failed_reviewer_ships_the_unreviewed_digest(mock_run):
    """An unreviewed brief is what shipped every day until now. A missing brief is a
    regression."""
    mock_run.return_value = Mock(stdout="", stderr="", returncode=0)
    s = _synthesis()

    out, stats = review_synthesis(s, _articles(4), job="stack")

    assert stats["reviewed"] is False
    assert stats["reason"] == "reviewer call failed"
    assert len(out["executive_brief"]) == 2


@patch("news.synthesizer.subprocess.run")
def test_a_reviewer_returning_no_verdicts_list_ships_unreviewed(mock_run):
    mock_run.return_value = Mock(stdout=_envelope({"nonsense": 1}), returncode=0)
    s = _synthesis()

    out, stats = review_synthesis(s, _articles(4), job="stack")

    assert stats["reviewed"] is False
    assert len(out["executive_brief"]) == 2


@patch("news.synthesizer.subprocess.run")
def test_verdicts_with_bogus_ids_are_ignored_not_applied(mock_run):
    """Silence is not a strike, and neither is an out-of-range id."""
    payload = {
        "verdicts": [
            {"id": 99, "supported": False, "reason": "r"},
            {"id": "one", "supported": False, "reason": "r"},
            {"id": None, "supported": False},
        ],
        "contradictions": [],
    }
    mock_run.return_value = Mock(stdout=_envelope(payload), returncode=0)
    s = _synthesis()

    out, stats = review_synthesis(s, _articles(4), job="stack")

    assert stats["struck"] == 0
    assert len(out["executive_brief"]) == 2


def test_no_cited_claims_short_circuits_without_calling_the_model():
    with patch("news.synthesizer.subprocess.run") as mock_run:
        out, stats = review_synthesis({"executive_brief": [], "sections": []}, [], job="stack")
    assert mock_run.call_count == 0
    assert stats["reviewed"] is False
    assert stats["reason"] == "no cited claims to review"
    assert out == {"executive_brief": [], "sections": []}


@patch("news.synthesizer.subprocess.run")
def test_contradictions_are_surfaced(mock_run):
    """The specific failure this review exists to catch: the brief asserts flatly and
    its own fact_check quietly undercuts it."""
    payload = {
        "verdicts": [{"id": i, "supported": True, "reason": "r"} for i in range(4)],
        "contradictions": ["brief calls it convergence; fact_check says self-reported"],
    }
    mock_run.return_value = Mock(stdout=_envelope(payload), returncode=0)

    _, stats = review_synthesis(_synthesis(), _articles(4), job="stack")

    assert stats["contradictions"] == ["brief calls it convergence; fact_check says self-reported"]


@patch("news.synthesizer.subprocess.run")
def test_review_is_labelled_as_its_own_job_in_the_trace(mock_run, tmp_path):
    """A review call must be distinguishable from the synthesis call that preceded it."""
    trace = tmp_path / "t.jsonl"
    mock_run.return_value = Mock(stdout=_envelope(_verdicts()), returncode=0)

    with patch.dict("os.environ", {"NEWS_LLM_TRACE": str(trace)}):
        review_synthesis(_synthesis(), _articles(4), job="stack")

    records = [json.loads(x) for x in trace.read_text().splitlines() if x.strip()]
    assert records and records[0]["job"] == "stack-review"


def test_collect_claims_ignores_a_non_dict_section():
    """The model occasionally emits a bare string where a section belongs."""
    s = {"executive_brief": [], "sections": ["not a dict", {"synthesis": "x", "article_ids": [0]}]}
    assert [c["index"] for c in collect_claims(s)] == [1]


def test_striking_a_field_that_is_not_a_list_is_a_no_op():
    """Defensive: a malformed synthesis must not make the strike pass raise.

    Exercised directly. It cannot be reached through review_synthesis, because
    collect_claims reads the same dict and would never yield a claim for a field
    that is not a list -- which is exactly why the branch needs its own test.
    """
    from news.reviewer import _apply

    claims = [{"field": "executive_brief", "index": 0, "text": "a", "ids": [0]}]
    synthesis = {"executive_brief": "not a list"}

    assert _apply(synthesis, claims, {0}) == 0
    assert synthesis["executive_brief"] == "not a list"


def test_striking_an_index_past_the_end_is_a_no_op():
    from news.reviewer import _apply

    claims = [{"field": "executive_brief", "index": 7, "text": "a", "ids": [0]}]
    synthesis = {"executive_brief": [{"text": "kept"}]}

    assert _apply(synthesis, claims, {0}) == 0
    assert len(synthesis["executive_brief"]) == 1


@patch("news.synthesizer.subprocess.run")
def test_contradictions_are_capped_and_non_strings_dropped(mock_run):
    payload = {
        "verdicts": [{"id": i, "supported": True, "reason": "r"} for i in range(4)],
        "contradictions": [f"c{i}" for i in range(9)] + [None, 42],
    }
    mock_run.return_value = Mock(stdout=_envelope(payload), returncode=0)

    _, stats = review_synthesis(_synthesis(), _articles(4), job="stack")

    assert stats["contradictions"] == ["c0", "c1", "c2", "c3", "c4"]


# --- the per-profile wrapper -------------------------------------------------


@patch("news.synthesizer.subprocess.run")
def test_review_and_log_passes_config_through_and_returns_the_synthesis(mock_run, caplog):
    """One line per pipeline instead of eighteen pasted three times."""
    import logging

    from news.reviewer import review_and_log

    mock_run.return_value = Mock(stdout=_envelope(_verdicts(**{"0": False})), returncode=0)
    cfg = {"claude_command": "claude", "claude_args": ["--model", "opus"], "review_timeout": 42}

    with caplog.at_level(logging.INFO, logger="news.reviewer"):
        out = review_and_log(_synthesis(), _articles(4), job="stack", synthesis_config=cfg)

    assert [b["text"] for b in out["executive_brief"]] == ["claim B"]
    assert mock_run.call_args[1]["timeout"] == 42
    assert any("Veracity review [stack]" in r.getMessage() for r in caplog.records)


@patch("news.synthesizer.subprocess.run")
def test_review_and_log_survives_an_empty_synthesis_config(mock_run):
    from news.reviewer import review_and_log

    mock_run.return_value = Mock(stdout=_envelope(_verdicts()), returncode=0)
    out = review_and_log(_synthesis(), _articles(4), job="digest", synthesis_config={})

    assert len(out["executive_brief"]) == 2
    assert mock_run.call_args[1]["timeout"] == 180


def test_the_review_is_validated_against_its_own_schema_not_the_digest_s(caplog):
    """The reviewer's payload is {verdicts, contradictions}, not {executive_brief, sections}.

    parse_synthesis_output defaults to the digest's required keys, so calling it
    bare logged 'Synthesis output unusable' at ERROR twice per review while the
    review itself succeeded. Pure false alarm, in the logs of the stage whose whole
    job is catching false claims. Observed live on the monitor run of 2026-08-27.
    """
    import logging

    from news.reviewer import REVIEW_REQUIRED_KEYS
    from news.synthesizer import parse_synthesis_output

    payload = json.dumps({"verdicts": [{"id": 0, "supported": True}], "contradictions": []})

    with caplog.at_level(logging.ERROR, logger="news.synthesizer"):
        out = parse_synthesis_output(payload, required=REVIEW_REQUIRED_KEYS)

    assert "error" not in out
    assert isinstance(out["verdicts"], list)
    assert not [r for r in caplog.records if "unusable" in r.getMessage()]


@patch("news.synthesizer.subprocess.run")
def test_a_real_review_call_logs_no_schema_error(mock_run, caplog):
    """End-to-end guard on the same thing, through the actual entrypoint."""
    import logging

    mock_run.return_value = Mock(stdout=_envelope(_verdicts()), returncode=0)

    with caplog.at_level(logging.ERROR):
        _, stats = review_synthesis(_synthesis(), _articles(4), job="monitor")

    assert stats["reviewed"] is True
    assert not [r for r in caplog.records if "unusable" in r.getMessage()]
