"""Canonical executive roster and name-handling rules for LLM synthesis prompts.

Shared by `monitor_synth.py` and `synthesizer.py` so both pipelines anchor
Greek surname-only references against the same source of truth, preventing
hallucinated first names and inconsistent transliterations.
"""

# Format per line: "Greek surname (gen./nom.) → English Name, Role (gender)"
# Gender matters because Greek surname endings encode it (-η/-ου = f, -ης/-ος = m)
# and the English transliteration must preserve it.
EXECUTIVE_ROSTER = """NBG (Εθνική Τράπεζα / ΕΤΕ):
- Μυλωνάς / Π. Μυλωνάς → Pavlos Mylonas, CEO (m)
- Θεοφιλίδη → Christina Theofilidi, GM Retail Banking (f)
- Μολυβιάτης → Stratos Molyviatis, NBG Group COO (m)
- Καραμούζης / Β. Καραμούζης → Vasilis Karamouzis, GM Corporate Banking (m)
- Πλέσσας → Dimitrios Plessas, AGM Cards & Digital Business (m)

Greek banking competitors (CEOs and senior execs commonly quoted):
- Μεγάλου → Christos Megalou, Piraeus Bank CEO (m)
- Ψάλτης → Vasilis Psaltis, Alpha Bank CEO (m)
- Καραβίας → Fokion Karavias, Eurobank CEO (m)
"""

NAME_HANDLING_PROMPT_BLOCK = (
    """**EXECUTIVE NAME ROSTER (CANONICAL — use these spellings exactly):**
"""
    + EXECUTIVE_ROSTER
    + """
**NAME HANDLING RULES (CRITICAL — past reports had hallucinated names):**
1. When a Greek surname matches the roster, use the canonical English transliteration EXACTLY as listed. Do NOT invent variants.
2. NEVER invent first names. If the article references only a surname, write only the surname (e.g., "Theofilidi said..." NOT "Christina Theofilidi said..." unless the article itself includes the first name). Use the roster to verify *which* person the surname refers to, but only add the first name if the article does.
3. Greek surname endings encode gender: -η / -ου are feminine, -ης / -ος are masculine. Preserve the ending in transliteration. Θεοφιλίδη → Theofilidi (NOT Theofilidis). Πολίτη → Politi (NOT Politis).
4. If a Greek surname is NOT in the roster, transliterate it phonetically and prefix the mention with "[unverified name]" so the reader knows to double-check.
5. Do NOT add stress accents to English transliterations (write "Georgopoulos" not "Georgópoulos", "Tzouros" not "Tzoúros").
6. NEVER attribute a quote or position to a person not actually named in the article. If unsure who said something, attribute it to the institution ("an NBG executive said...")."""
)
