"""Tests for the market profile — config validity + market_synth behavior."""

from datetime import datetime, timezone
from types import SimpleNamespace

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
    now = datetime.now(timezone.utc)
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
