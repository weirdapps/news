"""Configuration file loader for news reader with profile support.

Profiles allow different pipeline configurations (e.g., 'digest' for broad news,
'monitor' for NBG brand monitoring). Each profile has its own config directory:
- digest (default): loads from config/
- monitor: loads from config/monitor/

YAML values support shell-style env var interpolation: ${VAR} and ${VAR:-default}.
A `.env` file at the project root (gitignored) is loaded into os.environ at import
time. Real environment variables take precedence over .env values.
"""

import os
import re
from pathlib import Path
import yaml


# Project root and config directory paths
_PROJECT_ROOT = Path(__file__).parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"

VALID_PROFILES = ("digest", "monitor")

# Matches ${VAR} or ${VAR:-default}
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")


def _load_dotenv(path: Path) -> None:
    """Populate os.environ from a .env file. Real env vars are NOT overridden."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _expand_env(value):
    """Recursively expand ${VAR} / ${VAR:-default} in strings, dicts, and lists."""
    if isinstance(value, str):
        return _ENV_VAR_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else ""),
            value,
        )
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    return value


# Load .env once at import (no-op if file missing).
_load_dotenv(_PROJECT_ROOT / ".env")


def _profile_config_dir(profile: str = "digest") -> Path:
    """Return the config directory for a given profile.

    Args:
        profile: Profile name ('digest' or 'monitor')

    Returns:
        Path to the profile's config directory
    """
    if profile not in VALID_PROFILES:
        raise ValueError(f"Unknown profile '{profile}'. Valid: {VALID_PROFILES}")
    if profile == "digest":
        return _CONFIG_DIR
    return _CONFIG_DIR / profile


def load_config(path: Path) -> dict:
    """Load a YAML configuration file.

    Args:
        path: Path to the YAML file to load

    Returns:
        Dictionary containing the parsed YAML content

    Raises:
        FileNotFoundError: If the config file doesn't exist
        yaml.YAMLError: If the file contains invalid YAML
    """
    with open(path, "r", encoding="utf-8") as f:
        return _expand_env(yaml.safe_load(f))


def get_sources(path: Path | None = None, profile: str = "digest") -> dict:
    """Load source configuration (RSS feeds, NewsAPI keywords, web search queries).

    Args:
        path: Optional explicit path to sources.yaml
        profile: Profile name ('digest' or 'monitor')

    Returns:
        Dictionary with source definitions
    """
    if path is None:
        path = _profile_config_dir(profile) / "sources.yaml"
    return load_config(path)


def get_categories(path: Path | None = None, profile: str = "digest") -> dict:
    """Load category definitions and display order.

    Args:
        path: Optional explicit path to categories.yaml
        profile: Profile name ('digest' or 'monitor')

    Returns:
        Dictionary with category definitions and display_order list
    """
    if path is None:
        path = _profile_config_dir(profile) / "categories.yaml"
    return load_config(path)


def get_settings(path: Path | None = None, profile: str = "digest") -> dict:
    """Load pipeline settings, email config, schedule, etc.

    Args:
        path: Optional explicit path to settings.yaml
        profile: Profile name ('digest' or 'monitor')

    Returns:
        Dictionary with sections: pipeline, email, schedule, storage, synthesis, etc.
    """
    if path is None:
        path = _profile_config_dir(profile) / "settings.yaml"
    return load_config(path)


def get_keywords(path: Path | None = None, profile: str = "monitor") -> dict:
    """Load keyword definitions for entity monitoring.

    Only used by the monitor profile. Contains NBG name variants,
    competitor names, key people, etc.

    Args:
        path: Optional explicit path to keywords.yaml
        profile: Profile name

    Returns:
        Dictionary with keyword definitions
    """
    if path is None:
        path = _profile_config_dir(profile) / "keywords.yaml"
    return load_config(path)
