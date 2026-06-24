import json
from unittest.mock import MagicMock, patch

from news.tagger import extract_tickers_llm


def _mock_proc(stdout_text, returncode=0):
    proc = MagicMock()
    proc.stdout = stdout_text
    proc.stderr = ""
    proc.returncode = returncode
    return proc


@patch("news.tagger.subprocess.run")
def test_llm_returns_tickers(mock_run):
    mock_run.return_value = _mock_proc(json.dumps({"tickers": ["AAPL", "MSFT"]}))
    out = extract_tickers_llm("Apple and Microsoft beat estimates")
    assert out == ["AAPL", "MSFT"]
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "claude"
    assert "--model" in cmd
    assert "sonnet" in cmd


@patch("news.tagger.subprocess.run")
def test_llm_returns_empty_when_no_tickers(mock_run):
    mock_run.return_value = _mock_proc(json.dumps({"tickers": []}))
    out = extract_tickers_llm("Weather report for Athens")
    assert out == []


@patch("news.tagger.subprocess.run")
def test_llm_handles_malformed_response(mock_run):
    mock_run.return_value = _mock_proc('{"oops": "no tickers key"}')
    out = extract_tickers_llm("Some article")
    assert out == []


@patch("news.tagger.subprocess.run")
def test_llm_handles_markdown_fenced_json(mock_run):
    mock_run.return_value = _mock_proc('```json\n{"tickers": ["AAPL"]}\n```')
    out = extract_tickers_llm("Apple news")
    assert out == ["AAPL"]


@patch("news.tagger.subprocess.run")
def test_llm_uppercases_and_dedups(mock_run):
    mock_run.return_value = _mock_proc(json.dumps({"tickers": ["aapl", "AAPL", "msft"]}))
    out = extract_tickers_llm("text")
    assert out == ["AAPL", "MSFT"]


@patch("news.tagger.subprocess.run")
def test_llm_returns_empty_on_subprocess_failure(mock_run):
    mock_run.side_effect = Exception("command not found")
    out = extract_tickers_llm("text")
    assert out == []


@patch("news.tagger.subprocess.run")
def test_llm_returns_empty_on_nonzero_exit(mock_run):
    mock_run.return_value = _mock_proc("error", returncode=1)
    out = extract_tickers_llm("text")
    assert out == []


@patch("news.tagger.subprocess.run")
def test_llm_passes_custom_model(mock_run):
    mock_run.return_value = _mock_proc(json.dumps({"tickers": []}))
    extract_tickers_llm("text", model="opus")
    cmd = mock_run.call_args[0][0]
    assert "opus" in cmd
