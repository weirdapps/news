"""Name-handling rules and executive-roster builder for LLM synthesis prompts.

`NAME_HANDLING_RULES` is the brand-neutral guidance that any synthesis pipeline
can drop into a prompt verbatim. `build_roster(keywords_config)` returns the
brand-aware roster section (canonical names + roles + competitor names) plus
the generic rules — used by the monitor pipeline. Pass `None` (or omit) when
no brand config is available; the function returns just `NAME_HANDLING_RULES`.
"""

NAME_HANDLING_RULES = """**NAME HANDLING RULES (CRITICAL — past reports had hallucinated names):**
1. NEVER invent first names. If the article references only a surname, write only the surname. If you have a roster, use it to verify *which* person the surname refers to, but only add the first name if the article itself includes it.
2. Preserve the original-language surname suffix when transliterating. Suffix endings often encode gender; do not strip or change them.
3. Do not add stress accents to English transliterations.
4. NEVER attribute a quote or position to a person not actually named in the article. If unsure who said something, attribute to the institution.
5. If a surname is not in any provided roster, transliterate it phonetically and prefix the mention with "[unverified name]" so the reader knows to double-check."""


def build_roster(keywords_config: dict | None = None) -> str:
    """Return the executive-roster prompt section.

    With keywords_config: returns the full brand-aware roster
    (leadership + competitors) followed by NAME_HANDLING_RULES.

    Without keywords_config: returns only NAME_HANDLING_RULES (brand-neutral).
    """
    if keywords_config is None:
        return NAME_HANDLING_RULES

    leadership = keywords_config.get("company", {}).get("leadership", [])
    competitors = keywords_config.get("competitors", {})

    if not leadership and not competitors:
        return NAME_HANDLING_RULES

    parts = ["**EXECUTIVE NAME ROSTER (CANONICAL — use these spellings exactly):**"]

    if leadership:
        leadership_lines = []
        for person in leadership:
            name_en = person.get("name_en") or person.get("name", "")
            name_native = person.get("name") if person.get("name_en") else None
            role = person.get("role", "")
            if not name_en:
                continue
            if name_native and name_native != name_en:
                leadership_lines.append(f"- {name_native} → {name_en}, {role}")
            else:
                leadership_lines.append(f"- {name_en}, {role}")
        if leadership_lines:
            parts.append("Leadership:")
            parts.extend(leadership_lines)

    if competitors:
        comp_lines = []
        for _, comp in competitors.items():
            names = comp.get("names", [])
            if names:
                comp_lines.append(f"- {names[0]}")
        if comp_lines:
            parts.append("\nKey competitors:")
            parts.extend(comp_lines)

    parts.append("")  # blank line before rules
    parts.append(NAME_HANDLING_RULES)

    return "\n".join(parts)
