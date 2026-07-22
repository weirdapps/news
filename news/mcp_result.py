"""Canonical error model for FastMCP MCP tools (trading-data / news-reader).

Every tool failure surfaces as a structured, machine-readable error with the
same shape across the MCP fleet, so callers never hit the silent "tool returned
success but never ran" / empty-vs-failed ambiguity.

Wire behaviour: a decorated tool raises on failure. FastMCP converts the
exception into a CallToolResult with isError=True whose text is

    "Error executing tool <name>: <str(exc)>"

Because each MCPError's str() is the canonical JSON payload, the client receives
isError=True plus a parseable payload:

    {"error": "<code>", "message": "...", "retryable": <bool>,
     "remediation": "..."?, "details": {...}?}

(The "Error executing tool <name>: " prefix precedes the JSON; parse from the
first "{".) Success returns pass through unchanged (non-breaking).

Stdlib only — no third-party imports — so this file is safe to copy verbatim
into any Python MCP server and to import from standalone scripts. This copy is
kept byte-identical with the trading-data copy; edit both together (or promote
to a shared package once a third Python MCP server needs it).
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from typing import Any

# Closed set of error codes, aligned with the outlook-bridge MCP contract.
CODES: tuple[str, ...] = (
    "invalid_input",
    "not_found",
    "auth_required",
    "upstream",
    "unavailable",
    "internal",
)


class MCPError(Exception):
    """A structured tool error whose ``str()`` is the canonical JSON payload.

    Subclasses set ``code``. ``retryable`` defaults per-subclass via
    ``default_retryable`` (e.g. ``Upstream`` is retryable); pass
    ``retryable=`` to override for a specific failure.
    """

    code: str = "internal"
    default_retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        retryable: bool | None = None,
        remediation: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = str(message)
        self.retryable = self.default_retryable if retryable is None else retryable
        self.remediation = remediation
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.remediation:
            payload["remediation"] = self.remediation
        if self.details:
            payload["details"] = self.details
        return payload

    def __str__(self) -> str:
        return json.dumps(self.to_dict())


class InvalidInput(MCPError):
    """Caller passed bad arguments."""

    code = "invalid_input"


class NotFound(MCPError):
    """A required resource (file, record) does not exist."""

    code = "not_found"


class AuthRequired(MCPError):
    """Missing or expired credentials; caller must (re-)authenticate."""

    code = "auth_required"


class Upstream(MCPError):
    """An external dependency (API, network) failed. Retryable by default."""

    code = "upstream"
    default_retryable = True


class Unavailable(MCPError):
    """A local dependency (database, library) is missing or unusable."""

    code = "unavailable"


class Internal(MCPError):
    """An unexpected, uncategorised failure."""

    code = "internal"


def _coerce(exc: Exception) -> MCPError:
    """Map an arbitrary exception onto the canonical error model."""
    if isinstance(exc, MCPError):
        return exc
    if isinstance(exc, FileNotFoundError):
        return NotFound(str(exc))
    return Internal(f"{type(exc).__name__}: {exc}")


def tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a FastMCP tool so every failure emits the canonical error shape.

    Apply directly under ``@mcp.tool()``::

        @mcp.tool()
        @tool
        def my_tool(...) -> dict:
            ...

    An ``MCPError`` raised inside the tool propagates unchanged; any other
    exception is coerced (``FileNotFoundError`` -> ``NotFound``, everything else
    -> ``Internal``) so nothing ever leaves a tool unstructured. FastMCP then
    turns the raised error into an ``isError`` response (see module docstring).
    ``functools.wraps`` preserves name/docstring/signature so FastMCP's schema
    and tool registration are unchanged.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except MCPError:
            raise
        except Exception as exc:
            raise _coerce(exc) from exc

    return wrapper
