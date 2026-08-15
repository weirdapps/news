"""Source health: which configured sources have gone quiet for real.

A single run cannot tell a dead source from a quiet one. arXiv publishes on
weekdays, brand-monitor queries have nothing to say on most Saturdays, and a
feed whose markup or query operator broke returns exactly the same empty list.
That ambiguity is how the-agent-daily.org sat dead for weeks and how 21 feeds
kept a `allinurl:` operator that Google News had stopped honouring.

The database already holds the discriminator: when each source last produced an
article. A source silent for days is broken; a source silent since Friday is
just quiet. No new state, no per-run bookkeeping.
"""

import sqlite3
from datetime import UTC, datetime

# Shown in an email footer, so the list has to stay short enough to read.
_MAX_NAMED = 6


def stale_sources(
    conn: sqlite3.Connection,
    configured: list[str],
    pipeline: str = "digest",
    days: int = 7,
) -> list[tuple[str, int | None]]:
    """Return configured sources that have produced nothing recently.

    Args:
        conn: open database connection
        configured: every source name the profile has configured
        pipeline: profile to scope the history to; a source can be healthy in
            one profile and dead in another
        days: silence beyond this many days counts as stale

    Returns:
        ``(source_name, days_since_last_article)`` pairs, worst first. A days
        value of ``None`` means the source has never produced anything at all,
        which for an established profile means it has never worked.
    """
    if not configured:
        return []

    placeholders = ",".join("?" * len(configured))
    rows = conn.execute(
        f"SELECT source, MAX(fetched_at) AS last_seen FROM articles "
        f"WHERE pipeline = ? AND source IN ({placeholders}) GROUP BY source",
        [pipeline, *configured],
    ).fetchall()
    last_seen = {row["source"]: row["last_seen"] for row in rows}

    now = datetime.now(UTC)
    stale: list[tuple[str, int | None]] = []
    for name in configured:
        raw = last_seen.get(name)
        if not raw:
            stale.append((name, None))
            continue
        try:
            seen = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            stale.append((name, None))
            continue
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=UTC)
        age = (now - seen).days
        if age >= days:
            stale.append((name, age))

    # Never-produced first, then longest silence.
    stale.sort(key=lambda s: (s[1] is not None, -(s[1] or 0)))
    return stale


def format_health_note(stale: list[tuple[str, int | None]]) -> str:
    """One line naming the silent sources, for an email footer. "" if all healthy."""
    if not stale:
        return ""

    # "(never)" and "(30d)" mean different things: the first is a source that
    # has never once worked, which is a configuration error; the second is a
    # source that used to work and stopped, which is a regression.
    named = []
    for name, age in stale[:_MAX_NAMED]:
        named.append(f"{name} (never)" if age is None else f"{name} ({age}d)")

    suffix = f" +{len(stale) - _MAX_NAMED} more" if len(stale) > _MAX_NAMED else ""
    return f"{len(stale)} sources silent: " + ", ".join(named) + suffix
