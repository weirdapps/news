import csv

from scripts.build_tickers_yaml import build_ticker_dict


def test_build_dict_from_csv(tmp_path):
    csv_path = tmp_path / "etoro.csv"
    with csv_path.open("w") as f:
        w = csv.writer(f)
        w.writerow(["TKR", "NAME"])
        w.writerow(["AAPL", "Apple Inc."])
        w.writerow(["MSFT", "Microsoft Corp"])
        w.writerow(["GOOG", "Alphabet Inc."])
    result = build_ticker_dict(csv_path)
    assert result["aapl"] == "AAPL"
    assert result["apple inc."] == "AAPL"
    assert result["apple"] == "AAPL"  # stripped suffix


def test_strips_corporate_suffixes(tmp_path):
    csv_path = tmp_path / "etoro.csv"
    with csv_path.open("w") as f:
        w = csv.writer(f)
        w.writerow(["TKR", "NAME"])
        w.writerow(["BRK.B", "Berkshire Hathaway Inc"])
        w.writerow(["JNJ", "Johnson & Johnson"])
    result = build_ticker_dict(csv_path)
    assert result["berkshire hathaway"] == "BRK.B"
    assert result["johnson & johnson"] == "JNJ"


def test_strips_trailing_period(tmp_path):
    """Source CSV has dirty names like 'MICROSOFT.' (no Corp suffix, just period)."""
    csv_path = tmp_path / "etoro.csv"
    with csv_path.open("w") as f:
        w = csv.writer(f)
        w.writerow(["TKR", "NAME"])
        w.writerow(["MSFT", "MICROSOFT."])
        w.writerow(["GOOG", "ALPHABET ."])
    result = build_ticker_dict(csv_path)
    assert result["microsoft"] == "MSFT"
    assert result["alphabet"] == "GOOG"


def test_handles_period_as_suffix_separator(tmp_path):
    """Source CSV has 'AMAZON.CO.' with period instead of space before suffix."""
    csv_path = tmp_path / "etoro.csv"
    with csv_path.open("w") as f:
        w = csv.writer(f)
        w.writerow(["TKR", "NAME"])
        w.writerow(["AMZN", "AMAZON.CO."])
    result = build_ticker_dict(csv_path)
    assert result["amazon"] == "AMZN"


def test_stoplist_drops_common_words(tmp_path):
    """Common English words should not appear as dict keys even if they're tickers."""
    csv_path = tmp_path / "etoro.csv"
    with csv_path.open("w") as f:
        w = csv.writer(f)
        w.writerow(["TKR", "NAME"])
        w.writerow(["NWSA", "News Corp"])  # 'news' should be filtered
        w.writerow(["TGT", "Target Corp"])  # 'target' should be filtered
        w.writerow(["AAPL", "Apple Inc."])  # 'apple' is fine — proper noun
    result = build_ticker_dict(csv_path)
    # Stoplist drops common-word keys
    assert "news" not in result
    assert "target" not in result
    # But the tickers themselves are still registered
    assert result.get("nwsa") == "NWSA"
    assert result.get("tgt") == "TGT"
    # And legitimate names are intact
    assert result.get("apple") == "AAPL"
