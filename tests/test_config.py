"""Tests for configuration file loading."""

from news.config import load_config, get_sources, get_categories, get_settings


def test_load_config_reads_yaml(tmp_path):
    """Test that load_config reads YAML files correctly."""
    # Write a test YAML file
    test_file = tmp_path / "test.yaml"
    test_file.write_text("""
foo: bar
items:
  - one
  - two
  - three
nested:
  key1: value1
  key2: 42
""")

    # Load and verify
    result = load_config(test_file)
    assert result["foo"] == "bar"
    assert result["items"] == ["one", "two", "three"]
    assert result["nested"]["key1"] == "value1"
    assert result["nested"]["key2"] == 42


def test_get_sources_returns_feed_list(tmp_path):
    """Test that get_sources returns properly structured feed lists."""
    # Create sources.yaml
    sources_file = tmp_path / "sources.yaml"
    sources_file.write_text("""
rss_feeds:
  - url: https://example.com/feed.xml
    category: business
    tier: 1
  - url: https://example.com/tech.xml
    category: tech
    tier: 2

newsapi_keywords:
  - keyword: "artificial intelligence"
    category: ai
  - keyword: "banking"
    category: banking

websearch_queries:
  - query: "Greek banking news"
    category: greece
  - query: "Apple products"
    category: apple
""")

    result = get_sources(sources_file)

    # Verify structure
    assert "rss_feeds" in result
    assert "newsapi_keywords" in result
    assert "websearch_queries" in result

    # Verify RSS feeds
    assert len(result["rss_feeds"]) == 2
    assert result["rss_feeds"][0]["url"] == "https://example.com/feed.xml"
    assert result["rss_feeds"][0]["category"] == "business"
    assert result["rss_feeds"][0]["tier"] == 1

    # Verify NewsAPI keywords
    assert len(result["newsapi_keywords"]) == 2
    assert result["newsapi_keywords"][0]["keyword"] == "artificial intelligence"

    # Verify web search queries
    assert len(result["websearch_queries"]) == 2
    assert result["websearch_queries"][0]["query"] == "Greek banking news"


def test_get_categories_returns_category_definitions(tmp_path):
    """Test that get_categories returns category definitions."""
    # Create categories.yaml
    categories_file = tmp_path / "categories.yaml"
    categories_file.write_text("""
categories:
  business:
    display_name: "Business & Economy"
    keywords:
      - business
      - economy
      - markets
    priority: 1

  tech:
    display_name: "Technology"
    keywords:
      - technology
      - software
      - hardware
    priority: 2

display_order:
  - business
  - tech
""")

    result = get_categories(categories_file)

    # Verify structure
    assert "categories" in result
    assert "business" in result["categories"]
    assert "tech" in result["categories"]
    assert "display_order" in result

    # Verify business category
    assert result["categories"]["business"]["display_name"] == "Business & Economy"
    assert "business" in result["categories"]["business"]["keywords"]
    assert result["categories"]["business"]["priority"] == 1

    # Verify display order
    assert result["display_order"] == ["business", "tech"]


def test_get_settings_returns_defaults(tmp_path):
    """Test that get_settings returns default settings."""
    # Create settings.yaml
    settings_file = tmp_path / "settings.yaml"
    settings_file.write_text("""
pipeline:
  relevance_threshold: 20
  max_articles_per_category: 15

email:
  recipient: plessas@nbg.gr
  gmail_script: "/path/to/script.scpt"

schedule:
  timezone: "Europe/Athens"
  runs:
    - "09:00"
    - "13:00"
""")

    result = get_settings(settings_file)

    # Verify structure
    assert "pipeline" in result
    assert "email" in result
    assert "schedule" in result

    # Verify pipeline settings
    assert result["pipeline"]["relevance_threshold"] == 20
    assert result["pipeline"]["max_articles_per_category"] == 15

    # Verify email settings
    assert result["email"]["recipient"] == "plessas@nbg.gr"

    # Verify schedule settings
    assert result["schedule"]["timezone"] == "Europe/Athens"
    assert "09:00" in result["schedule"]["runs"]
