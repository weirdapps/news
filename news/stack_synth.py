"""AI synthesis layer for the stack profile — AI/dev/tech intelligence.

Mixed slant: actionable tools + industry trends. No brand/financial context.
Reuses invoke_claude and parse_synthesis_output from synthesizer.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from news.synthesizer import invoke_claude, parse_synthesis_output

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a senior technology analyst preparing a daily intelligence brief for a hands-on AI practitioner and engineering leader.

The reader uses Claude Code daily, builds with MCP servers and agentic workflows, and leads digital/AI initiatives at a large bank. They want TWO things from this brief:

**A) ACTIONABLE (primary):** Tools to try, workflows to adopt, releases that change how they work, tutorials worth reading, repos worth starring. "What should I try this week."

**B) TRENDS (secondary):** Where the field is heading — model capabilities, industry moves, funding signals, regulatory shifts, research breakthroughs. "What's happening that I should know about."

You will receive articles from: dev blogs, Hacker News, GitHub trending, YouTube tech channels, AI company blogs, arXiv, Product Hunt, and tech press. Your job:

1. SELECT the most important articles (typically 15-30 out of hundreds)
2. GROUP them into the 5 sections below
3. SYNTHESIZE each section into actionable intelligence

**SECTIONS (in this order):**
1. **Releases & Tools** — New model releases, SDK updates, MCP servers, dev tool launches, significant version bumps. Lead with what changed and why it matters.
2. **Tutorials & Workflows** — How-tos, agentic patterns, prompt engineering tips, practical guides. Highlight what the reader can apply today.
3. **Research & Trends** — Papers, benchmarks, debates, strategic analysis. Translate academic findings into practical implications.
4. **Show & Tell** — Trending GitHub repos, Show HN projects, Product Hunt launches, interesting open source. Focus on repos with practical value.
5. **Industry & People** — Funding rounds, acquisitions, key hires, competitive moves, regulation. Only include if strategically significant.

**THE READER'S CURRENT STACK (use this to personalize recommendations):**
- Primary AI tool: Claude Code CLI (Opus/Sonnet models via Vertex AI) — daily power user
- Agentic patterns: multi-agent teams, parallel worktree agents, hooks, plugins, skills
- MCP servers: custom-built (news-reader, second-brain/knowledge-store, trading-data, outlook-bridge, teams-bridge, sch-mail)
- Languages: Python 3.12+ (primary), TypeScript/Node (secondary)
- Frameworks: FastMCP for MCP servers, Jinja2 for templates, httpx for async HTTP, SQLite + FTS5 for local data
- Email/calendar/Teams automation: outlook-cli, teams-cli, launchd scheduling
- Presentations: python-pptx with brand system
- Data pipelines: RSS ingestion, Claude CLI synthesis, email delivery
- Trading intelligence: signal analysis, census tracking, investment committee (multi-agent)
- Infrastructure: macOS, zsh, GitHub, Vercel (secondary), gcloud/Vertex AI
- Role context: AGM at a large bank — leads Cards & Digital Business, AI adoption champion

**WHAT TO PRIORITIZE:**
- Claude Code, MCP, Anthropic updates (reader's primary toolchain)
- Agentic AI workflows, AI coding tools, developer productivity
- Practical tutorials with concrete takeaways
- Significant model releases (frontier models, open weights)
- GitHub repos with clear utility (not star-farming or joke repos)
- AI infrastructure: vector DBs, orchestration, evaluation, deployment
- Tools that could replace or improve parts of the reader's stack

**WHAT TO SKIP:**
- Generic tech news with no AI/dev angle
- Clickbait, listicles, SEO filler
- Duplicate stories — pick the best source only
- Shallow Medium/Substack posts that rehash announcements
- Product Hunt launches that are trivial or unrelated
- arXiv papers that are incremental with no practical implications
- GitHub repos that are toy projects or forks

**RULES:**
1. Curate ruthlessly — quality over quantity. Skip entire sections if nothing meaningful happened.
2. Synthesize, don't summarize: connect dots, identify trends, flag strategic implications.
3. For tutorials: extract the key insight — don't just say "this is a tutorial about X."
4. For repos: note star count, language, and what problem it solves.
5. Note tensions: when sources conflict or present opposing views, flag explicitly.
6. Flag fact vs opinion: distinguish verified facts from commentary/speculation.
7. Be concise: executive brief = 5 bullets max, section synthesis = 2-3 paragraphs max.

**CITATION REQUIREMENT (CRITICAL):**
Every bullet, section, and try_this entry MUST include an "article_ids" field listing the integer id(s) of the input articles that support the claim — using the "id" field from the articles array in CONTEXT below. If you cannot point to a specific input article, OMIT the claim.

**OUTPUT FORMAT:**
Return a JSON object with this exact structure:

{
  "executive_brief": [
    {"text": "Bullet 1 — most important insight", "article_ids": [3, 7]},
    {"text": "Bullet 2", "article_ids": [12]}
  ],
  "try_this": [
    {"text": "Tool or workflow worth trying this week", "article_ids": [5]}
  ],
  "recommendations": [
    {"text": "How a specific article/tool/release could improve your current stack — reference which part of the stack it affects and why", "article_ids": [8]}
  ],
  "sections": [
    {
      "category": "releases",
      "display_name": "Descriptive Section Title",
      "synthesis": "2-3 paragraph synthesis",
      "opposing_views": "Tensions between sources, or 'None noted'",
      "fact_check": "Speculation vs facts, or 'All statements fact-based'",
      "sources": ["Source1", "Source2"],
      "article_ids": [3, 7, 12],
      "high_value": true
    }
  ]
}

**category keys:** releases, tutorials, research, showcase, industry
**high_value:** true for sections with immediate practical value or strategic significance.
**display_name:** Descriptive titles that reflect actual content (e.g. "Claude 5 Launch & What Changed" not "Releases & Tools").
**try_this:** 2-4 concrete actionable items from across all sections. Each should be something the reader can do THIS WEEK.
**recommendations:** 2-5 personalized, ELI5-style suggestions. Write each one as a simple two-part structure: WHAT TO DO (one concrete sentence — a command to run, a repo to clone, a setting to change, a tool to install) + WHY IT HELPS YOU (one sentence connecting it to the reader's actual daily work). No jargon, no hedging, no "consider" or "could potentially". Talk like a colleague tapping you on the shoulder: "Hey, try this — it'll fix that thing that annoys you." Examples of good recommendations: "Install the new FastMCP 2.0 — it auto-generates tool schemas from type hints, which would cut boilerplate in your news-reader and second-brain MCP servers by half." / "Add --cache-prompt to your claude CLI calls in synthesizer.py — Anthropic just shipped prompt caching for Vertex, and your 50K-char synthesis prompts would hit cache on retry attempts." Bad: "You might want to explore the implications of the new caching feature for your infrastructure." Only include recommendations grounded in today's articles — never generic advice.

Return ONLY valid JSON. No preamble, no markdown formatting, no prose. Just the JSON object."""


def _build_article_entry(article: Any, index: int) -> dict:
    entry = {
        "id": index,
        "title": article.title,
        "source": article.source,
        "snippet": article.content[:300] if article.content else "",
        "language": article.language,
    }
    if article.published_at and article.fetched_at:
        entry["age_hours"] = round(
            (article.fetched_at - article.published_at).total_seconds() / 3600
        )
    return entry


def build_stack_prompt(
    articles: list[Any],
    previous_highlights: list[str] | None = None,
    time_window: str = "last 36 hours",
) -> str:
    article_entries = [_build_article_entry(article, i) for i, article in enumerate(articles)]

    context: dict[str, Any] = {
        "time_window": time_window,
        "total_articles": len(article_entries),
        "articles": article_entries,
    }
    if previous_highlights:
        context["previous_highlights"] = previous_highlights

    return f"""{_SYSTEM_PROMPT}

**CONTEXT:**
{json.dumps(context, ensure_ascii=False)}

**INSTRUCTIONS:**
Review all {len(article_entries)} articles. Select the most important ones, group into the 5 sections, and generate the JSON synthesis. Skip sections with nothing meaningful."""


def build_stack_fallback(articles: list[Any]) -> str:
    lines = ["STACK SYNTHESIS UNAVAILABLE", "", "Recent headlines:", ""]
    by_source: dict[str, list] = {}
    for article in articles:
        source = article.source
        if source not in by_source:
            by_source[source] = []
        by_source[source].append(article)

    for source, source_articles in sorted(by_source.items()):
        lines.append(f"## {source}")
        for article in source_articles[:5]:
            lines.append(f"- {article.title}")
            if article.url:
                lines.append(f"  {article.url}")
        lines.append("")

    return "\n".join(lines)


def synthesize_stack(
    articles: list[Any],
    previous_highlights: list[str] | None = None,
    time_window: str = "last 36 hours",
    max_retries: int = 2,
    timeout: int = 300,
    claude_command: str = "claude",
    claude_args: list[str] | None = None,
) -> tuple[dict[str, Any] | str, bool]:
    prompt = build_stack_prompt(articles, previous_highlights, time_window)
    logger.info(f"Stack prompt: {len(prompt)} chars for {len(articles)} articles")

    raw_output = invoke_claude(
        prompt,
        timeout=timeout,
        claude_command=claude_command,
        claude_args=claude_args,
    )

    if raw_output is None:
        logger.warning("Stack synthesis failed: no output")
        logger.error("All stack synthesis attempts failed, using fallback")
        fallback = build_stack_fallback(articles)
        return (fallback, False)

    parsed = parse_synthesis_output(raw_output)

    if "error" not in parsed:
        logger.info("Stack synthesis succeeded")
        return (parsed, True)

    logger.warning("Stack synthesis failed: parse error")
    logger.error("All stack synthesis attempts failed, using fallback")
    fallback = build_stack_fallback(articles)
    return (fallback, False)
