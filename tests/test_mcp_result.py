"""Unit tests for the canonical MCP error model (news/mcp_result.py).

Kept in sync with the trading-data copy of the same helper.
"""

import inspect
import json

import pytest

from news import mcp_result
from news.mcp_result import (
    AuthRequired,
    Internal,
    InvalidInput,
    MCPError,
    NotFound,
    Unavailable,
    Upstream,
    tool,
)


class TestErrorShape:
    def test_base_defaults(self):
        e = MCPError("boom")
        assert e.code == "internal"
        assert e.retryable is False
        assert e.to_dict() == {"error": "internal", "message": "boom", "retryable": False}

    def test_json_str_roundtrips(self):
        parsed = json.loads(str(NotFound("missing file: /x/y.csv")))
        assert parsed["error"] == "not_found"
        assert parsed["message"] == "missing file: /x/y.csv"
        assert parsed["retryable"] is False

    def test_codes_match_classes(self):
        assert InvalidInput("x").code == "invalid_input"
        assert NotFound("x").code == "not_found"
        assert AuthRequired("x").code == "auth_required"
        assert Upstream("x").code == "upstream"
        assert Unavailable("x").code == "unavailable"
        assert Internal("x").code == "internal"

    def test_all_codes_in_closed_set(self):
        for cls in (InvalidInput, NotFound, AuthRequired, Upstream, Unavailable, Internal):
            assert cls("x").code in mcp_result.CODES

    def test_retryable_override(self):
        assert Upstream("x", retryable=True).retryable is True

    def test_remediation_and_details_optional(self):
        bare = MCPError("x").to_dict()
        assert "remediation" not in bare
        assert "details" not in bare
        rich = AuthRequired(
            "no creds", remediation="run login", details={"service": "etoro"}
        ).to_dict()
        assert rich["remediation"] == "run login"
        assert rich["details"] == {"service": "etoro"}


class TestCoerce:
    def test_mcperror_passthrough(self):
        e = NotFound("x")
        assert mcp_result._coerce(e) is e

    def test_filenotfound_maps_to_not_found(self):
        c = mcp_result._coerce(FileNotFoundError("db not found: /p.db"))
        assert isinstance(c, NotFound)
        assert "/p.db" in c.message

    def test_unknown_maps_to_internal(self):
        c = mcp_result._coerce(ValueError("weird"))
        assert isinstance(c, Internal)
        assert "ValueError" in c.message


class TestToolDecorator:
    def test_success_passthrough(self):
        @tool
        def f():
            return [{"title": "x"}]

        assert f() == [{"title": "x"}]

    def test_preserves_metadata_and_signature(self):
        @tool
        def f(query, limit=20) -> list:
            """docstring here"""
            return []

        assert f.__name__ == "f"
        assert f.__doc__ == "docstring here"
        assert hasattr(f, "__wrapped__")
        params = inspect.signature(f).parameters
        assert "query" in params
        assert "limit" in params

    def test_mcperror_reraised_as_is(self):
        @tool
        def f():
            raise Unavailable("db down", remediation="start db")

        with pytest.raises(Unavailable) as ei:
            f()
        assert ei.value.code == "unavailable"
        assert json.loads(str(ei.value))["remediation"] == "start db"

    def test_filenotfound_becomes_not_found(self):
        @tool
        def f():
            raise FileNotFoundError("missing: /a.db")

        with pytest.raises(NotFound) as ei:
            f()
        assert "/a.db" in ei.value.message

    def test_unexpected_becomes_internal(self):
        @tool
        def f():
            raise ValueError("nope")

        with pytest.raises(Internal) as ei:
            f()
        assert ei.value.code == "internal"
