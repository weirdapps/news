"""Deterministic extractor for what a dated changelog entry actually changed.

Pure, stdlib-only, no I/O and no LLM, so it can run at parse time over every
entry of every fetched changelog document: the whole 159-entry corpus costs
~250 ms. What it produces lands in ``Article.changelog_digest`` and is what the
stack digest quotes when the optional prose upgrade in ``news.changelog_digest``
is unavailable.

Two vendor layouts need two different answers:

``headings``
    The body of a release-notes section already IS the delta. Unit containment
    of each consecutive heading pair in its predecessor is median 0.000, so
    diffing one section against the one below it would report every word of
    both as changed. Pass it through, bounded.
``accordions``
    Each dated entry republishes a whole system prompt, so the news is the
    difference against the same model's previous dated entry. Where there is no
    such entry, ship a labelled profile of the opening rather than an unlabelled
    wall of prompt text.

Cross-model CONTENT diffing is deliberately not done: a different model's prompt
is a lineup reference only, because rendering its absent model strings as
removals announces retirements that never happened.
"""

import difflib
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

# Hard cap on a digest, both layouts. 2000 rather than 2500: a span-recall sweep
# measured 1200 -> 59%, 1500 -> 69%, 2000 -> 78%, 2500 -> 88%, and the facts
# header below is never truncated, so it already carries the model-ID and
# knowledge-cutoff signal. The last 10 points are behavioural prose.
DIGEST_CAP = 2000

# Above this share of changed units the header says MAJOR REWRITE. A LABEL, not
# a gate: the diff is rendered either way. Gating on it discarded a good 51%
# diff whose first ten lines carried the model-string change.
_MAJOR_REWRITE_RATIO = 0.35
# Words of context kept either side of a change inside a ``~`` item.
_INLINE_CONTEXT_WORDS = 4
# Longest run of unchanged words rendered in full inside a ``~`` item.
_INLINE_ELIDE_WORDS = 9
_MAX_ITEM_CHARS = 300
_MAX_LIST_CHARS = 200
_MAX_SECTIONS_CHARS = 260
# Pairing inside one replaced block is O(old x new); above this it is skipped
# and the block renders as plain +/- lines. A bound on the worst case, not a
# quality knob: the largest real block in the corpus is 68 x 66.
_MAX_PAIR_CANDIDATES = 6000
_PAIR_THRESHOLD = 0.5

_NUMERIC_HEX_REF_RE = re.compile(r"&#x([0-9a-fA-F]+);")
_NUMERIC_DEC_REF_RE = re.compile(r"&#(\d+);")
_HARD_BREAK_RE = re.compile(r"\\(?=\n)")
_MD_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!<>&|~])")
_HORIZONTAL_WS_RE = re.compile(r"[ \t]+")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")

# A prompt section tag. Deliberately narrow -- lowercase and underscores only,
# which every tag on this page follows -- because a wider pattern starts eating
# comparisons and generics out of prose.
_TAG = r"</?[a-z][a-z0-9_]*>"
_TAG_FULL_RE = re.compile(rf"^{_TAG}$")
_TAG_SPLIT_RE = re.compile(rf"[ \t]*({_TAG})[ \t]*")

# Each abbreviation lookbehind must swallow its own terminating period: the
# lookbehind sits AFTER the period, so anchoring it before silently matches
# nothing at all.
_ABBREVIATIONS = (
    r"(?<!\be\.g\.)(?<!\bi\.e\.)(?<!\bvs\.)(?<!\bMr\.)(?<!\bMrs\.)(?<!\bDr\.)"
    r"(?<!\bU\.S\.)(?<!\bU\.K\.)(?<!\bInc\.)(?<!\bNo\.)(?<!\bcf\.)"
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])" + _ABBREVIATIONS + r"[ \t]+(?=[\"'(\[A-Z0-9])")

_LIST_MARKER_RE = re.compile(r"^(?:[-*\u2022]\s+|\d+\.\s+)")
_BULLET_LINE_RE = re.compile(r"^[ \t]*[*+-][ \t]+")
_WORD_RE = re.compile(r"\S+")

# API model strings. The trailing date stamp is optional: 'claude-opus-5' and
# 'claude-sonnet-5' carry no stamp, and they are precisely the strings a reader
# has pinned in a config file today.
_MODEL_ID_RES = (
    re.compile(
        r"(?<![\w/.-])claude-(?:opus|sonnet|haiku|fable|mythos|instant)-"
        r"\d(?:[a-z0-9.-]*[a-z0-9])?(?![\w-])"
    ),
    # The pre-4 ordering, e.g. 'claude-3-opus-20240229'.
    re.compile(r"(?<![\w/.-])claude-\d[.\d]*-(?:opus|sonnet|haiku|instant)[a-z0-9.-]*(?![\w-])"),
)
_URL_TARGET_RE = re.compile(r"\]\([^)]*\)")
_BARE_URL_RE = re.compile(r"https?://\S+")
# The window is 120, not 80, because the vendor's own phrasings straddle it: the
# Sonnet 4.6 entry puts 86 chars between "knowledge cutoff" and the month ("date
# - the date past which it cannot answer questions reliably - is the beginning
# of August 2025"), while the identically-shaped Opus 4.7 entry says "is the end
# of" and fits in 78. The three-letter forms are the NEWER phrasing ("is the end
# of Jan 2026" in both Fable 5 and Opus 4.8), so omitting them fails upward.
_CUTOFF_MONTHS = (
    "Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    "Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
)
_CUTOFF_RE = re.compile(
    rf"(?:knowledge (?:base|cutoff)|cutoff)[^.]{{0,120}}?\b({_CUTOFF_MONTHS})\s+(\d{{4}})"
)
_MONTH_EXPANSIONS = {
    "Jan": "January",
    "Feb": "February",
    "Mar": "March",
    "Apr": "April",
    "Jun": "June",
    "Jul": "July",
    "Aug": "August",
    "Sep": "September",
    "Oct": "October",
    "Nov": "November",
    "Dec": "December",
}

_ELLIPSIS = "\u2026"


# --------------------------------------------------------------------------
# Normalisation and segmentation
# --------------------------------------------------------------------------
def _codepoint(digits: str, base: int) -> str:
    """Expand one numeric character reference, or leave it alone.

    ``chr()`` raises on anything above U+10FFFF, and this module is called from
    the fetcher inside a bare ``except`` that turns any exception into a
    zero-article source. A malformed reference on one line of a vendor page is
    not worth losing the whole feed over, so it stays as written.
    """
    try:
        return chr(int(digits, base))
    except (ValueError, OverflowError):
        prefix = "&#x" if base == 16 else "&#"
        return f"{prefix}{digits};"


def _normalize(text: str) -> str:
    """Undo the docs pipeline's markdown mangling, once, for diff AND output.

    Applied identically to both bodies and never applied twice: the normalised
    text is also what gets rendered, so a mismatch between the two would diff
    one form and print another.

    Order is load-bearing. Numeric character references go first or ``&#x49;f``
    diffs against ``If``. ``**`` goes early because bold is the page's
    hand-applied change marker, and leaving it in makes a bolded-but-unchanged
    sentence look changed.

    Args:
        text: raw entry body as served by the docs site

    Returns:
        Normalised text with newlines preserved
    """
    text = _NUMERIC_HEX_REF_RE.sub(lambda m: _codepoint(m.group(1), 16), text)
    text = _NUMERIC_DEC_REF_RE.sub(lambda m: _codepoint(m.group(1), 10), text)
    # Mid-word change markers ship backslash-escaped, so the escape has to come
    # off before ``**`` is stripped or the asterisks survive into the digest.
    text = text.replace("\\*", "*")
    text = text.replace("**", "")
    text = _HARD_BREAK_RE.sub("", text)
    text = _MD_ESCAPE_RE.sub(r"\1", text)
    # The literal two-character backslash-n: one 2024 entry stores its whole
    # prompt on a single physical line, which welds paragraphs into one unit.
    text = text.replace("\\n", "\n")
    return _HORIZONTAL_WS_RE.sub(" ", text)


def _segment(text: str) -> list[tuple[str, str, str]]:
    """Split a normalised body into (key, rendered text, section) units.

    Section tags ride INSIDE prose lines, in runs, so each tag becomes its own
    unit wherever it sits: a section added or removed wholesale then shows up as
    two cheap units instead of smearing across the sentences on either side.

    Args:
        text: already-normalised body

    Returns:
        Units in document order. ``key`` is the case- and list-marker-insensitive
        match key; ``section`` is the innermost open tag, ``""`` outside any.
    """
    pieces: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        for piece in _TAG_SPLIT_RE.split(line):
            piece = piece.strip()
            if not piece:
                continue
            if _TAG_FULL_RE.match(piece):
                pieces.append(piece)
                continue
            pieces.extend(s.strip() for s in _SENTENCE_SPLIT_RE.split(piece) if s.strip())

    # A LENIENT tag stack: a close tag pops down to its own name if that name is
    # open and is ignored otherwise, because 5 of the 29 real entries are
    # unbalanced and a strict stack would mis-attribute the rest of the prompt.
    units: list[tuple[str, str, str]] = []
    stack: list[str] = []
    for piece in pieces:
        if _TAG_FULL_RE.match(piece):
            name = piece.strip("</>")
            if piece.startswith("</"):
                if name in stack:
                    while stack and stack.pop() != name:
                        pass
                units.append((piece, piece, stack[-1] if stack else ""))
            else:
                units.append((piece, piece, name))
                stack.append(name)
            continue
        key = _LIST_MARKER_RE.sub("", piece).strip().lower()
        units.append((key, piece, stack[-1] if stack else ""))
    return units


def _is_tag(key: str) -> bool:
    return bool(_TAG_FULL_RE.match(key))


# --------------------------------------------------------------------------
# Clipping helpers
# --------------------------------------------------------------------------
def _clip_words(text: str, limit: int = _MAX_ITEM_CHARS) -> str:
    """Clip at a word boundary, never mid-token and never in the middle."""
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit - 1)
    head = text[:cut] if cut > 0 else text[: limit - 1]
    return head.rstrip() + _ELLIPSIS


def _join_clipped(parts: list[str], sep: str, limit: int) -> str:
    """Join a list and clip it at a separator boundary, not mid-identifier."""
    joined = sep.join(parts)
    if len(joined) <= limit:
        return joined
    cut = joined.rfind(sep, 0, limit)
    head = joined[:cut] if cut > 0 else joined[: limit - 1]
    return head + _ELLIPSIS


# --------------------------------------------------------------------------
# Facts header
# --------------------------------------------------------------------------
def _model_ids(body: str) -> list[str]:
    """Sorted API model strings in a body, with URL targets removed first.

    Stripping has to precede extraction: real doc slugs such as
    ``claude.com/docs/claude-tag/overview`` mint phantom model IDs the moment
    the two steps are reordered.
    """
    scratch = _BARE_URL_RE.sub("", _URL_TARGET_RE.sub("]", body))
    found: set[str] = set()
    for pattern in _MODEL_ID_RES:
        found.update(pattern.findall(scratch))
    return sorted(found)


def _knowledge_cutoff(body: str) -> str:
    """First stated knowledge cutoff as ``Month YYYY``, or ``""``.

    Only the captured month and year are rendered, which drops the vendor's
    hedges ("is the end of May 2026") without a second pattern for each of them.
    Abbreviated months are expanded so "Jan 2026" and "January 2026" compare
    equal in the ``(was ...)`` line rather than reading as a cutoff change.
    """
    match = _CUTOFF_RE.search(body)
    if not match:
        return ""
    month = match.group(1)
    return f"{_MONTH_EXPANSIONS.get(month, month)} {match.group(2)}"


def _facts_header(
    body: str,
    predecessor_body: str | None,
    *,
    cross_model: bool,
    predecessor_label: str,
) -> list[str]:
    """Model-ID and knowledge-cutoff lines, computed for the prose LLM upfront.

    These lines are never truncated by the cap, which is why the digest still
    carries the two facts the reader's stack depends on -- a pinned model string
    and a cutoff -- even when the body diff is cut off after ten lines.

    Args:
        body: normalised entry body
        predecessor_body: normalised baseline body, or None
        cross_model: baseline belongs to a DIFFERENT model
        predecessor_label: how to name the baseline in prose

    Returns:
        Zero to two header lines, in document-stable order
    """
    current = _model_ids(body)
    lines: list[str] = []

    if predecessor_body is None:
        if current:
            lines.append("MODEL IDS: " + _join_clipped(current, ", ", _MAX_LIST_CHARS))
    else:
        previous = _model_ids(predecessor_body)
        gained = [i for i in current if i not in previous]
        lost = [i for i in previous if i not in current]
        if cross_model:
            if gained or lost:
                groups = []
                if gained:
                    groups.append(_join_clipped([f"+{i}" for i in gained], " ", _MAX_LIST_CHARS))
                if lost:
                    groups.append(_join_clipped([f"-{i}" for i in lost], " ", _MAX_LIST_CHARS))
                # The hedge lives INSIDE the label so that a later edit to the
                # rendering cannot quietly drop it and turn a lineup difference
                # into a retirement announcement.
                lines.append(
                    f"MODEL LINEUP vs {predecessor_label} (a DIFFERENT model - absence is "
                    f"not a retirement): " + " / ".join(groups)
                )
        elif gained or lost:
            if gained:
                lines.append("MODEL IDS +: " + _join_clipped(gained, ", ", _MAX_LIST_CHARS))
            if lost:
                lines.append("MODEL IDS -: " + _join_clipped(lost, ", ", _MAX_LIST_CHARS))
        elif current:
            lines.append(
                "MODEL IDS: unchanged (" + _join_clipped(current, ", ", _MAX_LIST_CHARS) + ")"
            )

    cutoff = _knowledge_cutoff(body)
    if cutoff:
        previous_cutoff = _knowledge_cutoff(predecessor_body) if predecessor_body else ""
        if previous_cutoff and previous_cutoff != cutoff:
            lines.append(f"KNOWLEDGE CUTOFF: {cutoff} (was {previous_cutoff})")
        else:
            lines.append(f"KNOWLEDGE CUTOFF: {cutoff}")
    return lines


# --------------------------------------------------------------------------
# Capping
# --------------------------------------------------------------------------
def _tail_lines(
    dropped: int,
    dropped_sections: list[str],
    noun: str,
) -> list[str]:
    """Truncation notices, reserved before filling so they are never chopped."""
    if dropped <= 0:
        return []
    lines = [f"{_ELLIPSIS} +{dropped} more {noun} not shown"]
    if dropped_sections:
        counts: dict[str, int] = {}
        for section in dropped_sections:
            if section:
                counts[section] = counts.get(section, 0) + 1
        if counts:
            summary = _join_clipped(
                [f"{name} ({n})" for name, n in counts.items()], ", ", _MAX_LIST_CHARS
            )
            lines.append(f"Also changed, not shown: {summary}")
    return lines


def _pack(
    header: list[str],
    items: list[str],
    cap: int,
    noun: str,
    sections: list[str] | None = None,
) -> str:
    """Header lines always survive; items fill the rest in DOCUMENT ORDER.

    Document order is the truncation strategy because on both pages it is also
    the relevance order: a release-notes section leads with the launch bullet
    and a system prompt leads with ``<product_information>``, so model IDs,
    tiers, availability and cutoffs are the last thing dropped without a single
    keyword rule. The tail notices are costed into the budget before filling
    starts, so a truncation can never eat its own truncation notice.

    Args:
        header: lines that must survive whatever happens
        items: rendered item lines in document order
        cap: hard character cap for the whole digest
        noun: what the "+N more ... not shown" line counts
        sections: per-item section names, when dropped items are worth grouping

    Returns:
        The packed digest, at most ``cap`` characters
    """
    base = "\n".join(header)
    kept: list[str] = []
    for index, item in enumerate(items):
        dropped = len(items) - index - 1
        tail = _tail_lines(dropped, sections[index + 1 :] if sections else [], noun)
        candidate = "\n".join([base, *kept, item, *tail])
        if len(candidate) > cap and kept:
            break
        kept.append(item)

    tail = _tail_lines(len(items) - len(kept), sections[len(kept) :] if sections else [], noun)
    return "\n".join([base, *kept, *tail])[:cap].rstrip()


# --------------------------------------------------------------------------
# Baseline selection
# --------------------------------------------------------------------------
_ORDINAL_RE = re.compile(r"(?<=\d)(?:st|nd|rd|th)(?=,)")


def _parse_date(date_str: str) -> datetime | None:
    try:
        return datetime.strptime(_ORDINAL_RE.sub("", date_str), "%B %d, %Y")
    except ValueError:
        return None


def select_predecessor(entries: list[dict], index: int) -> tuple[int | None, bool]:
    """Choose the baseline for one entry of a parsed changelog document.

    Preference order: the same model's next dated entry further down the page,
    which is a true revision lineage; failing that the chronologically previous
    entry anywhere in the document, which is a DIFFERENT model and is therefore
    usable only for the lineup facts header, never for a content diff.

    Args:
        entries: parsed entries of one document in document order, each with
            ``model``, ``date`` and ``body`` keys
        index: position of the entry needing a baseline

    Returns:
        ``(predecessor index or None, cross_model)``. ``cross_model`` is True
        only for the chronological fallback.
    """
    model = entries[index].get("model")
    own_date = _parse_date(entries[index].get("date", ""))
    if model:
        same_model = [i for i, e in enumerate(entries) if e.get("model") == model]
        parsed = [_parse_date(entries[i].get("date", "")) for i in same_model]
        dates = [d for d in parsed if d is not None]
        # Shipped as an assertion rather than a comment: the page being
        # newest-first within a model chunk is an undocumented vendor property,
        # and a violation would render additions as removals with total
        # confidence. Degrade to the no-baseline profile instead.
        if len(dates) != len(parsed) or any(
            dates[i] <= dates[i + 1] for i in range(len(dates) - 1)
        ):
            logger.warning(
                f"changelog entries for {model} are not strictly newest-first; "
                f"falling back to the no-baseline profile"
            )
            return None, False
        following = [i for i in same_model if i > index]
        if following:
            return following[0], False

    if own_date is None:
        return None, False
    # Models are not globally sorted on the page, so the fallback ranks on the
    # parsed date rather than on document position.
    best: int | None = None
    best_date: datetime | None = None
    for i, entry in enumerate(entries):
        if i == index:
            continue
        date = _parse_date(entry.get("date", ""))
        if date is None or date >= own_date:
            continue
        if best_date is None or date > best_date:
            best, best_date = i, date
    return (best, True) if best is not None else (None, False)


# --------------------------------------------------------------------------
# The public digest
# --------------------------------------------------------------------------
def changelog_delta(
    entry_body: str,
    predecessor_body: str | None,
    layout: str,
    *,
    predecessor_label: str = "",
    cross_model: bool = False,
    cap: int = DIGEST_CAP,
) -> str:
    """Digest of what one dated changelog entry changed.

    Args:
        entry_body: raw body of the entry being described
        predecessor_body: raw body of the baseline entry, or None
        layout: ``"headings"`` or ``"accordions"``, from the source config
        predecessor_label: how to name the baseline in prose
        cross_model: baseline belongs to a DIFFERENT model, so its content is
            not comparable and only the lineup facts are used
        cap: hard character cap

    Returns:
        A plain-text digest of at most ``cap`` characters. Never raises: this
        runs at parse time, where there is no error path.
    """
    if layout == "headings":
        return _headings_digest(entry_body, cap)

    body = _normalize(entry_body)
    previous = _normalize(predecessor_body) if predecessor_body is not None else None
    label = predecessor_label or "the previous dated entry"
    facts = _facts_header(body, previous, cross_model=cross_model, predecessor_label=label)

    if previous is None or cross_model:
        return _profile_digest(body, facts, cap)
    if body == previous:
        units = _segment(body)
        return "\n".join(
            [
                *facts,
                f"NO TEXTUAL CHANGE vs {label}: identical after normalisation "
                f"({len(units)} sentences/tags compared).",
            ]
        )[:cap]
    return _same_model_digest(body, previous, facts, label, cap)


def _headings_digest(body: str, cap: int) -> str:
    """Pass the body through: it already IS the delta, so nothing is diffed.

    Truncation is depth-first then breadth-first -- whole bullets while they
    fit, else every bullet cut to its first sentence, else drop the tail --
    because the median entry is one 271-char bullet that never reaches stage
    two, while the fat entries are many small bullets where a headline apiece
    beats five bullets in full.
    """
    text = _MD_LINK_RE.sub(r"\1", _normalize(body))
    items: list[str] = []
    previous_blank = True
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            previous_blank = True
            continue
        # A non-bullet line continues the bullet above only when it is a wrapped
        # fragment of it. A blank line in between makes it a new paragraph: the
        # page writes standalone lead-ins ("We also released new official SDKs:")
        # that were otherwise welded onto the tail of the preceding bullet and
        # misattributed to it.
        if _BULLET_LINE_RE.match(line) or previous_blank or not items:
            items.append(stripped)
        else:
            items[-1] += " " + stripped
        previous_blank = False

    header = [f"CHANGES ANNOUNCED ({len(items)} item{'' if len(items) == 1 else 's'}):"]
    budget = cap - len(header[0]) - 1
    full = [_clip_words(item, cap) for item in items]
    if sum(len(item) + 1 for item in full) <= budget:
        return _pack(header, full, cap, "item(s)")
    return _pack(header, _fit_headings_items(full, budget), cap, "item(s)")


def _fit_headings_items(full: list[str], budget: int) -> list[str]:
    """Give every announced item a headline, then restore whole ones in order.

    Cutting every bullet to its lead sentence and stopping there was silently
    losing the body of a model-launch entry while leaving a third of the budget
    unspent: the Sonnet 5 launch note dropped its 1M context window, its removal
    of manual extended thinking and its new tokenizer, and said nothing about it.
    A shortened item keeps a trailing ellipsis so the loss is visible.
    """
    lead: list[str] = []
    for item in full:
        head = _SENTENCE_SPLIT_RE.split(item)[0]
        lead.append(head if head == item else f"{head} {_ELLIPSIS}")

    chosen = list(lead)
    used = sum(len(item) + 1 for item in chosen)
    for index, item in enumerate(full):
        if item == chosen[index]:
            continue
        growth = len(item) - len(chosen[index])
        if used + growth <= budget:
            chosen[index] = item
            used += growth
    return chosen


def _profile_digest(body: str, facts: list[str], cap: int) -> str:
    """No usable baseline: ship the opening of the prompt, labelled and bounded.

    Every entry in every era opens with the product block -- tagged entries with
    ``<claude_behavior> <product_information>``, untagged 2024 ones with "The
    assistant is Claude, created by Anthropic" -- so model names, tiers, API
    strings and cutoffs land in the first 2,000 characters without a keyword
    list or a tag heuristic.
    """
    units = _segment(body)
    header = [
        "NEW MODEL ENTRY: no earlier dated entry for this model, so the whole prompt "
        f"is new. Prompt is {len(units)} sentences/tags.",
        *facts,
    ]
    opened = [text for key, text, _ in units if _is_tag(key) and not key.startswith("</")]
    if opened:
        header.append(_clip_words("Sections: " + ", ".join(opened), _MAX_SECTIONS_CHARS))
    header.append("Opening of the prompt, verbatim:")
    return _pack(
        header,
        [_clip_words(text, _MAX_ITEM_CHARS) for _, text, _ in units],
        cap,
        "sentence(s) of the prompt",
    )


def _same_model_digest(body: str, previous: str, facts: list[str], label: str, cap: int) -> str:
    """Unit-level diff of two dated entries for the same model."""
    current_units = _segment(body)
    previous_units = _segment(previous)
    current_keys = [u[0] for u in current_units]
    previous_keys = [u[0] for u in previous_units]

    items: list[str] = []
    item_sections: list[str] = []
    seen: set[str] = set()
    added = removed = edited = changed = 0
    sections_added: list[str] = []
    sections_removed: list[str] = []

    matcher = difflib.SequenceMatcher(None, previous_keys, current_keys, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old = list(range(i1, i2))
        new = list(range(j1, j2))
        changed += len(new)
        pairs, only_old, only_new = _pair(previous_keys, current_keys, old, new)

        # Emitted in the new document's order, with removals sorted to the head
        # of their block so they read as "this went, that arrived".
        emitted: list[tuple[float, str, str]] = []
        for oi, ni in pairs:
            edited += 1
            emitted.append(
                (
                    float(ni),
                    current_units[ni][2],
                    _render_item(
                        "~",
                        current_units[ni][2],
                        _inline_diff(previous_units[oi][1], current_units[ni][1]),
                    ),
                )
            )
        for ni in only_new:
            added += 1
            key, text, section = current_units[ni]
            if _is_tag(key) and not key.startswith("</"):
                sections_added.append(key.strip("</>"))
            emitted.append((float(ni), section, _render_item("+", section, text)))
        for oi in only_old:
            removed += 1
            key, text, section = previous_units[oi]
            if _is_tag(key) and not key.startswith("</"):
                sections_removed.append(key.strip("</>"))
            emitted.append((j1 - 0.5, section, _render_item("-", section, text)))

        emitted.sort(key=lambda e: e[0])
        for _, section, rendered in emitted:
            # The 2024 entries publish the prompt twice, a "Text-only:" rendition
            # plus the image-enabled one, so 47% of their units are duplicates.
            if rendered in seen:
                continue
            seen.add(rendered)
            items.append(rendered)
            item_sections.append(section)

    if not items:
        return "\n".join(
            [
                *facts,
                f"NO TEXTUAL CHANGE vs {label}: identical after normalisation "
                f"({len(current_units)} sentences/tags compared).",
            ]
        )[:cap]

    total = max(1, len(current_units))
    kind = "DELTA" if changed / total <= _MAJOR_REWRITE_RATIO else "MAJOR REWRITE"
    header = [
        *facts,
        f"{kind} vs {label}: {changed} of {len(current_units)} sentences/tags changed "
        f"({round(100 * changed / total)}%); +{added} added, -{removed} removed, "
        f"~{edited} edited.",
    ]
    if sections_added or sections_removed:
        header.append(
            "Sections: "
            + _join_clipped(
                [f"+<{n}>" for n in sections_added] + [f"-<{n}>" for n in sections_removed],
                ", ",
                _MAX_SECTIONS_CHARS,
            )
        )
    return _pack(header, items, cap, "changed passage(s)", sections=item_sections)


def _pair(
    previous_keys: list[str],
    current_keys: list[str],
    old: list[int],
    new: list[int],
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Greedily pair replaced units so a reworded sentence renders as one ``~``.

    Deterministic by construction: candidates are scored by ratio and ties are
    broken by (old index, new index), never by set or dict iteration order.

    Tag units are never paired, with each other or with prose. Character
    similarity happily pairs ``<behavior_instructions>`` with
    ``<product_information>`` and renders a section reorder as a rename; tags
    are cheap to print twice and the header's Sections line already sums them.
    """
    scored: list[tuple[float, int, int]] = []
    if len(old) * len(new) <= _MAX_PAIR_CANDIDATES:
        matcher = difflib.SequenceMatcher(autojunk=False)
        for oi in old:
            if _is_tag(previous_keys[oi]):
                continue
            matcher.set_seq2(previous_keys[oi])
            for ni in new:
                if _is_tag(current_keys[ni]):
                    continue
                matcher.set_seq1(current_keys[ni])
                # Both quick ratios are upper bounds, so pruning on them cannot
                # change which pairs clear the threshold.
                if (
                    matcher.real_quick_ratio() < _PAIR_THRESHOLD
                    or matcher.quick_ratio() < _PAIR_THRESHOLD
                ):
                    continue
                ratio = matcher.ratio()
                if ratio >= _PAIR_THRESHOLD:
                    scored.append((ratio, oi, ni))

    scored.sort(key=lambda c: (-c[0], c[1], c[2]))
    used_old: set[int] = set()
    used_new: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for _, oi, ni in scored:
        if oi in used_old or ni in used_new:
            continue
        used_old.add(oi)
        used_new.add(ni)
        pairs.append((oi, ni))
    pairs.sort(key=lambda p: p[1])
    return pairs, [i for i in old if i not in used_old], [i for i in new if i not in used_new]


def _inline_diff(old: str, new: str) -> str:
    """Word-level diff of two paired sentences, eliding long unchanged runs."""
    a, b = _WORD_RE.findall(old), _WORD_RE.findall(new)
    opcodes = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    out: list[str] = []
    for index, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag == "equal":
            span = b[j1:j2]
            if len(span) <= _INLINE_ELIDE_WORDS:
                out.append(" ".join(span))
            elif index == 0:
                out.append(f"{_ELLIPSIS} " + " ".join(span[-_INLINE_CONTEXT_WORDS:]))
            elif index == len(opcodes) - 1:
                out.append(" ".join(span[:_INLINE_CONTEXT_WORDS]) + f" {_ELLIPSIS}")
            else:
                out.append(
                    " ".join(span[:_INLINE_CONTEXT_WORDS])
                    + f" {_ELLIPSIS} "
                    + " ".join(span[-_INLINE_CONTEXT_WORDS:])
                )
            continue
        if tag in ("delete", "replace"):
            out.append("[-" + " ".join(a[i1:i2]) + "-]")
        if tag in ("insert", "replace"):
            out.append("{+" + " ".join(b[j1:j2]) + "+}")
    return " ".join(part for part in out if part)


def _render_item(mark: str, section: str, text: str) -> str:
    prefix = f"{mark} [{section}] " if section else f"{mark} "
    return prefix + _clip_words(text, _MAX_ITEM_CHARS)
