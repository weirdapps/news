"""Brand-literal denylist for the "no brand-specific literals" guard tests.

Why this file exists
--------------------
The guard tests assert that `monitor_synth.py`, `roster.py` and `topic_synth.py`
contain no brand-specific literals. To do that they need the list of literals to
look for, and until 2026-09-09 that list was written inline in the test files.

This repository is PUBLIC. A denylist of real people is still a disclosure of
those people, and worse, it discloses precisely who is being monitored. So the
list is split:

* INSTITUTIONS stay here in the clear. They are company names, not personal
  data, and a repo called "news" that monitors Greek banking discloses nothing
  by naming Greek banks.
* PERSON NAMES move out to `tests/brand_denylist.local.txt`, which is gitignored,
  or to the `NEWS_BRAND_DENYLIST_EXTRA` environment variable (comma-separated).

This is the "move it to a private file the public repo loads at runtime" option,
not the forbidden one. Nothing here splits or encodes a literal to defeat a grep
while leaving the disclosure intact: the names are genuinely absent from the
published tree.

Consequence to be honest about
------------------------------
If neither source is present the person-name half of the guard does not run.
`person_denylist()` therefore reports whether it found anything, and the guard
tests emit a warning rather than passing silently, so an empty list can never be
mistaken for a clean result. To keep the full guard in CI, set
NEWS_BRAND_DENYLIST_EXTRA from a repository secret.
"""

from __future__ import annotations

import os
from pathlib import Path

_LOCAL_FILE = Path(__file__).with_name("brand_denylist.local.txt")

# Institution and brand names. Not personal data; safe in a public repo.
INSTITUTION_DENYLIST: list[str] = [
    "National Bank of Greece",
    "NBG",
    "Εθνική",
    "Ethniki",
    "Piraeus",
    "Alpha Bank",
    "Eurobank",
]


def person_denylist() -> list[str]:
    """Person names to check for, loaded from outside the repository.

    Order of precedence: the NEWS_BRAND_DENYLIST_EXTRA env var, then
    tests/brand_denylist.local.txt (one name per line, '#' comments allowed).
    Returns an empty list when neither is available.
    """
    raw = os.environ.get("NEWS_BRAND_DENYLIST_EXTRA", "")
    if raw.strip():
        return [n.strip() for n in raw.split(",") if n.strip()]
    if _LOCAL_FILE.is_file():
        return [
            line.strip()
            for line in _LOCAL_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    return []


def full_denylist() -> list[str]:
    """Institutions plus whatever person names are available locally."""
    return INSTITUTION_DENYLIST + person_denylist()


def warn_if_person_list_missing() -> str | None:
    """Return a warning message when the person half of the guard is inactive."""
    if person_denylist():
        return None
    return (
        "person-name denylist is EMPTY: set NEWS_BRAND_DENYLIST_EXTRA or create "
        f"{_LOCAL_FILE.name}. The institution checks ran; the person checks did not."
    )
