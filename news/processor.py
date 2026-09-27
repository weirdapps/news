"""Article processing: deduplication, classification, scoring, and quality filtering."""

import html
import re
import unicodedata
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from news.models import Article

_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(value: str) -> str:
    """Plain text of a feed body. RSS descriptions are HTML by spec.

    A Google News item's body is nothing but an anchor wrapping the headline plus
    a font tag naming the outlet, so without this the monitor scored and prompted
    on markup: the link URL, the colour attribute, the entities.
    """
    if not value:
        return ""
    return " ".join(html.unescape(_TAG_RE.sub(" ", value)).split())


# Greek letters that pass for Latin ones. The Greek press types Latin brand names on
# a Greek keyboard often enough to show up in real headlines and in exchange
# announcements: a Latin word with a Greek capital Α, Β or Ν standing in for A, B, N.
_LATIN_LOOKALIKES = str.maketrans(
    {
        "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K",
        "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
        "α": "a", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p",
        "τ": "t", "υ": "u", "χ": "x",
    }
)  # fmt: skip
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")
_GREEK_LETTER_RE = re.compile(r"[\u0370-\u03ff]")
_WORD_RE = re.compile(r"\w+")


def _latinise_mixed_word(match: re.Match[str]) -> str:
    word = match.group(0)
    if _LATIN_LETTER_RE.search(word) and _GREEK_LETTER_RE.search(word):
        return word.translate(_LATIN_LOOKALIKES)
    return word


def fold(text: str) -> str:
    """Case-, accent- and final-sigma-insensitive form of text, for name matching.

    Greek headlines are routinely set in capitals without accents, and a
    lowercased name with accents never matched them. casefold() also maps a final
    sigma to the medial one, so a genitive typed in capitals still matches. A word
    mixing Latin and Greek letters has its Greek look-alikes made Latin; an
    all-Greek word is left alone.
    """
    decomposed = unicodedata.normalize("NFD", text)
    bare = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _WORD_RE.sub(_latinise_mixed_word, bare).casefold()


@lru_cache(maxsize=128)
def _names_pattern(names: tuple[str, ...]) -> re.Pattern[str] | None:
    alternatives = sorted(
        {r"\s+".join(re.escape(word) for word in fold(name).split()) for name in names},
        key=len,
        reverse=True,
    )
    alternatives = [alt for alt in alternatives if alt]
    if not alternatives:
        return None
    # Whole words only. A bare substring test let a three-letter acronym fire on
    # every word containing it, and a short brand fire inside a longer word.
    return re.compile(r"(?<!\w)(?:" + "|".join(alternatives) + r")(?!\w)")


def mentions_any(folded_text: str, names: Iterable[str]) -> bool:
    """True when folded_text (see fold) contains any of names as whole words."""
    pattern = _names_pattern(tuple(n for n in names if isinstance(n, str)))
    return bool(pattern and pattern.search(folded_text))


def _is_google_news(article: Article) -> bool:
    return "news.google.com/" in (article.url or "")


def _headline(article: Article) -> str:
    """The title without the " - Outlet" Google News appends to every headline."""
    title = article.title or ""
    return title.rsplit(" - ", 1)[0] if _is_google_news(article) else title


def brand_mentions(article: Article, keywords_config: dict) -> set[str]:
    """Which tracked groups an article names.

    Returns a subset of "company", "entity" (a name under company.entities),
    "competitor", "regulator" and "sector" (a phrase under sector_terms, for a
    story about the banks as a group). Phrases in company.false_positives are cut out
    before the company and entity names are looked for, so another country's
    «National Bank» does not count as the company. Entity names are cut out
    before the company names are looked for, so a short company name does not
    fire inside an entity's name: a broker note signed by the group's securities
    arm is an entity mention, not the company's own news.

    A Google News item is read by its headline alone: its title ends with the
    outlet's name and its body is the headline plus that name again, and an
    outlet named after a tracked brand (a sponsored racing team, the brand's own
    site) is not a mention of it.
    """
    if _is_google_news(article):
        raw = _headline(article)
    else:
        raw = " ".join((article.title, article.content, article.transcript_abstract))
    text = fold(html_to_text(raw))
    company = keywords_config.get("company") or {}
    own_text = text
    false_positives = _names_pattern(
        tuple(n for n in company.get("false_positives") or [] if isinstance(n, str))
    )
    if false_positives:
        own_text = false_positives.sub(" ", own_text)
    entities = _names_pattern(
        tuple(
            name
            for entity in (company.get("entities") or {}).values()
            for name in entity.get("names") or []
            if isinstance(name, str)
        )
    )
    competitors = keywords_config.get("competitors") or {}
    competitor_names = [name for comp in competitors.values() for name in comp.get("names") or []]
    # A product that shares a competitor's name is not the competitor.
    competitor_false_positives = _names_pattern(
        tuple(
            phrase
            for comp in competitors.values()
            for phrase in comp.get("false_positives") or []
            if isinstance(phrase, str)
        )
    )
    competitor_text = (
        competitor_false_positives.sub(" ", text) if competitor_false_positives else text
    )
    regulator_names = keywords_config.get("regulators") or []

    found: set[str] = set()
    if entities and entities.search(own_text):
        found.add("entity")
        own_text = entities.sub(" ", own_text)
    if mentions_any(own_text, company.get("names") or []):
        found.add("company")
    if mentions_any(competitor_text, competitor_names):
        found.add("competitor")
    if mentions_any(text, regulator_names):
        found.add("regulator")
    if mentions_any(text, keywords_config.get("sector_terms") or []):
        found.add("sector")
    return found


def _headline_key(article: Article) -> str:
    return " ".join(re.findall(r"\w+", fold(_headline(article))))


def collapse_same_headlines(articles: list[Article]) -> list[Article]:
    """One article per headline, in input order.

    The same story reaches the monitor through several feeds: a press release
    reprinted by a dozen outlets, a publisher's own feed beside the Google News
    copy of the same piece. Each copy used to take its own slot in the synthesis
    prompt. A publisher's copy beats a Google News one, which has no body and a
    redirect URL; among equals the best-scored wins. The survivor takes the
    group's best score, and its also_reported_by gains the absorbed sources.
    """
    best: dict[str, Article] = {}
    top_score: dict[str, int] = {}
    for article in articles:
        key = _headline_key(article)
        if not key:
            continue
        top_score[key] = max(top_score.get(key, article.relevance_score), article.relevance_score)
        rank = (not _is_google_news(article), article.relevance_score)
        if key not in best or rank > (not _is_google_news(best[key]), best[key].relevance_score):
            best[key] = article
    for key, winner in best.items():
        winner.relevance_score = top_score[key]
    kept = []
    for article in articles:
        key = _headline_key(article)
        if not key:
            kept.append(article)
            continue
        winner = best[key]
        if winner is article:
            kept.append(article)
        elif article.source != winner.source and article.source not in winner.also_reported_by:
            winner.also_reported_by.append(article.source)
    return kept


# What opens the require_mention gate: the company, an entity, a competitor, or a
# story about the sector as a whole. Regulators do not: a publisher's full text
# cites the central bank in passing all the time, and regulator news has its own
# feeds. A regulator mention still earns its scoring bonus.
_GATE_GROUPS = frozenset({"company", "entity", "competitor", "sector"})


def sources_requiring_mention(sources: dict) -> set[str]:
    """Names of sources that declared `require_mention: true`.

    Such a source is a publisher's whole section feed, not a search: it carries
    everything the outlet prints, so only articles naming the company, one of its
    entities or a competitor are kept from it.
    """
    gated: set[str] = set()
    for key in ("rss_feeds", "html_sources", "api_sources", "changelog_sources"):
        for source in sources.get(key) or []:
            if isinstance(source, dict) and source.get("require_mention") and source.get("name"):
                gated.add(source["name"])
    return gated


def deduplicate(
    articles: list[Article], existing_hashes: set[str]
) -> tuple[list[Article], list[Article]]:
    """
    Deduplicate articles based on content hash.

    Args:
        articles: List of articles to deduplicate
        existing_hashes: Set of hashes from previously processed articles

    Returns:
        Tuple of (unique_articles, duplicate_articles)
    """
    unique = []
    dupes = []
    seen: dict[str, Article] = {}  # hash -> first article with that hash

    for article in articles:
        # Ensure hash is computed
        if not article.content_hash:
            article.compute_hash()

        # Check if hash already exists in previous runs
        if article.content_hash in existing_hashes:
            dupes.append(article)
            continue

        # Check if we've seen this hash in current batch
        if article.content_hash in seen:
            # It's a duplicate - track source on original
            original = seen[article.content_hash]
            if article.source not in original.also_reported_by:
                original.also_reported_by.append(article.source)
            dupes.append(article)
        else:
            # First time seeing this hash
            seen[article.content_hash] = article
            unique.append(article)

    return unique, dupes


def _keyword_matches(text: str, keyword: str) -> bool:
    """Check if keyword matches in text with appropriate boundary logic."""
    kw = keyword.lower()
    if len(kw) <= 3:
        return bool(re.search(r"\b" + re.escape(kw) + r"\b", text))
    return kw in text


def classify_article(article: Article, categories_config: dict) -> None:
    """
    Classify article into categories based on keyword matching.

    Modifies article.categories in place.

    Args:
        article: Article to classify
        categories_config: Dict with "categories" key containing category definitions
    """
    text = (article.title + " " + article.content).lower()
    categories = categories_config.get("categories", {})

    for category_key, category_info in categories.items():
        keywords = category_info.get("keywords", [])
        for keyword in keywords:
            if _keyword_matches(text, keyword):
                if category_key not in article.categories:
                    article.categories.append(category_key)
                break


def compute_relevance_score(
    article: Article,
    scoring: dict,
    source_tier: int = 2,
    keywords_config: dict | None = None,
) -> int:
    """
    Compute relevance score for an article.

    Sets article.relevance_score and returns the score.

    Args:
        article: Article to score
        scoring: Scoring configuration dict
        source_tier: Tier of the source (1=premium, 2=standard, 3=supplementary)
        keywords_config: Optional brand-monitoring config. When provided,
            ``company.names`` earn company_mention, a ``company.entities`` name
            without the company earns entity_mention (default company_mention),
            ``competitors.*.names`` earn competitor_mention, ``regulators`` earn
            regulatory_mention and ``sector_terms`` earn sector_mention. When None
            (digest profile), none of them applies.

    Returns:
        Computed relevance score
    """
    score = 0
    # The abstract carries what was actually said in a video, while content is
    # only the uploader's blurb. Scoring reads both, or transcript-only signal
    # is filtered out by selection before anyone ever reads it.
    text = (article.title + " " + article.content + " " + article.transcript_abstract).lower()

    # Brand bonuses. When keywords_config is None (digest profile), none applies.
    # A config that predates competitor_mention named the competitor bonus
    # greek_banking; it is still read when competitor_mention is absent.
    if keywords_config:
        found = brand_mentions(article, keywords_config)
        if "company" in found:
            score += scoring.get("company_mention", 0)
        elif "entity" in found:
            score += scoring.get("entity_mention", scoring.get("company_mention", 0))
        if "competitor" in found:
            score += scoring.get("competitor_mention", scoring.get("greek_banking", 0))
        if "regulator" in found:
            score += scoring.get("regulatory_mention", 0)
        if "sector" in found:
            score += scoring.get("sector_mention", 0)

    # Check for Claude/AI tools mentions
    claude_patterns = [
        "claude code",
        "claude ai",
        "anthropic",
        "model context protocol",
        "mcp server",
        "agentic ai",
    ]
    if any(pattern in text for pattern in claude_patterns):
        score += scoring.get("claude_mention", 0)

    # Category match bonus
    if article.categories:
        score += scoring.get("category_match", 0)

    # Source tier bonus
    tier_key = f"tier_{source_tier}_bonus"
    score += scoring.get(tier_key, 0)

    # Recency bonus
    if article.published_at:
        age = datetime.now(UTC) - article.published_at
        hours = age.total_seconds() / 3600

        if hours <= 1:
            score += scoring.get("recency_1h", 0)
        elif hours <= 4:
            score += scoring.get("recency_4h", 0)
        elif hours <= 12:
            score += scoring.get("recency_12h", 0)
        elif hours <= 24:
            score += scoring.get("recency_24h", 0)

    article.relevance_score = score
    return score


def filter_quality(
    articles: list[Article],
    min_words: int = 100,
    max_age_hours: int = 36,
    source_max_age: dict[str, int] | None = None,
    source_min_words: dict[str, int] | None = None,
) -> tuple[list[Article], list[Article]]:
    """
    Filter articles by quality: word count and age.

    Args:
        articles: Articles to filter
        min_words: Minimum word count
        max_age_hours: Maximum age in hours
        source_max_age: Optional per-source age windows keyed by source name.
            Curated sources that publish evergreen material on a weekly cadence
            would otherwise be wiped out by the news-wire window, so they may
            declare a longer one. It is still a ceiling, not an exemption.
        source_min_words: Optional per-source word floors keyed by source name.
            An exchange filing's body is boilerplate ("please see the attached
            announcement"); its substance is the title and the PDF.

    Returns:
        Tuple of (kept_articles, dropped_articles)
    """
    kept = []
    dropped = []
    now = datetime.now(UTC)
    overrides = source_max_age or {}
    floors = source_min_words or {}

    for article in articles:
        # Check word count. The abstract counts: a video's description can be a
        # single line while its transcript abstract is substantial, and dropping
        # here happens before scoring, so the enrichment would never be seen.
        word_count = len((article.content + " " + article.transcript_abstract).split())
        if word_count < floors.get(article.source, min_words):
            dropped.append(article)
            continue

        # Check age, against this source's window if it declared one
        window = overrides.get(article.source, max_age_hours)
        if article.published_at and article.published_at < now - timedelta(hours=window):
            dropped.append(article)
            continue

        kept.append(article)

    return kept, dropped


def extract_content(article: Article) -> None:
    """
    Extract full content from article URL if content is too short.

    Uses trafilatura first, falls back to readability-lxml.
    Modifies article.content in place.

    Args:
        article: Article to extract content for
    """
    # Check if content is already long enough
    word_count = len(article.content.split())
    if word_count >= 100:
        return

    try:
        # Try trafilatura first
        import trafilatura

        downloaded = trafilatura.fetch_url(article.url)
        if downloaded:
            extracted = trafilatura.extract(downloaded)
            if extracted and len(extracted.split()) >= 100:
                article.content = extracted
                return
    except Exception:
        pass

    try:
        # Fallback to readability
        import requests
        from readability import Document

        response = requests.get(article.url, timeout=10)
        doc = Document(response.content)
        content = doc.summary()
        # Strip HTML tags
        import re

        content = re.sub(r"<[^>]+>", "", content)
        if len(content.split()) >= 100:
            article.content = content
    except Exception:
        pass  # Keep original content if extraction fails


def process_articles(
    articles: list[Article],
    existing_hashes: set[str],
    categories_config: dict,
    scoring_config: dict,
    source_tiers: dict[str, int],
    min_words: int = 100,
    max_age_hours: int = 36,
    keywords_config: dict | None = None,
    source_max_age: dict[str, int] | None = None,
    require_mention_sources: set[str] | None = None,
    source_min_words: dict[str, int] | None = None,
) -> tuple[list[Article], dict]:
    """
    Process articles through the full pipeline.

    Pipeline:
    1. Filter by quality (word count, age)
    2. Deduplicate
    3. Drop articles from require_mention sources that name nothing tracked
    4. Classify into categories
    5. Compute relevance scores

    Args:
        articles: Articles to process
        existing_hashes: Set of hashes from previously processed articles
        categories_config: Category definitions
        scoring_config: Scoring configuration
        source_tiers: Mapping of source name to tier
        min_words: Minimum word count for quality filter
        max_age_hours: Maximum age in hours for quality filter
        keywords_config: Optional brand-monitoring config; threaded to
            ``compute_relevance_score`` for company/competitor bonuses.
        source_max_age: Optional per-source age windows; threaded to
            ``filter_quality`` so slow-publishing curated sources survive the
            news-wire window.
        require_mention_sources: Optional names of sources whose articles are
            kept only when they name something in keywords_config (see
            ``sources_requiring_mention``). Ignored without keywords_config.
        source_min_words: Optional per-source word floors; threaded to
            ``filter_quality``.

    Returns:
        Tuple of (processed_articles, stats_dict)
    """
    stats = {
        "input_count": len(articles),
        "quality_dropped": 0,
        "duplicates": 0,
        "unmentioned_dropped": 0,
        "output_count": 0,
    }

    # Step 1: Filter quality
    kept, dropped = filter_quality(
        articles, min_words, max_age_hours, source_max_age, source_min_words
    )
    stats["quality_dropped"] = len(dropped)

    # Step 2: Deduplicate
    unique, dupes = deduplicate(kept, existing_hashes)
    stats["duplicates"] = len(dupes)

    # Step 3: Gate broad feeds, before the tagger spends anything on what goes
    if require_mention_sources and keywords_config:
        mentioned = []
        for article in unique:
            if article.source in require_mention_sources and not (
                brand_mentions(article, keywords_config) & _GATE_GROUPS
            ):
                stats["unmentioned_dropped"] += 1
            else:
                mentioned.append(article)
        unique = mentioned

    # Step 4: Classify and score each unique article
    for article in unique:
        classify_article(article, categories_config)
        tier = source_tiers.get(article.source, 2)
        compute_relevance_score(article, scoring_config, tier, keywords_config=keywords_config)
        from news.tagger import tag_article

        tag_article(article)

    stats["output_count"] = len(unique)
    return unique, stats
