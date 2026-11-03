"""Characterization tests: what every pipeline does at its three shared seams.

Pinned before the #41 refactor pulled the synthesis dispatch, the deliver branch and
the run tail out into helpers, so the extraction is checked against the behaviour the
VPS timers already depend on: the verdict main() turns into an exit code, which email
goes out (digest or alert), where a failed send is saved, and what log_run records.
"""

import asyncio
import sqlite3
from contextlib import ExitStack
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import main as m
from news.models import Article
from news.storage import init_db

_SYNTHESIZED = {
    "executive_brief": [{"text": "a sourced claim", "article_ids": [1]}],
    "sections": [],
    "company_mentions": [],
}

# profile -> (patch target of its synthesizer, run_type label in runs.log,
#             save_fallback label, notify_macos title on a failed send)
_PROFILES = {
    "digest": ("main.synthesize", "scheduled", "digest", "News Digest Send Failed"),
    "monitor": ("main.synthesize_monitor", "monitor-scheduled", "monitor", "Monitor Send Failed"),
    "stack": ("news.stack_synth.synthesize_stack", "stack-scheduled", "stack", "Stack Send Failed"),
    "market": ("news.market_synth.synthesize_market", "market-scheduled", None, None),
    "topic": ("main.synthesize_topic", "topic-adhoc", "topic", "Topic Brief Send Failed"),
}


def _settings(tmp_path):
    return {
        "pipeline": {
            "max_digest_articles": 100,
            "max_articles_per_source": 10,
            "min_article_length_words": 2,
            "max_article_age_hours": 36,
            "digest_window_hours": 24,
        },
        "email": {"recipient": "r@example.com"},
        "storage": {
            "db_path": str(tmp_path / "news.db"),
            "run_log_path": str(tmp_path / "runs.log"),
            "transcripts_db_path": str(tmp_path / "transcripts.db"),
        },
        "schedule": {"timezone": "Europe/Athens", "runs": ["00:00", "12:00"]},
        "synthesis": {"max_retries": 1, "timeout": 60, "claude_command": "claude"},
        "scoring": {},
    }


def _article(profile):
    return Article(
        url=f"https://example.com/{profile}/one",
        title="One story",
        source="Source One",
        content="Body text long enough for every quality gate in these settings.",
        categories=["ai"],
        language="en",
        published_at=datetime.now(UTC),
        relevance_score=5,
    )


def _process(articles, existing_hashes, **_):
    out = [a for a in articles if a.content_hash not in existing_hashes]
    stats = {"output_count": len(out), "duplicates": 0, "quality_dropped": 0}
    return out, {**stats, "unmentioned_dropped": 0}


def _run(profile, tmp_path, *, synthesis_ok=True, auth=True, sent=True, print_only=False):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    articles = [_article(profile)]
    verdict = (dict(_SYNTHESIZED), True) if synthesis_ok else ("fallback text", False)
    synth_target = _PROFILES[profile][0]
    mocks = {
        "send_email": MagicMock(return_value=sent),
        "save_fallback": MagicMock(return_value="/tmp/fallback.html"),
        "notify_macos": MagicMock(),
        "log_run": MagicMock(),
        "insert_digest": MagicMock(return_value=7),
        "update_digest_sent": MagicMock(),
        "synth": MagicMock(return_value=verdict),
    }
    with ExitStack() as stack:
        for name, value in [
            ("main.get_settings", MagicMock(return_value=_settings(tmp_path))),
            ("main.get_sources", MagicMock(return_value={"rss_feeds": []})),
            ("main.get_categories", MagicMock(return_value={"categories": {}})),
            ("main.get_keywords", MagicMock(return_value={"display": {"monitor_label": "Mon"}})),
            ("main.get_connection", MagicMock(return_value=conn)),
            ("main.init_db", MagicMock()),
            ("main.check_gcloud_auth", MagicMock(return_value=auth)),
            ("main.process_articles", MagicMock(side_effect=_process)),
            ("main.fetch_all_sources", AsyncMock(return_value=(articles, []))),
            ("main.fetch_rss_feeds", AsyncMock(return_value=(articles, []))),
            ("news.reviewer.review_and_log", MagicMock(side_effect=lambda r, *a, **k: r)),
            (synth_target, mocks["synth"]),
        ]:
            stack.enter_context(patch(name, value))
        for name in ("send_email", "save_fallback", "notify_macos", "log_run"):
            stack.enter_context(patch(f"main.{name}", mocks[name]))
        stack.enter_context(patch("main.insert_digest", mocks["insert_digest"]))
        stack.enter_context(patch("main.update_digest_sent", mocks["update_digest_sent"]))
        if profile == "topic":
            coro = m.run_topic_pipeline(query="anything", print_only=print_only)
        else:
            coro = getattr(m, f"run_{profile}_pipeline")()
        delivered = asyncio.run(coro)
    return delivered, mocks


_EMAILING = ["digest", "monitor", "stack"]


@pytest.mark.parametrize("profile", [*_EMAILING, "topic"])
def test_a_synthesized_run_is_delivered_and_records_the_send(profile, tmp_path):
    delivered, mocks = _run(profile, tmp_path)

    assert delivered is True
    assert mocks["send_email"].call_count == 1
    assert "synthesis unavailable" not in mocks["send_email"].call_args.kwargs["subject"]
    assert mocks["send_email"].call_args.kwargs["recipient"] == "r@example.com"
    mocks["update_digest_sent"].assert_called_once_with(
        mocks["update_digest_sent"].call_args[0][0], 7
    )
    log = mocks["log_run"].call_args.kwargs
    assert (log["run_type"], log["synthesis_ok"], log["sent_ok"]) == (
        _PROFILES[profile][1],
        True,
        True,
    )
    assert (log["article_count"], log["new_count"]) == (1, 1)


@pytest.mark.parametrize("profile", _EMAILING)
@pytest.mark.parametrize("auth", [True, False], ids=["synthesis-failed", "auth-expired"])
def test_a_withheld_run_sends_one_alert_and_reports_degraded(profile, auth, tmp_path):
    delivered, mocks = _run(profile, tmp_path, synthesis_ok=False, auth=auth)

    assert delivered is False
    assert mocks["synth"].call_count == (1 if auth else 0)
    assert mocks["send_email"].call_count == 1
    sent = mocks["send_email"].call_args.kwargs
    assert "synthesis unavailable" in sent["subject"]
    assert "AI synthesis could not be produced" in sent["html_body"]
    digest = mocks["insert_digest"].call_args[0][1]
    assert digest.html_output == sent["html_body"]
    assert digest.synthesis_text != ""
    assert mocks["log_run"].call_args.kwargs["synthesis_ok"] is False


def test_a_withheld_topic_run_still_sends_the_brief_marked_failed(tmp_path):
    delivered, mocks = _run("topic", tmp_path, synthesis_ok=False)

    assert delivered is False
    assert mocks["send_email"].call_count == 1
    assert mocks["insert_digest"].call_args[0][1].synthesis_text == "fallback text"


@pytest.mark.parametrize("profile", [*_EMAILING, "topic"])
def test_a_failed_send_saves_the_html_and_is_not_delivered(profile, tmp_path):
    delivered, mocks = _run(profile, tmp_path, sent=False)

    assert delivered is False
    html = mocks["send_email"].call_args.kwargs["html_body"]
    saved = mocks["save_fallback"].call_args
    assert saved.args[0] == html
    assert saved.kwargs.get("label", "digest") == _PROFILES[profile][2]
    assert mocks["notify_macos"].call_args.kwargs == {
        "title": _PROFILES[profile][3],
        "message": "Saved to /tmp/fallback.html",
    }
    mocks["update_digest_sent"].assert_not_called()
    assert mocks["log_run"].call_args.kwargs["sent_ok"] is False


@pytest.mark.parametrize("synthesis_ok", [True, False])
def test_the_market_run_stores_without_sending(synthesis_ok, tmp_path):
    delivered, mocks = _run("market", tmp_path, synthesis_ok=synthesis_ok)

    assert delivered is synthesis_ok
    mocks["send_email"].assert_not_called()
    digest = mocks["insert_digest"].call_args[0][1]
    assert (digest.pipeline, digest.html_output) == ("market", "")
    log = mocks["log_run"].call_args.kwargs
    assert (log["run_type"], log["sent_ok"], log["synthesis_ok"]) == (
        "market-scheduled",
        None,
        synthesis_ok,
    )


def test_a_printed_topic_run_is_delivered_without_an_email(tmp_path, capsys):
    delivered, mocks = _run("topic", tmp_path, print_only=True)

    assert delivered is True
    mocks["send_email"].assert_not_called()
    mocks["update_digest_sent"].assert_not_called()
    assert mocks["log_run"].call_args.kwargs["sent_ok"] is True
    assert "<html" in capsys.readouterr().out.lower()
