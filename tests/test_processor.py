from datetime import UTC, datetime, timedelta

from news.models import Article
from news.processor import (
    classify_article,
    compute_relevance_score,
    deduplicate,
    filter_quality,
    process_articles,
)


def _make_article(
    title="Test",
    content="Long enough content. " * 50,  # 150 words total
    url="https://example.com/1",
    source="Src",
    categories=None,
    language="en",
    published_at=None,
) -> Article:
    """Helper to create test articles."""
    if categories is None:
        categories = []
    if published_at is None:
        published_at = datetime.now(UTC)
    return Article(
        url=url,
        title=title,
        source=source,
        content=content,
        categories=categories,
        language=language,
        published_at=published_at,
    )


def test_deduplicate_removes_exact_duplicates():
    """Test that exact duplicate articles are detected and separated."""
    # Create 3 articles: 2 with same title+content, 1 different
    article1 = _make_article(
        title="Same Title", content="Same content. " * 20, url="https://example.com/1"
    )
    article2 = _make_article(
        title="Same Title", content="Same content. " * 20, url="https://example.com/2"
    )
    article3 = _make_article(
        title="Different",
        content="Different content. " * 20,
        url="https://example.com/3",
    )

    # Compute hashes
    article1.compute_hash()
    article2.compute_hash()
    article3.compute_hash()

    articles = [article1, article2, article3]
    unique, dupes = deduplicate(articles, set())

    assert len(unique) == 2  # article1 and article3
    assert len(dupes) == 1  # article2
    assert dupes[0].url == "https://example.com/2"
    # Check that also_reported_by was updated on the original
    assert (
        "https://example.com/2" in unique[0].also_reported_by or "Src" in unique[0].also_reported_by
    )


def test_deduplicate_respects_existing_hashes():
    """Test that articles whose hash exists in existing_hashes are filtered out."""
    article1 = _make_article(
        title="Test", content="Test content. " * 20, url="https://example.com/1"
    )
    article1.compute_hash()

    # Hash already exists
    existing_hashes = {article1.content_hash}

    unique, dupes = deduplicate([article1], existing_hashes)

    assert len(unique) == 0
    assert len(dupes) == 1
    assert dupes[0].url == "https://example.com/1"


def test_classify_article_adds_categories():
    """Test that article gets classified into categories based on keyword matching."""
    article = _make_article(
        title="Claude AI Agent Used by NBG",
        content="National Bank of Greece announces use of AI agents for customer service. " * 10,
    )

    categories_config = {
        "categories": {
            "ai": {
                "display_name": "Artificial Intelligence",
                "keywords": ["ai", "artificial intelligence", "claude", "agent", "llm"],
                "priority": 1,
            },
            "banking": {
                "display_name": "Banking",
                "keywords": ["bank", "national bank of greece", "nbg", "finance"],
                "priority": 2,
            },
        }
    }

    classify_article(article, categories_config)

    assert "ai" in article.categories
    assert "banking" in article.categories


def test_compute_relevance_score():
    """Test relevance scoring with company mention, category, tier, and recency."""
    # Article mentioning the configured company name, in banking category,
    # tier 1, published 2h ago.
    two_hours_ago = datetime.now(UTC) - timedelta(hours=2)
    article = _make_article(
        title="NBG Quarterly Results",
        content="National Bank of Greece reports strong quarterly results. " * 20,
        categories=["banking"],
        published_at=two_hours_ago,
    )

    scoring_config = {
        "company_mention": 30,
        "greek_banking": 20,
        "category_match": 10,
        "tier_1_bonus": 15,
        "tier_2_bonus": 10,
        "tier_3_bonus": 5,
        "recency_1h": 20,
        "recency_4h": 15,
        "recency_12h": 10,
        "recency_24h": 5,
    }
    keywords = {"company": {"names": ["NBG", "National Bank of Greece"]}}

    score = compute_relevance_score(
        article, scoring_config, source_tier=1, keywords_config=keywords
    )

    # Expected: company_mention(30) + category_match(10) + tier_1_bonus(15) + recency_4h(15) = 70
    assert score >= 70
    assert article.relevance_score >= 70


def test_compute_relevance_score_uses_keywords_config_for_company_match():
    """Company-mention bonus is awarded based on keywords_config patterns, not a hardcoded list."""
    article = _make_article(
        title="AcmeCorp Q1 results",
        content="AcmeCorp posted strong results. " * 20,
    )
    scoring = {
        "company_mention": 50,
        "category_match": 0,
        "tier_1_bonus": 0,
        "tier_2_bonus": 0,
        "tier_3_bonus": 0,
        "recency_1h": 0,
        "recency_4h": 0,
        "recency_12h": 0,
        "recency_24h": 0,
        "claude_mention": 0,
    }
    keywords = {"company": {"names": ["AcmeCorp"]}}

    score = compute_relevance_score(article, scoring, source_tier=2, keywords_config=keywords)
    assert score >= 50


def test_compute_relevance_score_no_keywords_config_skips_company_bonus():
    """Without keywords_config (digest profile), the company_mention bonus does not apply."""
    article = _make_article(
        title="National Bank of Greece Q1",
        content="NBG news. " * 20,
    )
    scoring = {
        "company_mention": 50,
        "category_match": 0,
        "tier_1_bonus": 0,
        "tier_2_bonus": 0,
        "tier_3_bonus": 0,
        "recency_1h": 0,
        "recency_4h": 0,
        "recency_12h": 0,
        "recency_24h": 0,
        "claude_mention": 0,
        "greek_banking": 0,
    }

    score = compute_relevance_score(article, scoring, source_tier=2)  # no keywords_config
    assert score == 0


def test_compute_relevance_score_uses_competitors_for_greek_banking_bonus():
    """Sector bonus comes from keywords_config.competitors, not hardcoded names."""
    article = _make_article(
        title="XYZ Bank reports strong results",
        content="XYZ Bank had a great quarter. " * 20,
    )
    scoring = {
        "company_mention": 0,
        "greek_banking": 30,
        "category_match": 0,
        "tier_1_bonus": 0,
        "tier_2_bonus": 0,
        "tier_3_bonus": 0,
        "recency_1h": 0,
        "recency_4h": 0,
        "recency_12h": 0,
        "recency_24h": 0,
        "claude_mention": 0,
    }
    keywords = {"competitors": {"xyz": {"names": ["XYZ Bank"]}}}

    score = compute_relevance_score(article, scoring, source_tier=2, keywords_config=keywords)
    assert score >= 30


def test_processor_module_has_no_brand_specific_literals():
    """processor.py source contains no brand-specific company or competitor literals."""
    import news.processor as proc_mod

    src = open(proc_mod.__file__).read()
    forbidden = [
        "national bank of greece",
        "nbg",
        "ethniki trapeza",
        "piraeus",
        "alpha bank",
        "eurobank",
        "hellenic bank",
        "greek bank",  # phrase — was hardcoded in the old greek_banking_patterns
    ]
    for f in forbidden:
        assert f.lower() not in src.lower(), f"Found brand-specific literal in processor.py: {f}"


def test_filter_quality_drops_short_articles():
    """Test that short articles are dropped and long ones kept."""
    short = _make_article(content="Too short.")
    long_content = "Long enough content. " * 50  # 150 words
    long = _make_article(content=long_content)

    articles = [short, long]
    kept, dropped = filter_quality(articles, min_words=100, max_age_hours=36)

    assert len(kept) == 1
    assert kept[0].content == long_content
    assert len(dropped) == 1
    assert dropped[0].content == "Too short."


def test_filter_quality_drops_old_articles():
    """Test that old articles are dropped and recent ones kept."""
    forty_eight_hours_ago = datetime.now(UTC) - timedelta(hours=48)
    two_hours_ago = datetime.now(UTC) - timedelta(hours=2)

    old = _make_article(published_at=forty_eight_hours_ago)
    recent = _make_article(published_at=two_hours_ago)

    articles = [old, recent]
    kept, dropped = filter_quality(articles, min_words=100, max_age_hours=36)

    assert len(kept) == 1
    assert kept[0].published_at == two_hours_ago
    assert len(dropped) == 1
    assert dropped[0].published_at == forty_eight_hours_ago


# --- Per-source age override --------------------------------------------------
# Curated, slow-publishing sources (evergreen deep dives) would be wiped out by
# the news-wire age window, so a source may declare a longer one of its own.


def test_filter_quality_keeps_old_articles_from_a_source_with_an_age_override():
    sixty_hours_ago = datetime.now(UTC) - timedelta(hours=60)
    evergreen = _make_article(source="The Agent Daily", published_at=sixty_hours_ago)

    kept, dropped = filter_quality(
        [evergreen],
        min_words=100,
        max_age_hours=36,
        source_max_age={"The Agent Daily": 720},
    )

    assert kept == [evergreen]
    assert dropped == []


def test_filter_quality_honours_a_source_word_floor_override():
    """An exchange filing's body is boilerplate ("please see the attached"); its
    substance is the title and the PDF, so the feed declares a lower word floor."""
    filing = _make_article(source="Exchange Filings", content="please see the attachment")
    wire = _make_article(source="Wire", content="please see the attachment")

    kept, dropped = filter_quality(
        [filing, wire],
        min_words=10,
        max_age_hours=36,
        source_min_words={"Exchange Filings": 0},
    )

    assert kept == [filing]
    assert dropped == [wire]


def test_filter_quality_still_drops_old_articles_from_sources_without_an_override():
    sixty_hours_ago = datetime.now(UTC) - timedelta(hours=60)
    wire = _make_article(source="TechCrunch", published_at=sixty_hours_ago)
    evergreen = _make_article(source="The Agent Daily", published_at=sixty_hours_ago)

    kept, dropped = filter_quality(
        [wire, evergreen],
        min_words=100,
        max_age_hours=36,
        source_max_age={"The Agent Daily": 720},
    )

    assert kept == [evergreen]
    assert dropped == [wire]


def test_filter_quality_applies_the_override_ceiling_not_an_exemption():
    """An override is a longer window, not a licence to keep anything forever."""
    ancient = _make_article(
        source="The Agent Daily",
        published_at=datetime.now(UTC) - timedelta(hours=800),
    )

    kept, dropped = filter_quality(
        [ancient],
        min_words=100,
        max_age_hours=36,
        source_max_age={"The Agent Daily": 720},
    )

    assert kept == []
    assert dropped == [ancient]


def test_process_articles_threads_the_source_age_override_through():
    sixty_hours_ago = datetime.now(UTC) - timedelta(hours=60)
    evergreen = _make_article(source="The Agent Daily", published_at=sixty_hours_ago)

    processed, stats = process_articles(
        articles=[evergreen],
        existing_hashes=set(),
        categories_config={"categories": {}},
        scoring_config={},
        source_tiers={},
        min_words=10,
        max_age_hours=36,
        source_max_age={"The Agent Daily": 720},
    )

    assert stats["quality_dropped"] == 0
    assert len(processed) == 1


def test_relevance_score_counts_a_claude_mention_found_only_in_the_abstract():
    """The description is marketing copy; the substance is in the transcript."""
    article = _make_article(content="Subscribe for more videos every week!")
    article.transcript_abstract = (
        "A walkthrough of building an MCP server and wiring it into Claude Code."
    )

    score = compute_relevance_score(article, {"claude_mention": 30}, source_tier=2)

    assert score == 30


def test_filter_quality_counts_the_abstract_toward_the_word_gate():
    """A video with a terse description but a rich abstract must survive.

    filter_quality runs before scoring, so dropping here means the abstract is
    never seen at all -- the enrichment is computed and then discarded.
    """
    terse = _make_article(content="New video out now")
    terse.transcript_abstract = (
        "Meta released Muse Glimmer, a 30-billion-parameter agentic model under the "
        "Apache 2.0 licence, with 4-bit quantisation bringing it under 20 GB."
    )

    kept, dropped = filter_quality([terse], min_words=10, max_age_hours=36)

    assert kept == [terse]
    assert dropped == []


# --- Brand-name matching: case, accents, final sigma, whole words, HTML ---

_BRAND_ONLY_SCORING = {
    "company_mention": 80,
    "greek_banking": 40,
    "regulatory_mention": 35,
    "category_match": 0,
    "tier_1_bonus": 0,
    "tier_2_bonus": 0,
    "tier_3_bonus": 0,
    "recency_1h": 0,
    "recency_4h": 0,
    "recency_12h": 0,
    "recency_24h": 0,
    "claude_mention": 0,
}


def _brand_score(title, keywords, content=""):
    article = _make_article(title=title, content=content)
    return compute_relevance_score(article, _BRAND_ONLY_SCORING, keywords_config=keywords)


def test_company_match_ignores_case_and_accents_of_an_all_caps_greek_headline():
    """Greek headlines are often ALL CAPS without accents; the name must still match."""
    keywords = {"company": {"names": ["Ακμή Τράπεζα"]}}
    assert _brand_score("ΑΚΜΗ ΤΡΑΠΕΖΑ: ΝΕΟ ΠΡΟΓΡΑΜΜΑ", keywords) == 80


def test_company_match_folds_final_sigma():
    """A genitive ending in ς must match the same word typed in capitals (Σ)."""
    keywords = {"company": {"names": ["Ακμής Τράπεζας"]}}
    assert _brand_score("ΚΕΡΔΗ ΤΗΣ ΑΚΜΗΣ ΤΡΑΠΕΖΑΣ", keywords) == 80


def test_short_greek_acronym_does_not_match_inside_a_word():
    """A three-letter acronym must not fire on every word that happens to contain it."""
    keywords = {"company": {"names": ["ΤΙΚ"]}}
    assert _brand_score("Η πολιτική της κυβέρνησης για την ενέργεια", keywords) == 0
    assert _brand_score("Η ΤΙΚ ανακοίνωσε αποτελέσματα", keywords) == 80


def test_competitor_name_does_not_match_a_longer_word():
    """'Prima' is a competitor; 'primarily' is not a mention of it."""
    keywords = {"competitors": {"prima": {"names": ["Prima"]}}}
    assert _brand_score("Markets were primarily driven by rates", keywords) == 0
    assert _brand_score("Prima reports record deposits", keywords) == 40


def test_multi_word_name_matches_across_non_breaking_spaces():
    keywords = {"company": {"names": ["Acme Bank"]}}
    assert _brand_score("Acme\xa0Bank opens a new branch", keywords) == 80


def test_name_inside_html_attributes_does_not_count_as_a_mention():
    """Feed bodies are HTML; a name buried in a link URL is not a mention."""
    keywords = {"company": {"names": ["ACM"]}}
    content = (
        '<a href="https://news.example.com/rss/articles/CBMi-acm-Qx9" target="_blank">'
        'Weather turns cold</a>&nbsp;&nbsp;<font color="#6f6f6f">Outlet</font>'
    )
    assert _brand_score("Weather turns cold", keywords, content=content) == 0


def test_group_entity_mention_counts_as_company_mention():
    """company.entities belong to the group: naming one earns the company bonus."""
    keywords = {
        "company": {
            "names": ["AcmeCorp"],
            "entities": {"acme_am": {"names": ["Acme Asset Management", "Ακμή ΑΕΔΑΚ"]}},
        }
    }
    assert _brand_score("Acme Asset Management launches a bond fund", keywords) == 80
    assert _brand_score("ΑΚΜΗ ΑΕΔΑΚ: νέο αμοιβαίο", keywords) == 80


def test_latin_brand_typed_with_greek_lookalike_capitals_still_matches():
    """The Greek press types Latin brands on a Greek keyboard: Greek Ν and Β in «ΝΒX»."""
    keywords = {"company": {"names": ["AcmeCorp"], "entities": {"p": {"names": ["NBX Pay"]}}}}
    assert _brand_score("Η ΝΒX Pay φέρνει νέες πληρωμές", keywords) == 80


def test_greek_word_in_capitals_is_not_turned_into_latin():
    """Only mixed-script words are normalised; an all-Greek capitalised word still matches."""
    keywords = {"company": {"names": ["Ακμή Τράπεζα"]}}
    assert _brand_score("ΑΚΜΗ ΤΡΑΠΕΖΑ ΚΑΙ ΚΕΡΔΗ", keywords) == 80


def test_false_positive_phrase_does_not_earn_the_company_bonus():
    """«Acme Bank of Ruritania» is someone else's bank; the configured phrase is excluded."""
    keywords = {
        "company": {
            "names": ["Acme Bank"],
            "false_positives": ["Acme Bank of Ruritania"],
        }
    }
    assert _brand_score("Acme Bank of Ruritania cuts rates", keywords) == 0
    assert _brand_score("Acme Bank of Ruritania cuts rates; Acme Bank follows", keywords) == 80


def test_entity_mention_uses_its_own_weight_when_configured():
    """A broker note quoting the group's securities arm is not the company's own news."""
    keywords = {
        "company": {"names": ["AcmeCorp"], "entities": {"sec": {"names": ["Acme Securities"]}}}
    }
    scoring = {**_BRAND_ONLY_SCORING, "entity_mention": 50}
    article = _make_article(title="Widgets Inc: 28% upside, says Acme Securities")
    assert compute_relevance_score(article, scoring, keywords_config=keywords) == 50
    both = _make_article(title="AcmeCorp unit Acme Securities hires analysts")
    assert compute_relevance_score(both, scoring, keywords_config=keywords) == 80


def test_company_name_inside_an_entity_name_counts_only_as_the_entity():
    """A short company name ("Acme") must not fire inside "Acme Securities"."""
    keywords = {"company": {"names": ["Acme"], "entities": {"sec": {"names": ["Acme Securities"]}}}}
    scoring = {**_BRAND_ONLY_SCORING, "entity_mention": 50}
    note = _make_article(title="Widgets Inc: 28% upside, says Acme Securities")
    assert compute_relevance_score(note, scoring, keywords_config=keywords) == 50
    own = _make_article(title="Acme results beat; Acme Securities upgrades peers")
    assert compute_relevance_score(own, scoring, keywords_config=keywords) == 80


def test_google_news_outlet_suffix_is_not_a_mention():
    """Google News appends ' - Outlet' and repeats the outlet in the body. An outlet
    named after a tracked brand (a sponsor's sports team, the brand's own site) is
    not a mention of it."""
    keywords = {"competitors": {"prima": {"names": ["Prima"]}}}

    def gn(title):
        return _make_article(
            title=title,
            url="https://news.google.com/rss/articles/CBMi-x",
            content=f'<a href="https://news.google.com/rss/articles/CBMi-x">{title}</a>'
            f'&nbsp;&nbsp;<font color="#6f6f6f">{title.rsplit(" - ", 1)[-1]}</font>',
        )

    sponsored = gn("A strong fight to P11 in Baku - Prima Racing Team")
    assert compute_relevance_score(sponsored, _BRAND_ONLY_SCORING, keywords_config=keywords) == 0
    real = gn("Prima raises its guidance - Business Daily")
    assert compute_relevance_score(real, _BRAND_ONLY_SCORING, keywords_config=keywords) == 40


def test_regulator_mention_earns_the_regulatory_bonus():
    """The regulators list feeds scoring.regulatory_mention; it used to be read by nothing."""
    keywords = {"regulators": ["Central Bank of Ruritania"]}
    assert _brand_score("Central Bank of Ruritania raises capital buffers", keywords) == 35


def test_competitor_false_positive_phrase_is_not_a_mention():
    """A software product sharing a competitor's name ("Linedata Prima") is not the
    competitor. competitors.<key>.false_positives are cut out before matching."""
    keywords = {
        "competitors": {"prima": {"names": ["Prima"], "false_positives": ["Linedata Prima"]}}
    }
    assert _brand_score("Insurer deploys Linedata Prima for fund accounting", keywords) == 0
    assert _brand_score("Prima raises its guidance", keywords) == 40
    assert _brand_score("Linedata Prima rollout ends; Prima raises its guidance", keywords) == 40


def test_sector_term_earns_the_sector_bonus():
    keywords = {"sector_terms": ["Ruritanian banks"]}
    scoring = {**_BRAND_ONLY_SCORING, "sector_mention": 20}
    article = _make_article(title="Ruritanian banks post record profits")
    assert compute_relevance_score(article, scoring, keywords_config=keywords) == 20


def test_generic_sector_story_opens_the_require_mention_gate():
    """A story about the country's banks as a group names no single bank."""
    keywords = {"company": {"names": ["AcmeCorp"]}, "sector_terms": ["Ruritanian banks"]}
    sector = _make_article(
        title="Ruritanian banks post record profits",
        url="https://outlet.example/s",
        source="Outlet Economy",
    )
    miss = _make_article(
        title="Tomato prices rise", url="https://outlet.example/t", source="Outlet Economy"
    )

    processed, _ = process_articles(
        [sector, miss],
        existing_hashes=set(),
        categories_config={},
        scoring_config={},
        source_tiers={},
        min_words=1,
        keywords_config=keywords,
        require_mention_sources={"Outlet Economy"},
    )

    assert [a.url for a in processed] == ["https://outlet.example/s"]


def test_competitor_bonus_reads_competitor_mention():
    """competitor_mention is the key; greek_banking is read only as a legacy fallback."""
    keywords = {"competitors": {"xyz": {"names": ["XYZ Bank"]}}}
    article = _make_article(title="XYZ Bank cuts fees")
    scoring = {"competitor_mention": 40, "greek_banking": 5}
    assert compute_relevance_score(article, scoring, keywords_config=keywords) == 40


def test_company_competitor_and_regulator_bonuses_add_up():
    keywords = {
        "company": {"names": ["AcmeCorp"]},
        "competitors": {"xyz": {"names": ["XYZ Bank"]}},
        "regulators": ["Central Bank of Ruritania"],
    }
    title = "Central Bank of Ruritania fines XYZ Bank; AcmeCorp unaffected"
    assert _brand_score(title, keywords) == 80 + 40 + 35


# --- require_mention: broad publisher feeds keep only what names a tracked entity ---


def test_sources_requiring_mention_collects_flagged_sources_of_every_kind():
    from news.processor import sources_requiring_mention

    sources = {
        "rss_feeds": [
            {"name": "Outlet Economy", "require_mention": True},
            {"name": "Acme Query"},
        ],
        "html_sources": [{"name": "Outlet Listing", "require_mention": True}],
    }
    assert sources_requiring_mention(sources) == {"Outlet Economy", "Outlet Listing"}


def test_process_articles_drops_unmentioned_articles_only_from_gated_sources():
    keywords = {
        "company": {"names": ["AcmeCorp"]},
        "competitors": {"xyz": {"names": ["XYZ Bank"]}},
    }
    broad_hit = _make_article(
        title="AcmeCorp expands lending", url="https://outlet.example/1", source="Outlet Economy"
    )
    broad_competitor = _make_article(
        title="XYZ Bank cuts fees", url="https://outlet.example/2", source="Outlet Economy"
    )
    broad_miss = _make_article(
        title="Tomato prices rise", url="https://outlet.example/3", source="Outlet Economy"
    )
    broad_regulator_only = _make_article(
        title="Tourism arrivals up, the Central Bank of Ruritania says",
        url="https://outlet.example/4",
        source="Outlet Economy",
    )
    query_miss = _make_article(
        title="Banking sector outlook", url="https://query.example/1", source="Acme Query"
    )
    keywords["regulators"] = ["Central Bank of Ruritania"]

    processed, stats = process_articles(
        [broad_hit, broad_competitor, broad_miss, broad_regulator_only, query_miss],
        existing_hashes=set(),
        categories_config={},
        scoring_config={},
        source_tiers={},
        min_words=1,
        keywords_config=keywords,
        require_mention_sources={"Outlet Economy"},
    )

    assert {a.url for a in processed} == {
        "https://outlet.example/1",
        "https://outlet.example/2",
        "https://query.example/1",
    }, "a passing regulator mention does not open the gate; regulators have their own feeds"
    assert stats["unmentioned_dropped"] == 2
    assert stats["output_count"] == 3


# --- collapse_same_headlines: one prompt slot per story ---


def test_collapse_same_headlines_prefers_the_publisher_copy_at_the_groups_best_score():
    """A Google News copy has no body and a redirect URL; the publisher's own copy has
    both, so it survives, carrying the best score any copy earned."""
    from news.processor import collapse_same_headlines

    direct = _make_article(
        title="Acme opens 20 branches", url="https://outlet.example/acme", source="Outlet Feed"
    )
    direct.relevance_score = 50
    via_google = _make_article(
        title="Acme opens 20 branches - Outlet",
        url="https://news.google.com/rss/articles/CBMi1",
        source="Acme Query",
    )
    via_google.relevance_score = 90
    reprint = _make_article(
        title="ACME OPENS 20 BRANCHES", url="https://other.example/x", source="Other Feed"
    )
    reprint.relevance_score = 10
    different = _make_article(
        title="Acme closes 3 branches", url="https://outlet.example/other", source="Outlet Feed"
    )

    kept = collapse_same_headlines([direct, via_google, reprint, different])

    assert [a.url for a in kept] == ["https://outlet.example/acme", "https://outlet.example/other"]
    assert kept[0].relevance_score == 90
    assert sorted(kept[0].also_reported_by) == ["Acme Query", "Other Feed"]


def test_collapse_same_headlines_strips_the_outlet_suffix_only_from_google_news_titles():
    """A publisher's own headline may contain ' - '; only Google News appends an outlet."""
    from news.processor import collapse_same_headlines

    a = _make_article(title="Acme - results beat", url="https://outlet.example/1", source="A")
    b = _make_article(title="Acme", url="https://outlet.example/2", source="B")

    assert len(collapse_same_headlines([a, b])) == 2
