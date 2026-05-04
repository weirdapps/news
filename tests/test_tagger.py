from news.tagger import extract_tickers_rules

TICKER_DICT = {
    "apple": "AAPL",
    "apple inc.": "AAPL",
    "aapl": "AAPL",
    "microsoft": "MSFT",
    "microsoft corp": "MSFT",
    "msft": "MSFT",
    "alphabet": "GOOG",
    "google": "GOOG",
    "goog": "GOOG",
}


def test_cashtag_match():
    text = "Markets watching $AAPL ahead of earnings"
    assert extract_tickers_rules(text, TICKER_DICT) == ["AAPL"]


def test_company_name_match():
    text = "Apple announced new headphones; Microsoft followed."
    assert sorted(extract_tickers_rules(text, TICKER_DICT)) == ["AAPL", "MSFT"]


def test_no_match():
    text = "Random news about weather"
    assert extract_tickers_rules(text, TICKER_DICT) == []


def test_dedup():
    text = "Apple, $AAPL, and Apple Inc. are all the same company"
    assert extract_tickers_rules(text, TICKER_DICT) == ["AAPL"]


def test_case_insensitive():
    text = "MICROSOFT had a strong quarter"
    assert extract_tickers_rules(text, TICKER_DICT) == ["MSFT"]


def test_word_boundary_no_substring_match():
    text = "Snapple sold record bottles"
    assert extract_tickers_rules(text, TICKER_DICT) == []


def test_returns_sorted_unique():
    text = "Microsoft and Apple beat estimates; Google missed."
    assert extract_tickers_rules(text, TICKER_DICT) == ["AAPL", "GOOG", "MSFT"]


def test_short_key_requires_uppercase_in_original_text():
    """Common English words like 'on'/'as'/'by' must NOT match short tickers."""
    short_dict = {"on": "ON", "as": "AS", "by": "BY", "ai": "AI", "a": "A"}
    text = "The company on Tuesday announced a deal as part of the by-laws."
    # Lowercase prepositions should NOT match
    assert extract_tickers_rules(text, short_dict) == []


def test_short_key_matches_when_uppercase():
    """Same short tickers DO match when explicitly uppercase in text."""
    short_dict = {"on": "ON", "ai": "AI"}
    text = "ON Semiconductor and AI startups led the rally."
    assert extract_tickers_rules(text, short_dict) == ["AI", "ON"]


def test_short_key_cashtag_still_works():
    """Cashtag form $A always matches regardless of length."""
    short_dict = {"a": "A"}
    text = "Shares of $A rose 2%."
    assert extract_tickers_rules(text, short_dict) == ["A"]


def test_long_key_still_case_insensitive():
    """Names ≥4 chars still match case-insensitively when capitalized."""
    long_dict = {"apple": "AAPL", "microsoft": "MSFT"}
    text = "Apple beat estimates while Microsoft fell."
    assert sorted(extract_tickers_rules(text, long_dict)) == ["AAPL", "MSFT"]


def test_long_key_requires_capitalized_in_original():
    """Common English words like 'target'/'news' must NOT match when lowercase."""
    long_dict = {"target": "TGT", "news": "NWSA", "apple": "AAPL"}
    text = "We need to target a news article about apple farming"
    # All matched words are lowercase common words — none should tag as ticker
    assert extract_tickers_rules(text, long_dict) == []


def test_long_key_matches_when_capitalized():
    """Same words DO match when capitalized in text (proper-noun position)."""
    long_dict = {"target": "TGT", "news": "NWSA", "apple": "AAPL"}
    text = "Target announced earnings while Apple closed higher and News Corp filed."
    assert sorted(extract_tickers_rules(text, long_dict)) == ["AAPL", "NWSA", "TGT"]


def test_long_key_uppercase_match():
    """ALL CAPS still matches (e.g., headlines)."""
    long_dict = {"target": "TGT"}
    text = "TARGET CORP REPORTS Q3 EARNINGS"
    assert extract_tickers_rules(text, long_dict) == ["TGT"]


def test_alternation_regex_perf_smoke():
    """Ensure tagger handles a large dict in reasonable time."""
    import time

    big_dict = {f"company{i}": f"TKR{i}" for i in range(5000)}
    big_dict["apple"] = "AAPL"
    text = "Apple announced iPhone updates today. " * 100  # ~3500 chars
    start = time.time()
    for _ in range(20):  # 20 calls = approximate one article worth of work × 20
        extract_tickers_rules(text, big_dict)
    elapsed = time.time() - start
    # Should be well under 5s for 20 calls (= 0.25s per call) with 5K-key dict
    assert elapsed < 5.0, f"Too slow: {elapsed:.2f}s for 20 calls"
    # And should find AAPL
    assert "AAPL" in extract_tickers_rules(text, big_dict)
