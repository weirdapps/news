#!/bin/bash
# MCP server launcher. Resolves to the script's own directory so the
# repo can be cloned anywhere. Override venv with NEWS_VENV_PYTHON
# (default: ./venv/bin/python relative to repo root).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
exec "${NEWS_VENV_PYTHON:-$SCRIPT_DIR/venv/bin/python}" -m news.mcp_server
