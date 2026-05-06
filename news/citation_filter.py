"""Filter and enrich synthesis output using article_ids citations.

The synthesis LLM is required to cite the input article id(s) supporting each
bullet, section, alert, and per-article mention. This module:

- Drops items that fail to cite any valid id (silent — invented content stays
  out of the email).
- Enriches surviving sections / mentions with the resolved (title, url, source)
  so renderers can show clickable links.

Pure functions over (parsed_synthesis_block, input_articles_list). The article
list is indexed positionally — `article_ids` are integer offsets matching the
`id` field that `build_prompt` / `build_monitor_prompt` write into the prompt's
articles array.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _coerce_ids(raw: Any) -> list[int]:
    """Coerce a raw article_ids value into a list of ints. Tolerates strings.

    Returns [] for missing/invalid values rather than raising — a malformed
    citation is treated as no citation, which causes the bullet to be dropped.
    """
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for item in raw:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _valid_ids(raw_ids: list[int], pool_size: int) -> list[int]:
    """Filter ids to those in [0, pool_size)."""
    return [i for i in raw_ids if 0 <= i < pool_size]


def filter_unsourced_bullets(bullets: list[Any], articles: list[Any]) -> list[str]:
    """Return only bullets with at least one valid article_id, flattened to text.

    Accepts either {text, article_ids} dicts (new schema) or plain strings
    (legacy / non-compliant LLM output — always dropped as unsourced).
    """
    pool_size = len(articles)
    kept: list[str] = []
    dropped = 0
    for bullet in bullets:
        if not isinstance(bullet, dict):
            dropped += 1
            continue
        ids = _valid_ids(_coerce_ids(bullet.get("article_ids")), pool_size)
        if not ids:
            dropped += 1
            continue
        text = bullet.get("text")
        if not isinstance(text, str) or not text.strip():
            dropped += 1
            continue
        kept.append(text)
    if dropped:
        logger.info(f"citation_filter: dropped {dropped} unsourced bullet(s)")
    return kept


def filter_unsourced_sections(sections: list[Any], articles: list[Any]) -> list[dict]:
    """Return only sections with at least one valid article_id."""
    pool_size = len(articles)
    kept: list[dict] = []
    dropped = 0
    for section in sections:
        if not isinstance(section, dict):
            dropped += 1
            continue
        ids = _valid_ids(_coerce_ids(section.get("article_ids")), pool_size)
        if not ids:
            dropped += 1
            continue
        kept.append(section)
    if dropped:
        logger.info(f"citation_filter: dropped {dropped} unsourced section(s)")
    return kept


def enrich_section_articles(sections: list[dict], articles: list[Any]) -> list[dict]:
    """For each section, resolve article_ids → [{title, url, source}] in `articles`.

    Mutates and returns the section list. Sections must already be filtered.
    """
    pool_size = len(articles)
    for section in sections:
        ids = _valid_ids(_coerce_ids(section.get("article_ids")), pool_size)
        section["articles"] = [
            {
                "title": articles[i].title,
                "url": articles[i].url,
                "source": articles[i].source,
            }
            for i in ids
        ]
    return sections


def enrich_mentions(mentions: list[Any], articles: list[Any]) -> list[dict]:
    """Filter mentions without article_ids; enrich surviving with `url` from first id."""
    pool_size = len(articles)
    kept: list[dict] = []
    dropped = 0
    for mention in mentions:
        if not isinstance(mention, dict):
            dropped += 1
            continue
        ids = _valid_ids(_coerce_ids(mention.get("article_ids")), pool_size)
        if not ids:
            dropped += 1
            continue
        mention = dict(mention)  # don't mutate caller's dict
        mention["url"] = articles[ids[0]].url
        kept.append(mention)
    if dropped:
        logger.info(f"citation_filter: dropped {dropped} unsourced mention(s)")
    return kept


def filter_competitor_watch(
    competitor_watch: Any, articles: list[Any]
) -> dict[str, str]:
    """Filter competitor_watch entries without article_ids.

    Accepts both schemas:
    - new: {key: {summary, article_ids}}
    - legacy: {key: "summary string"} (always dropped as unsourced)

    Returns flat {key: summary} for backward-compatible template iteration.
    """
    if not isinstance(competitor_watch, dict):
        return {}
    pool_size = len(articles)
    kept: dict[str, str] = {}
    dropped = 0
    for key, value in competitor_watch.items():
        if not isinstance(value, dict):
            dropped += 1
            continue
        ids = _valid_ids(_coerce_ids(value.get("article_ids")), pool_size)
        if not ids:
            dropped += 1
            continue
        summary = value.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            dropped += 1
            continue
        kept[key] = summary
    if dropped:
        logger.info(f"citation_filter: dropped {dropped} unsourced competitor entry(s)")
    return kept
