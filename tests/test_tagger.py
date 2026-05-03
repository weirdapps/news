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
