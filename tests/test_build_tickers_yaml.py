from pathlib import Path
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
