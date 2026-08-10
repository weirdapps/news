"""Tests for the market profile — config validity + market_synth behavior."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from news.config import VALID_PROFILES, get_categories, get_settings, get_sources


def test_market_profile_registered():
    assert "market" in VALID_PROFILES


def test_market_settings_load():
    s = get_settings(profile="market")
    assert s["schedule"]["runs"], "market profile must define schedule runs"
    assert s["storage"]["db_path"]
    assert "synthesis" in s
    assert "scoring" in s


def test_market_sources_structured():
    src = get_sources(profile="market")
    feeds = src["rss_feeds"]
    assert len(feeds) >= 10
    for f in feeds:
        assert f["name"] and f["url"] and f["category"] and f["language"]
    # TipRanks news must be one of the sources (user requirement)
    assert any("tipranks" in f["url"].lower() for f in feeds)
    # broad market anchors present
    joined = " ".join(f["url"].lower() for f in feeds)
    assert "reuters" in joined and "federalreserve" in joined


def test_market_categories_present():
    c = get_categories(profile="market")
    cats = c["categories"]
    for key in (
        "macro_rates",
        "equities",
        "sectors_themes",
        "commodities_energy",
        "crypto",
        "greece_athex",
    ):
        assert key in cats, f"missing category {key}"
        assert cats[key]["keywords"], f"category {key} has no keywords"


def _mk_article(title, source="Reuters", content=("word " * 20), lang="en"):
    now = datetime.now(UTC)
    return SimpleNamespace(
        title=title,
        source=source,
        content=content,
        language=lang,
        url="https://example.com/a",
        published_at=now,
        fetched_at=now,
    )


def test_build_market_prompt_includes_articles_and_schema():
    from news.market_synth import build_market_prompt

    arts = [_mk_article("Fed holds rates steady"), _mk_article("NVDA earnings beat")]
    prompt = build_market_prompt(arts, time_window="last 18 hours")
    assert "Fed holds rates steady" in prompt
    assert "NVDA earnings beat" in prompt
    # schema + citation contract must be in the prompt
    assert "executive_brief" in prompt
    assert "article_ids" in prompt
    assert "macro_rates" in prompt


def test_market_fallback_lists_headlines():
    from news.market_synth import build_market_fallback

    out = build_market_fallback([_mk_article("Oil spikes on OPEC cut")])
    assert "Oil spikes on OPEC cut" in out


def test_synthesize_market_falls_back_on_no_output(monkeypatch):
    import news.market_synth as ms

    monkeypatch.setattr(ms, "invoke_claude", lambda *a, **k: None)
    result, ok = ms.synthesize_market([_mk_article("x")], max_retries=1)
    assert ok is False
    assert isinstance(result, str)  # fallback text, not a dict


# --- FIX 1: market must stop pretending to send -----------------------------------


def test_log_run_with_none_sent_ok_writes_no_email(tmp_path):
    """When sent_ok is None (store-only, no send attempted), the log must not say
    'send FAILED' — that implies a failed attempt, not an intentional skip.

    Mutation-resistance: removing the None branch and treating None as False would
    write 'send FAILED', failing the assertion on the left side of the 'not in' check.
    """
    from main import log_run

    log_path = tmp_path / "runs.log"
    log_run(str(log_path), "market-scheduled", 50, 30, True, None, 5.0)
    content = log_path.read_text()
    assert "send FAILED" not in content
    assert "no email" in content


def test_log_run_with_false_sent_ok_still_writes_send_failed(tmp_path):
    """Existing behaviour for other pipelines is unchanged: False => 'send FAILED'."""
    from main import log_run

    log_path = tmp_path / "runs.log"
    log_run(str(log_path), "scheduled", 10, 5, True, False, 2.0)
    assert "send FAILED" in log_path.read_text()


def test_market_pipeline_always_logs_no_email(tmp_path, monkeypatch):
    """Market never logs 'send FAILED' — it has no send implementation at all.

    run_market_pipeline has no send_email call in its body (lines 1195-1352 on master).
    Regardless of whether NEWS_MARKET_RECIPIENT is set, sent_ok must be None so that
    log_run writes 'no email' rather than implying a failed SMTP attempt.

    Mutation-resistance: hardcoding sent_ok=False (reverting the fix) makes log_run
    write 'send FAILED', which fails the 'not in' assertion. Setting the env var
    explicitly here proves the branch is gone — if a branch were re-introduced that
    returned False when the var is present, the assertion would still catch it.
    """
    import asyncio

    import main as m

    monkeypatch.setenv("NEWS_MARKET_RECIPIENT", "analyst@example.com")  # var IS set
    monkeypatch.setattr(
        m,
        "get_settings",
        lambda profile: {
            "pipeline": {
                "max_digest_articles": 5,
                "max_articles_per_source": 5,
                "min_article_length_words": 5,
                "max_article_age_hours": 48,
                "digest_window_hours": 48,
            },
            "email": {"recipient": "user@example.com"},
            "storage": {
                "db_path": str(tmp_path / "test.db"),
                "run_log_path": str(tmp_path / "run.log"),
                "archive_dir": str(tmp_path / "archive"),
                "archive_after_days": 30,
            },
            "schedule": {"timezone": "Europe/Athens", "runs": ["09:00"]},
            "synthesis": {
                "timeout": 10,
                "max_retries": 1,
                "claude_command": "claude",
                "claude_args": [],
            },
            "scoring": {},
        },
    )
    monkeypatch.setattr(m, "get_sources", lambda profile: {"rss_feeds": []})
    monkeypatch.setattr(m, "get_categories", lambda profile: {"categories": {}})
    monkeypatch.setattr(m, "fetch_all_sources", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(
        m,
        "process_articles",
        lambda **kw: ([], {"output_count": 0, "duplicates": 0, "quality_dropped": 0}),
    )
    monkeypatch.setattr(m, "_preflight_auth_ok", lambda _: False)
    monkeypatch.setattr(m, "install_llm_deadline", lambda *a, **k: None)

    asyncio.run(m.run_market_pipeline(run_type="scheduled"))

    log_content = (tmp_path / "run.log").read_text()
    assert "send FAILED" not in log_content
