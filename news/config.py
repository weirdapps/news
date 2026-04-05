"""Configuration file loader for news reader."""

from pathlib import Path
import yaml


# Project root and config directory paths
_PROJECT_ROOT = Path(__file__).parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"


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
        return yaml.safe_load(f)


def get_sources(path: Path | None = None) -> dict:
    """Load source configuration (RSS feeds, NewsAPI keywords, web search queries).

    Args:
        path: Optional path to sources.yaml. Defaults to config/sources.yaml

    Returns:
        Dictionary with keys: rss_feeds, newsapi_keywords, websearch_queries
    """
    if path is None:
        path = _CONFIG_DIR / "sources.yaml"
    return load_config(path)


def get_categories(path: Path | None = None) -> dict:
    """Load category definitions and display order.

    Args:
        path: Optional path to categories.yaml. Defaults to config/categories.yaml

    Returns:
        Dictionary with category definitions and display_order list
    """
    if path is None:
        path = _CONFIG_DIR / "categories.yaml"
    return load_config(path)


def get_settings(path: Path | None = None) -> dict:
    """Load pipeline settings, email config, schedule, etc.

    Args:
        path: Optional path to settings.yaml. Defaults to config/settings.yaml

    Returns:
        Dictionary with sections: pipeline, email, schedule, storage, synthesis, etc.
    """
    if path is None:
        path = _CONFIG_DIR / "settings.yaml"
    return load_config(path)
