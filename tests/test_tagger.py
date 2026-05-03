from news.tagger import extract_tickers_rules

TICKER_DICT = {
    "apple": "AAPL", "apple inc.": "AAPL", "aapl": "AAPL",
    "microsoft": "MSFT", "microsoft corp": "MSFT", "msft": "MSFT",
    "alphabet": "GOOG", "google": "GOOG", "goog": "GOOG",
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
    """Names ≥4 chars still match case-insensitively."""
    long_dict = {"apple": "AAPL", "microsoft": "MSFT"}
    text = "apple beat estimates while Microsoft fell."
    assert sorted(extract_tickers_rules(text, long_dict)) == ["AAPL", "MSFT"]
