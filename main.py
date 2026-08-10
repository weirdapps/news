"""News Reader orchestrator - main entry point."""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Ensure imports work when called from cron
sys.path.insert(0, str(Path(__file__).resolve().parent))

from news.auth import check_gcloud_auth
from news.config import (
    VALID_PROFILES,
    get_categories,
    get_keywords,
    get_settings,
    get_sources,
)
from news.deliver import (
    build_alert_html,
    build_alert_subject,
    build_monitor_subject,
    build_stack_subject,
    build_subject,
    build_topic_subject,
    notify_macos,
    render_digest_html,
    render_monitor_html,
    render_stack_html,
    render_topic_html,
    save_fallback,
    send_email,
)
from news.fetcher import fetch_all_sources, fetch_rss_feeds
from news.llm_policy import (
    MAX_ATTEMPTS,
    PUSH_WAIT_SECONDS,
    ROW_CAPS,
    backoff,
    resolve_deadline,
)
from news.models import Digest
from news.monitor_synth import synthesize_monitor
from news.processor import process_articles
from news.storage import (
    get_article_by_hash,
    get_articles_since,
    get_connection,
    get_last_digest,
    init_db,
    insert_article,
    insert_digest,
    update_digest_sent,
)
from news.synthesizer import synthesize
from news.topic_synth import (
    build_google_news_url,
    build_topic_fallback,
    synthesize_topic,
)

# Constants
_ATHENS_TZ = ZoneInfo("Europe/Athens")
_PROJECT_ROOT = Path(__file__).resolve().parent
_DEFAULT_LOCK_PATH = _PROJECT_ROOT / "data" / "pipeline.lock"

# Shown in the failure-alert email when synthesis cannot be produced. We withhold
# the unsynthesized article dump and send this one-line notice instead.
_SYNTH_FAIL_REASON = (
    "AI synthesis could not be produced this run — the model call failed "
    "(e.g. an unrecoverable gcloud auth error after a re-auth attempt)."
)


def setup_logging() -> None:
    """Configure logging for the orchestrator."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def acquire_lock(lock_path: str) -> bool:
    """Acquire lock file. Returns True if acquired, False if already locked.

    Args:
        lock_path: Path to lock file

    Returns:
        True if lock acquired successfully, False if already locked
    """
    lock_file = Path(lock_path)

    # Check if lock exists and handle stale locks
    if lock_file.exists():
        try:
            pid = int(lock_file.read_text().strip())
            try:
                os.kill(pid, 0)  # Signal 0 just checks if process exists
                logging.warning(f"Lock held by process {pid}")
                return False
            except OSError:
                logging.info(f"Removing stale lock from process {pid}")
                lock_file.unlink()
        except (ValueError, FileNotFoundError):
            logging.warning("Invalid lock file, removing")
            lock_file.unlink(missing_ok=True)

    # Atomically create lock file with current PID
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        logging.warning("Lock file created by another process")
        return False
    logging.info(f"Lock acquired: {lock_path}")
    return True


def release_lock(lock_path: str) -> None:
    """Release lock file.

    Args:
        lock_path: Path to lock file
    """
    lock_file = Path(lock_path)
    lock_file.unlink(missing_ok=True)
    logging.info(f"Lock released: {lock_path}")


# Effective TimeoutStartSec of each scheduled news unit, read live from the VPS on
# 2026-08-10 with `systemctl --user show news-<profile>.service -p TimeoutStartUSec`.
# These four are the whole scheduled set; `topic` is run by hand and has no unit.
# A profile missing from this map gets no deadline rather than a guessed one.
_UNIT_TIMEOUT_SECONDS: dict[str, int] = {
    "digest": 2400,
    "monitor": 600,
    "market": 600,
    "stack": 600,
}

# TimeoutStopSec on all four units, which is also the user manager's
# DefaultTimeoutStopUSec. Reserved out of the budget so a GIVE_UP still has room to
# render and send the per-slot alert email before systemd's SIGTERM.
_SHUTDOWN_GRACE_SECONDS = 90

# Largest single backoff decide() can actually emit: RATE_LIMIT at n=3, so 240s.
# Derived, not written down, so it tracks ROW_CAPS and MAX_ATTEMPTS. n >= 4 is
# unreachable because decide() tests `attempt.total >= MAX_ATTEMPTS` before it consults
# the row and total >= n always holds, which is why backoff()'s own 600s ceiling never
# applies. Reported in the startup log; deliberately NOT part of the reserve below.
_MAX_REACHABLE_BACKOFF_SECONDS = max(
    backoff(n, outcome) for outcome in ROW_CAPS for n in range(1, MAX_ATTEMPTS)
)


def _deadline_reserve_seconds(max_call_seconds: float) -> float:
    """Seconds held back from a unit's TimeoutStartSec. 390s on today's config.

    390 = max_call 300 + shutdown_grace 90. It is a function rather than a literal
    because max_call is each profile's own ``synthesis.timeout``: pinning 390 would
    silently go wrong the moment one profile's timeout differed from another's.

    WHY ``max_backoff`` IS ABSENT, and why restoring it would be a regression rather
    than a correction. Spec §8 writes the reserve as
    ``max_call_seconds + max_backoff + shutdown_grace``. That term is double-counted.
    ``decide()``'s budget test is FORWARD-LOOKING — ``now + sleep_s + max_call_seconds
    > deadline`` at llm_policy.py:278 — so it already refuses any backoff whose sleep
    plus the following call would not fit, and no backoff can push the loop past the
    deadline. Subtracting the maximum again charges for it twice. Literally, the
    reserve is 630s and the margin for news-monitor, news-market and news-stack is
    MINUS 30s, so the startup assertion below would refuse to run three of the four
    production units. Owner signed off on dropping it, 2026-08-10.

    ``max_call_seconds`` is NOT double-counted and must stay. ``invoke_claude`` calls
    ``_run_once()`` unconditionally at the top of its loop (synthesizer.py:366) with no
    deadline test before the FIRST call, so if fetching and tagging have eaten the
    budget that call still starts and can run its whole timeout past the deadline.
    This term is the only thing covering that hole.
    """
    return max_call_seconds + _SHUTDOWN_GRACE_SECONDS


def _llm_budget_seconds(
    profile: str,
    max_call_seconds: float,
    unit_timeout_seconds: float | None = None,
) -> float | None:
    """Seconds of LLM budget this profile's unit can fund. None if it has no unit.

    The reserve, and the reasoning behind which terms it does and does not contain,
    lives in ``_deadline_reserve_seconds``.

    Raises RuntimeError when the margin is not positive. A unit that cannot fund one
    worst-case call plus its shutdown grace cannot produce output at all, and an
    immediate loud failure — nonzero exit, which fires OnFailure=hc-fail@ — beats a
    SIGTERM later with no email. The threshold lands at 391s, which is where spec §8's
    own remedy (raise TimeoutStartSec, as it did for sb-calendar-sync) already applies.
    """
    if unit_timeout_seconds is None:
        unit_timeout_seconds = _UNIT_TIMEOUT_SECONDS.get(profile)
    if unit_timeout_seconds is None:
        return None

    budget = unit_timeout_seconds - _deadline_reserve_seconds(max_call_seconds)
    if budget <= 0:
        raise RuntimeError(
            f"profile '{profile}' cannot fund a single LLM call: TimeoutStartSec="
            f"{unit_timeout_seconds:g}s minus max_call={max_call_seconds:g}s minus "
            f"shutdown_grace={_SHUTDOWN_GRACE_SECONDS}s leaves {budget:g}s. Raise the "
            f"unit's TimeoutStartSec above "
            f"{_deadline_reserve_seconds(max_call_seconds):g}s, or lower "
            f"synthesis.timeout for this profile. Refusing to run."
        )
    return budget


def install_llm_deadline(profile: str, now: float | None = None) -> float | None:
    """Export PTS_LLM_DEADLINE for this run so the policy's budget test is real.

    Nothing in the estate exports this variable, so resolve_deadline() falls back to
    a flat ``now + 900`` for every profile — 1.5x the entire 600s budget of three of
    the four news units. Spec §5 rule 2 exists precisely to keep the retry loop inside
    its unit, and against that default it can only fire once the unit is already dead.
    The port made that consequential: it introduced real backoff sleeps where the old
    nested loops had none, so a persistent 429 now costs 428-780s of in-loop time
    against roughly 8s before.

    ``setdefault``, not assignment: an operator running the pipeline by hand, or a
    future runner that computes a better value, must still win.

    Wall clock, not monotonic. PTS_LLM_DEADLINE is an absolute POSIX time and
    resolve_deadline compares it against ``time.time()``; a monotonic value here would
    switch the whole mechanism off silently.
    """
    logger = logging.getLogger(__name__)
    max_call_seconds = float(get_settings(profile=profile).get("synthesis", {}).get("timeout", 300))

    budget = _llm_budget_seconds(profile, max_call_seconds)
    if budget is None:
        logger.info(
            "profile '%s' has no systemd unit; leaving PTS_LLM_DEADLINE unset "
            "(the policy applies its own default budget)",
            profile,
        )
        return None

    deadline = (time.time() if now is None else now) + budget
    os.environ.setdefault("PTS_LLM_DEADLINE", repr(deadline))
    installed = float(os.environ["PTS_LLM_DEADLINE"])
    if installed != deadline:
        # setdefault kept an inherited value. Report what is actually in effect:
        # announcing the computed budget here would describe a policy the run is
        # not running under.
        logger.info(
            "PTS_LLM_DEADLINE was already set to %g and is kept; the computed "
            "budget for '%s' would have been %gs.",
            installed,
            profile,
            budget,
        )
        return installed
    logger.info(
        "LLM budget for '%s': %gs (TimeoutStartSec=%gs - max_call=%gs - grace=%gs). "
        "Largest backoff the policy can emit is %gs.",
        profile,
        budget,
        _UNIT_TIMEOUT_SECONDS[profile],
        max_call_seconds,
        _SHUTDOWN_GRACE_SECONDS,
        _MAX_REACHABLE_BACKOFF_SECONDS,
    )
    return deadline


def _may_wait_for_token_push(max_call_seconds: float, now: float | None = None) -> bool:
    """True when this run's remaining budget can fund a wait for the Mac's token push.

    Deliberately the same test ``decide()`` applies before it returns WAIT_FOR_PUSH
    (``now + wait + max_call_seconds > deadline`` -> UNRECOVERABLE_AUTH), so the
    pre-flight and the reactive path inside the policy loop cannot disagree about what
    the budget affords.

    Derived from the budget, never from the profile's name. On today's numbers only
    news-digest passes — 2010s of budget against the 1020 + 300 the wait needs — while
    the three 600s units sit at 210s and never do. Written this way, changing a unit's
    TimeoutStartSec changes the behaviour automatically, where a hardcoded profile list
    would go stale and silently contradict ``_llm_budget_seconds``.

    Evaluated against the clock rather than granted once at startup, because fetching
    and tagging run first: a slow fetch can consume the room the wait needed, and the
    honest answer then is no.
    """
    now = time.time() if now is None else now
    return now + PUSH_WAIT_SECONDS + max_call_seconds <= resolve_deadline(now, os.environ)


def _preflight_auth_ok(synthesis_config: dict) -> bool:
    """Pre-flight gcloud check, permitting a token-push wait only if the budget funds it."""
    max_call_seconds = float(synthesis_config.get("timeout", 300))
    return check_gcloud_auth(may_wait_for_push=_may_wait_for_token_push(max_call_seconds))


def get_time_window(now: datetime, last_digest_at: datetime | None, tz_name: str) -> str:
    """Build human-readable time window string.

    Args:
        now: Current datetime (UTC)
        last_digest_at: Last digest timestamp (UTC) or None
        tz_name: Timezone name (e.g. "Europe/Athens")

    Returns:
        Formatted string like "09:00–13:00 Athens, April 5 2026"
    """
    tz = ZoneInfo(tz_name)
    now_local = now.astimezone(tz)

    if last_digest_at is None:
        # No previous digest, use generic window
        return f"recent news — {now_local.strftime('%B %-d %Y')}"

    last_local = last_digest_at.astimezone(tz)

    # Format: HH:MM–HH:MM Athens, Month D YYYY
    start_time = last_local.strftime("%H:%M")
    end_time = now_local.strftime("%H:%M")
    date_str = now_local.strftime("%B %-d %Y")

    return f"{start_time}–{end_time} Athens, {date_str}"


def get_next_digest_time(current_time_str: str, schedule: list[str]) -> str:
    """Find next digest time in schedule.

    Args:
        current_time_str: Current time as "HH:MM" string
        schedule: List of times like ["09:00", "13:00", "17:00", "21:00"]

    Returns:
        Next scheduled time string (wraps to first time if past last)
    """
    # Parse current time
    current_hour, current_minute = map(int, current_time_str.split(":"))
    current_minutes = current_hour * 60 + current_minute

    # Find next time
    for time_str in schedule:
        hour, minute = map(int, time_str.split(":"))
        minutes = hour * 60 + minute
        if minutes > current_minutes:
            return time_str

    # Wrap to first time (tomorrow)
    return schedule[0]


def log_run(
    log_path: str,
    run_type: str,
    article_count: int,
    new_count: int,
    synthesis_ok: bool,
    sent_ok: bool | None,
    duration_seconds: float,
) -> None:
    """Append run summary to log file.

    Args:
        log_path: Path to log file
        run_type: "scheduled" or "adhoc"
        article_count: Total articles processed
        new_count: New articles (not duplicates)
        synthesis_ok: Whether synthesis succeeded
        sent_ok: True=sent OK, False=send FAILED, None=no email (store-only run)
        duration_seconds: Total execution time
    """
    log_file = Path(log_path).expanduser()
    log_file.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC)
    timestamp = now.isoformat()

    synthesis_status = "synthesis OK" if synthesis_ok else "synthesis FAILED"
    if sent_ok is None:
        email_status = "no email"
    elif sent_ok:
        email_status = "sent OK"
    else:
        email_status = "send FAILED"

    line = (
        f"{timestamp} | {run_type} | "
        f"articles: {article_count} | new: {new_count} | "
        f"{synthesis_status} | {email_status} | "
        f"duration: {duration_seconds:.1f}s\n"
    )

    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line)


async def run_pipeline(
    run_type: str = "scheduled",
    profile: str = "digest",
    query: str | None = None,
    hours: int = 24,
    print_only: bool = False,
) -> None:
    """Execute the appropriate pipeline based on profile.

    Args:
        run_type: "scheduled" or "adhoc"
        profile: "digest", "monitor", or "topic"
        query: Free-text query (required for topic profile)
        hours: Time window in hours (topic profile only)
        print_only: If True, print HTML to stdout instead of emailing (topic only)
    """
    if profile == "monitor":
        await run_monitor_pipeline(run_type=run_type)
        return
    if profile == "topic":
        assert query is not None, "topic profile requires --query"
        await run_topic_pipeline(query=query, hours=hours, print_only=print_only)
        return
    if profile == "stack":
        await run_stack_pipeline(run_type=run_type)
        return
    if profile == "market":
        await run_market_pipeline(run_type=run_type)
        return
    await run_digest_pipeline(run_type=run_type)


def _setup_digest_pipeline(settings: dict, sources: dict):
    """Extract and prepare pipeline configuration."""
    pipeline_config = settings["pipeline"]
    email_config = settings["email"]
    storage_config = settings["storage"]
    schedule_config = settings["schedule"]
    synthesis_config = settings.get("synthesis", {})
    scoring_config = settings.get("scoring", {})

    db_path = Path(storage_config["db_path"]).expanduser()
    run_log_path = Path(storage_config["run_log_path"]).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    source_tiers = {source["name"]: source.get("tier", 2) for source in sources["rss_feeds"]}

    return {
        "pipeline": pipeline_config,
        "email": email_config,
        "schedule": schedule_config,
        "synthesis": synthesis_config,
        "scoring": scoring_config,
        "db_path": db_path,
        "run_log_path": run_log_path,
        "source_tiers": source_tiers,
    }


def _select_digest_articles(all_recent: list, pipeline_config: dict):
    """Select top articles for digest with per-source diversity cap."""
    max_digest = pipeline_config.get("max_digest_articles", 300)
    max_per_source = pipeline_config.get("max_articles_per_source", 20)

    all_recent.sort(key=lambda a: a.relevance_score, reverse=True)
    source_counts: dict[str, int] = {}
    capped_articles = []

    for article in all_recent:
        count = source_counts.get(article.source, 0)
        if count < max_per_source:
            capped_articles.append(article)
            source_counts[article.source] = count + 1
        if len(capped_articles) >= max_digest:
            break

    return capped_articles


async def run_digest_pipeline(run_type: str = "scheduled") -> None:
    """Execute the full news digest pipeline.

    Args:
        run_type: "scheduled" or "adhoc"
    """
    start_time = datetime.now(UTC)
    logger = logging.getLogger(__name__)
    logger.info(f"Starting {run_type} pipeline run")

    # Load configurations
    settings = get_settings()
    sources = get_sources()
    categories_config = get_categories()

    # Setup pipeline configuration and paths
    config = _setup_digest_pipeline(settings, sources)

    # Connect to database
    conn = get_connection(config["db_path"])
    init_db(conn)

    # Get last digest for time window calculation
    last_digest = get_last_digest(conn)
    last_digest_at = last_digest.created_at if last_digest else None

    # Calculate time window
    time_window = get_time_window(
        start_time,
        last_digest_at,
        config["schedule"]["timezone"],
    )
    logger.info(f"Time window: {time_window}")

    # Get previous highlights from last digest
    previous_highlights = []
    if last_digest and last_digest.synthesis_text:
        try:
            previous_synthesis = json.loads(last_digest.synthesis_text)
            if isinstance(previous_synthesis, dict):
                previous_highlights = previous_synthesis.get("executive_brief", [])
        except json.JSONDecodeError:
            pass

    # FETCH: Get articles from RSS feeds (+ any feed-less HTML sources)
    logger.info(f"Fetching {len(sources['rss_feeds'])} RSS feeds")
    raw_articles, fetch_errors = await fetch_all_sources(sources)
    logger.info(f"Fetched {len(raw_articles)} articles")

    if fetch_errors:
        logger.warning(f"Fetch errors: {len(fetch_errors)}")
        for error in fetch_errors[:5]:  # Log first 5
            logger.warning(f"  {error}")

    # PROCESS: Get existing hashes and process articles
    logger.info("Processing articles")
    existing_hashes = set()
    for article in raw_articles:
        article.compute_hash()
        if get_article_by_hash(conn, article.content_hash):
            existing_hashes.add(article.content_hash)

    processed_articles, process_stats = process_articles(
        articles=raw_articles,
        existing_hashes=existing_hashes,
        categories_config=categories_config,
        scoring_config=config["scoring"],
        source_tiers=config["source_tiers"],
        min_words=config["pipeline"]["min_article_length_words"],
        max_age_hours=config["pipeline"]["max_article_age_hours"],
    )

    logger.info(
        f"Processing complete: {process_stats['output_count']} new articles "
        f"({process_stats['duplicates']} duplicates, {process_stats['quality_dropped']} quality drops)"
    )

    # STORE: Insert new articles into database
    for article in processed_articles:
        insert_article(conn, article)

    # DIGEST POOL: Pull last 48h of articles from DB for synthesis
    digest_window = timedelta(hours=config["pipeline"].get("digest_window_hours", 48))
    digest_since = start_time - digest_window
    all_recent = get_articles_since(conn, digest_since, min_score=0)

    # Select top articles by relevance score, with per-source diversity limit
    capped_articles = _select_digest_articles(all_recent, config["pipeline"])

    logger.info(
        f"Digest pool: {len(all_recent)} articles from last "
        f"{config['pipeline'].get('digest_window_hours', 48)}h, "
        f"selected top {len(capped_articles)} by score"
    )

    # SYNTHESIZE: Check auth first — skip synthesis if expired (avoid wasted retries)
    auth_ok = _preflight_auth_ok(config["synthesis"])
    if auth_ok:
        synthesis_result, synthesis_ok = synthesize(
            articles=capped_articles,
            previous_highlights=previous_highlights,
            time_window=time_window,
            max_retries=config["synthesis"].get("max_retries", 2),
            timeout=config["synthesis"].get("timeout", 300),
            claude_command=config["synthesis"].get("claude_command", "claude"),
            claude_args=config["synthesis"].get("claude_args", []),
        )
    else:
        logger.warning("gcloud auth expired — skipping synthesis, using fallback")
        from news.synthesizer import build_fallback_digest

        synthesis_result = build_fallback_digest(capped_articles)
        synthesis_ok = False

    relevant_articles = all_recent

    # Prepare synthesis data for rendering
    synthesis_data: dict
    if synthesis_ok:
        # Contract: synthesize() returns dict on success, str on failure.
        assert isinstance(synthesis_result, dict)
        from news.citation_filter import (
            enrich_section_articles,
            filter_unsourced_bullets,
            filter_unsourced_sections,
        )

        synthesis_data = synthesis_result
        synthesis_data["executive_brief"] = filter_unsourced_bullets(
            synthesis_data.get("executive_brief", []), capped_articles
        )
        synthesis_data["what_changed"] = filter_unsourced_bullets(
            synthesis_data.get("what_changed", []), capped_articles
        )
        synthesis_data["sections"] = enrich_section_articles(
            filter_unsourced_sections(synthesis_data.get("sections", []), capped_articles),
            capped_articles,
        )
        synthesis_text = json.dumps(synthesis_data)
    else:
        # Fallback case - plain text
        assert isinstance(synthesis_result, str)
        synthesis_data = {"fallback_text": synthesis_result}
        synthesis_text = synthesis_result

    # Calculate next digest time
    now_athens = start_time.astimezone(_ATHENS_TZ)
    current_time_str = now_athens.strftime("%H:%M")
    next_digest = get_next_digest_time(current_time_str, config["schedule"]["runs"])

    # DELIVER: Render HTML and send email
    source_count = len(sources["rss_feeds"])
    time_display = now_athens.strftime("%H:%M")
    date_display = now_athens.strftime("%a %-d %b").lower()

    if synthesis_ok:
        subject = build_subject(
            dt=now_athens,
            is_adhoc=(run_type == "adhoc"),
            partial_sources=(len(fetch_errors) > 0),
            synthesis_failed=False,
            article_count=len(relevant_articles),
            source_count=source_count,
        )
        html_output = render_digest_html(
            synthesis=synthesis_data,
            article_count=len(relevant_articles),
            source_count=source_count,
            time_display=time_display,
            date_display=date_display,
            next_digest=next_digest,
            subject=subject,
        )
    else:
        # Synthesis failed — send a one-line alert, NOT the unsynthesized dump.
        subject = build_alert_subject("News Digest", now_athens)
        html_output = build_alert_html(
            label="News Digest",
            time_display=time_display,
            date_display=date_display,
            reason=_SYNTH_FAIL_REASON,
            next_run=next_digest,
        )

    # Send email
    email_sent = send_email(
        subject=subject,
        html_body=html_output,
        recipient=config["email"]["recipient"],
    )

    # Handle send failure
    if not email_sent:
        logger.error("Email send failed - saving fallback and notifying")
        fallback_path = save_fallback(html_output)
        notify_macos(
            title="News Digest Send Failed",
            message=f"Saved to {fallback_path}",
        )

    # Record digest in database
    digest = Digest(
        digest_type=run_type,
        created_at=start_time,
        article_count=len(relevant_articles),
        synthesis_text=synthesis_text,
        html_output=html_output,
        sent_at=None,
    )
    digest_id = insert_digest(conn, digest)

    if email_sent:
        update_digest_sent(conn, digest_id)

    # Close database
    conn.close()

    # Log run
    duration = (datetime.now(UTC) - start_time).total_seconds()
    log_run(
        log_path=str(config["run_log_path"]),
        run_type=run_type,
        article_count=len(processed_articles),
        new_count=process_stats["output_count"],
        synthesis_ok=synthesis_ok,
        sent_ok=email_sent,
        duration_seconds=duration,
    )

    logger.info(f"Pipeline complete in {duration:.1f}s")


async def run_monitor_pipeline(run_type: str = "scheduled") -> None:
    """Execute the brand monitoring pipeline.

    Args:
        run_type: "scheduled" or "adhoc"
    """
    start_time = datetime.now(UTC)
    logger = logging.getLogger(__name__)
    logger.info(f"Starting monitor {run_type} pipeline run")

    # Load monitor-specific configurations
    settings = get_settings(profile="monitor")
    sources = get_sources(profile="monitor")
    keywords_config = get_keywords(profile="monitor")

    # Setup pipeline configuration and paths
    config = _setup_digest_pipeline(settings, sources)

    # Connect to database
    conn = get_connection(config["db_path"])
    init_db(conn)

    # Get last monitor digest for continuity
    last_digest = get_last_digest(conn, pipeline="monitor")
    last_digest_at = last_digest.created_at if last_digest else None

    # Calculate time window
    time_window = get_time_window(
        start_time,
        last_digest_at,
        config["schedule"]["timezone"],
    )
    logger.info(f"Monitor time window: {time_window}")

    # Get previous synthesis for trend comparison
    previous_summary = None
    if last_digest and last_digest.synthesis_text:
        try:
            previous_summary = json.loads(last_digest.synthesis_text)
        except json.JSONDecodeError:
            pass

    # FETCH: Get articles from monitor RSS feeds (+ any feed-less HTML sources)
    rss_feeds = sources.get("rss_feeds", [])
    logger.info(f"Fetching {len(rss_feeds)} monitor RSS feeds")
    raw_articles, fetch_errors = await fetch_all_sources(sources)
    logger.info(f"Fetched {len(raw_articles)} articles")

    if fetch_errors:
        logger.warning(f"Fetch errors: {len(fetch_errors)}")
        for error in fetch_errors[:5]:
            logger.warning(f"  {error}")

    # PROCESS: Dedup, classify, score
    existing_hashes = set()
    for article in raw_articles:
        article.pipeline = "monitor"
        article.compute_hash()
        if get_article_by_hash(conn, article.content_hash):
            existing_hashes.add(article.content_hash)

    # Use keywords config as categories for classification.
    # keywords_config is also threaded into compute_relevance_score so the
    # company/competitor bonuses match the configured names instead of a
    # hardcoded brand list.
    processed_articles, process_stats = process_articles(
        articles=raw_articles,
        existing_hashes=existing_hashes,
        categories_config=keywords_config,
        scoring_config=config["scoring"],
        source_tiers=config["source_tiers"],
        min_words=config["pipeline"]["min_article_length_words"],
        max_age_hours=config["pipeline"]["max_article_age_hours"],
        keywords_config=keywords_config,
    )

    # Set pipeline on all processed articles
    for article in processed_articles:
        article.pipeline = "monitor"

    logger.info(
        f"Processing complete: {process_stats['output_count']} new articles "
        f"({process_stats['duplicates']} duplicates, {process_stats['quality_dropped']} quality drops)"
    )

    # Check skip_empty setting
    skip_empty = config["pipeline"].get("skip_empty", True)
    if skip_empty and process_stats["output_count"] == 0 and run_type != "adhoc":
        logger.info("No new mentions — skipping synthesis and delivery")
        conn.close()
        duration = (datetime.now(UTC) - start_time).total_seconds()
        log_run(
            log_path=str(config["run_log_path"]),
            run_type=f"monitor-{run_type}",
            article_count=0,
            new_count=0,
            synthesis_ok=True,
            sent_ok=True,
            duration_seconds=duration,
        )
        return

    # STORE: Insert new articles
    for article in processed_articles:
        insert_article(conn, article)

    # DIGEST POOL: Pull recent monitor articles
    digest_window = timedelta(hours=config["pipeline"].get("digest_window_hours", 24))
    digest_since = start_time - digest_window
    all_recent = get_articles_since(conn, digest_since, min_score=0, pipeline="monitor")

    # Select top articles
    capped_articles = _select_digest_articles(all_recent, config["pipeline"])

    logger.info(f"Monitor pool: {len(all_recent)} articles, selected top {len(capped_articles)}")

    # SYNTHESIZE: Check auth first — skip synthesis if expired (avoid wasted retries)
    auth_ok = _preflight_auth_ok(config["synthesis"])
    if auth_ok:
        synthesis_result, synthesis_ok = synthesize_monitor(
            articles=capped_articles,
            keywords_config=keywords_config,
            previous_summary=previous_summary,
            time_window=time_window,
            last_run_at=last_digest_at,
            max_retries=config["synthesis"].get("max_retries", 2),
            timeout=config["synthesis"].get("timeout", 300),
            claude_command=config["synthesis"].get("claude_command", "claude"),
            claude_args=config["synthesis"].get("claude_args", []),
        )
    else:
        logger.warning("gcloud auth expired — skipping monitor synthesis, using fallback")
        from news.monitor_synth import build_monitor_fallback

        synthesis_result = build_monitor_fallback(capped_articles)
        synthesis_ok = False

    # Prepare synthesis data
    synthesis_data: dict
    if synthesis_ok:
        # Contract: synthesize_monitor() returns dict on success, str on failure.
        assert isinstance(synthesis_result, dict)
        from news.citation_filter import (
            enrich_mentions,
            filter_competitor_watch,
            filter_unsourced_bullets,
        )

        synthesis_data = synthesis_result
        synthesis_data["executive_brief"] = filter_unsourced_bullets(
            synthesis_data.get("executive_brief", []), capped_articles
        )
        synthesis_data["alerts"] = filter_unsourced_bullets(
            synthesis_data.get("alerts", []), capped_articles
        )
        synthesis_data["company_mentions"] = enrich_mentions(
            synthesis_data.get("company_mentions", []), capped_articles
        )
        synthesis_data["competitor_watch"] = filter_competitor_watch(
            synthesis_data.get("competitor_watch"), capped_articles
        )
        synthesis_data["mention_count"] = len(synthesis_data["company_mentions"])
        synthesis_text = json.dumps(synthesis_data)
    else:
        assert isinstance(synthesis_result, str)
        synthesis_data = {"fallback_text": synthesis_result}
        synthesis_text = synthesis_result

    # Calculate next scan time
    now_athens = start_time.astimezone(_ATHENS_TZ)
    current_time_str = now_athens.strftime("%H:%M")
    next_scan = get_next_digest_time(current_time_str, config["schedule"]["runs"])

    # DELIVER: Render monitor HTML and send email
    source_count = len(rss_feeds)
    time_display = now_athens.strftime("%H:%M")
    date_display = now_athens.strftime("%a %-d %b").lower()

    mention_count = (
        len(synthesis_data.get("company_mentions", [])) if synthesis_ok else len(capped_articles)
    )
    has_alerts = bool(synthesis_data.get("alerts")) if synthesis_ok else False

    if synthesis_ok:
        subject = build_monitor_subject(
            dt=now_athens,
            keywords_config=keywords_config,
            is_adhoc=(run_type == "adhoc"),
            mention_count=mention_count,
            source_count=source_count,
            has_alerts=has_alerts,
            synthesis_failed=False,
        )
        html_output = render_monitor_html(
            synthesis=synthesis_data,
            mention_count=mention_count,
            source_count=source_count,
            time_display=time_display,
            date_display=date_display,
            keywords_config=keywords_config,
            next_scan=next_scan,
            subject=subject,
        )
    else:
        # Synthesis failed — send a one-line alert, NOT the unsynthesized dump.
        monitor_label = keywords_config.get("display", {}).get("monitor_label", "Brand Monitor")
        subject = build_alert_subject(monitor_label, now_athens)
        html_output = build_alert_html(
            label=monitor_label,
            time_display=time_display,
            date_display=date_display,
            reason=_SYNTH_FAIL_REASON,
            next_run=next_scan,
        )

    # Send email
    email_sent = send_email(
        subject=subject,
        html_body=html_output,
        recipient=config["email"]["recipient"],
    )

    if not email_sent:
        logger.error("Monitor email send failed - saving fallback")
        fallback_path = save_fallback(html_output, label="monitor")
        notify_macos(
            title="Monitor Send Failed",
            message=f"Saved to {fallback_path}",
        )

    # Record monitor digest in database
    digest = Digest(
        digest_type=run_type,
        created_at=start_time,
        article_count=len(capped_articles),
        synthesis_text=synthesis_text,
        html_output=html_output,
        sent_at=None,
        pipeline="monitor",
    )
    digest_id = insert_digest(conn, digest)

    if email_sent:
        update_digest_sent(conn, digest_id)

    conn.close()

    # Log run
    duration = (datetime.now(UTC) - start_time).total_seconds()
    log_run(
        log_path=str(config["run_log_path"]),
        run_type=f"monitor-{run_type}",
        article_count=len(processed_articles),
        new_count=process_stats["output_count"],
        synthesis_ok=synthesis_ok,
        sent_ok=email_sent,
        duration_seconds=duration,
    )

    logger.info(f"Monitor pipeline complete in {duration:.1f}s")


async def run_stack_pipeline(run_type: str = "scheduled") -> None:
    """Execute the stack (AI/dev intelligence) pipeline."""
    start_time = datetime.now(UTC)
    logger = logging.getLogger(__name__)
    logger.info(f"Starting stack {run_type} pipeline run")

    settings = get_settings(profile="stack")
    sources = get_sources(profile="stack")
    categories_config = get_categories(profile="stack")

    config = _setup_digest_pipeline(settings, sources)

    conn = get_connection(config["db_path"])
    init_db(conn)

    last_digest = get_last_digest(conn, pipeline="stack")
    last_digest_at = last_digest.created_at if last_digest else None

    time_window = get_time_window(
        start_time,
        last_digest_at,
        config["schedule"]["timezone"],
    )
    logger.info(f"Stack time window: {time_window}")

    previous_highlights: list[str] = []
    if last_digest and last_digest.synthesis_text:
        try:
            previous_synthesis = json.loads(last_digest.synthesis_text)
            if isinstance(previous_synthesis, dict):
                previous_highlights = previous_synthesis.get("executive_brief", [])
        except json.JSONDecodeError:
            pass

    # FETCH (RSS feeds + any feed-less HTML sources)
    rss_feeds = sources.get("rss_feeds", [])
    logger.info(f"Fetching {len(rss_feeds)} stack RSS feeds")
    raw_articles, fetch_errors = await fetch_all_sources(sources)
    logger.info(f"Fetched {len(raw_articles)} articles")

    if fetch_errors:
        logger.warning(f"Fetch errors: {len(fetch_errors)}")
        for error in fetch_errors[:5]:
            logger.warning(f"  {error}")

    # PROCESS
    existing_hashes: set[str] = set()
    for article in raw_articles:
        article.pipeline = "stack"
        article.compute_hash()
        if get_article_by_hash(conn, article.content_hash):
            existing_hashes.add(article.content_hash)

    processed_articles, process_stats = process_articles(
        articles=raw_articles,
        existing_hashes=existing_hashes,
        categories_config=categories_config,
        scoring_config=config["scoring"],
        source_tiers=config["source_tiers"],
        min_words=config["pipeline"]["min_article_length_words"],
        max_age_hours=config["pipeline"]["max_article_age_hours"],
    )

    for article in processed_articles:
        article.pipeline = "stack"

    logger.info(
        f"Processing complete: {process_stats['output_count']} new articles "
        f"({process_stats['duplicates']} duplicates, "
        f"{process_stats['quality_dropped']} quality drops)"
    )

    # STORE
    for article in processed_articles:
        insert_article(conn, article)

    # POOL: pull recent stack articles
    digest_window = timedelta(hours=config["pipeline"].get("digest_window_hours", 36))
    digest_since = start_time - digest_window
    all_recent = get_articles_since(conn, digest_since, min_score=0, pipeline="stack")

    capped_articles = _select_digest_articles(all_recent, config["pipeline"])
    logger.info(f"Stack pool: {len(all_recent)} articles, selected top {len(capped_articles)}")

    # SYNTHESIZE
    from news.stack_synth import build_stack_fallback, synthesize_stack

    auth_ok = _preflight_auth_ok(config["synthesis"])
    if auth_ok and capped_articles:
        synthesis_result, synthesis_ok = synthesize_stack(
            articles=capped_articles,
            previous_highlights=previous_highlights,
            time_window=time_window,
            max_retries=config["synthesis"].get("max_retries", 2),
            timeout=config["synthesis"].get("timeout", 300),
            claude_command=config["synthesis"].get("claude_command", "claude"),
            claude_args=config["synthesis"].get("claude_args", []),
        )
    else:
        if not auth_ok:
            logger.warning("gcloud auth expired — skipping synthesis, using fallback")
        elif not capped_articles:
            logger.warning("No articles fetched — using fallback")
        synthesis_result = build_stack_fallback(capped_articles)
        synthesis_ok = False

    # Prepare synthesis data
    synthesis_data: dict
    if synthesis_ok:
        assert isinstance(synthesis_result, dict)
        from news.citation_filter import (
            filter_unsourced_bullets,
            filter_unsourced_sections,
        )

        synthesis_data = synthesis_result
        synthesis_data["executive_brief"] = filter_unsourced_bullets(
            synthesis_data.get("executive_brief", []), capped_articles
        )
        synthesis_data["try_this"] = filter_unsourced_bullets(
            synthesis_data.get("try_this", []), capped_articles
        )
        synthesis_data["recommendations"] = filter_unsourced_bullets(
            synthesis_data.get("recommendations", []), capped_articles
        )
        synthesis_data["sections"] = filter_unsourced_sections(
            synthesis_data.get("sections", []), capped_articles
        )
        synthesis_text = json.dumps(synthesis_data)
    else:
        assert isinstance(synthesis_result, str)
        synthesis_data = {"fallback_text": synthesis_result}
        synthesis_text = synthesis_result

    # DELIVER
    now_athens = start_time.astimezone(_ATHENS_TZ)
    current_time_str = now_athens.strftime("%H:%M")
    next_run = get_next_digest_time(current_time_str, config["schedule"]["runs"])

    source_count = len(rss_feeds)
    time_display = now_athens.strftime("%H:%M")
    date_display = now_athens.strftime("%a %-d %b").lower()

    if synthesis_ok:
        subject = build_stack_subject(
            dt=now_athens,
            is_adhoc=(run_type == "adhoc"),
            synthesis_failed=False,
            article_count=len(all_recent),
            source_count=source_count,
        )
        html_output = render_stack_html(
            synthesis=synthesis_data,
            article_count=len(all_recent),
            source_count=source_count,
            time_display=time_display,
            date_display=date_display,
            next_run=next_run,
            subject=subject,
        )
    else:
        # Synthesis failed — send a one-line alert, NOT the unsynthesized dump.
        subject = build_alert_subject("Stack", now_athens)
        html_output = build_alert_html(
            label="Stack",
            time_display=time_display,
            date_display=date_display,
            reason=_SYNTH_FAIL_REASON,
            next_run=next_run,
        )

    email_sent = send_email(
        subject=subject,
        html_body=html_output,
        recipient=config["email"]["recipient"],
    )

    if not email_sent:
        logger.error("Stack email send failed - saving fallback")
        fallback_path = save_fallback(html_output, label="stack")
        notify_macos(
            title="Stack Send Failed",
            message=f"Saved to {fallback_path}",
        )

    digest = Digest(
        digest_type=run_type,
        created_at=start_time,
        article_count=len(all_recent),
        synthesis_text=synthesis_text,
        html_output=html_output,
        sent_at=None,
        pipeline="stack",
    )
    digest_id = insert_digest(conn, digest)

    if email_sent:
        update_digest_sent(conn, digest_id)

    conn.close()

    duration = (datetime.now(UTC) - start_time).total_seconds()
    log_run(
        log_path=str(config["run_log_path"]),
        run_type=f"stack-{run_type}",
        article_count=len(processed_articles),
        new_count=process_stats["output_count"],
        synthesis_ok=synthesis_ok,
        sent_ok=email_sent,
        duration_seconds=duration,
    )

    logger.info(f"Stack pipeline complete in {duration:.1f}s")


async def run_market_pipeline(run_type: str = "scheduled") -> None:
    """Execute the market (market-moving news) pipeline — STORE-ONLY.

    Fetches + tags + synthesizes market-moving news and persists it to news.db
    (pipeline='market'). The trading Investment Brief consumes it via
    recent_for_tickers (holdings) + digest_history(pipeline='market'). No email
    is sent by design; the brief is the consumer.
    """
    start_time = datetime.now(UTC)
    logger = logging.getLogger(__name__)
    logger.info(f"Starting market {run_type} pipeline run")

    settings = get_settings(profile="market")
    sources = get_sources(profile="market")
    categories_config = get_categories(profile="market")

    config = _setup_digest_pipeline(settings, sources)

    conn = get_connection(config["db_path"])
    init_db(conn)

    last_digest = get_last_digest(conn, pipeline="market")
    last_digest_at = last_digest.created_at if last_digest else None

    time_window = get_time_window(
        start_time,
        last_digest_at,
        config["schedule"]["timezone"],
    )
    logger.info(f"Market time window: {time_window}")

    previous_highlights: list[str] = []
    if last_digest and last_digest.synthesis_text:
        try:
            previous_synthesis = json.loads(last_digest.synthesis_text)
            if isinstance(previous_synthesis, dict):
                previous_highlights = previous_synthesis.get("executive_brief", [])
        except json.JSONDecodeError:
            pass

    # FETCH (RSS feeds + any feed-less HTML sources)
    rss_feeds = sources.get("rss_feeds", [])
    logger.info(f"Fetching {len(rss_feeds)} market RSS feeds")
    raw_articles, fetch_errors = await fetch_all_sources(sources)
    logger.info(f"Fetched {len(raw_articles)} articles")

    if fetch_errors:
        logger.warning(f"Fetch errors: {len(fetch_errors)}")
        for error in fetch_errors[:5]:
            logger.warning(f"  {error}")

    # PROCESS
    existing_hashes: set[str] = set()
    for article in raw_articles:
        article.pipeline = "market"
        article.compute_hash()
        if get_article_by_hash(conn, article.content_hash):
            existing_hashes.add(article.content_hash)

    processed_articles, process_stats = process_articles(
        articles=raw_articles,
        existing_hashes=existing_hashes,
        categories_config=categories_config,
        scoring_config=config["scoring"],
        source_tiers=config["source_tiers"],
        min_words=config["pipeline"]["min_article_length_words"],
        max_age_hours=config["pipeline"]["max_article_age_hours"],
    )

    for article in processed_articles:
        article.pipeline = "market"

    logger.info(
        f"Processing complete: {process_stats['output_count']} new articles "
        f"({process_stats['duplicates']} duplicates, "
        f"{process_stats['quality_dropped']} quality drops)"
    )

    # STORE
    for article in processed_articles:
        insert_article(conn, article)

    # POOL: pull recent market articles
    digest_window = timedelta(hours=config["pipeline"].get("digest_window_hours", 18))
    digest_since = start_time - digest_window
    all_recent = get_articles_since(conn, digest_since, min_score=0, pipeline="market")

    capped_articles = _select_digest_articles(all_recent, config["pipeline"])
    logger.info(f"Market pool: {len(all_recent)} articles, selected top {len(capped_articles)}")

    # SYNTHESIZE
    from news.market_synth import build_market_fallback, synthesize_market

    auth_ok = _preflight_auth_ok(config["synthesis"])
    if auth_ok and capped_articles:
        synthesis_result, synthesis_ok = synthesize_market(
            articles=capped_articles,
            previous_highlights=previous_highlights,
            time_window=time_window,
            max_retries=config["synthesis"].get("max_retries", 2),
            timeout=config["synthesis"].get("timeout", 300),
            claude_command=config["synthesis"].get("claude_command", "claude"),
            claude_args=config["synthesis"].get("claude_args", []),
        )
    else:
        if not auth_ok:
            logger.warning("gcloud auth expired — skipping synthesis, using fallback")
        elif not capped_articles:
            logger.warning("No articles fetched — using fallback")
        synthesis_result = build_market_fallback(capped_articles)
        synthesis_ok = False

    if synthesis_ok:
        assert isinstance(synthesis_result, dict)
        from news.citation_filter import (
            filter_unsourced_bullets,
            filter_unsourced_sections,
        )

        synthesis_data = synthesis_result
        synthesis_data["executive_brief"] = filter_unsourced_bullets(
            synthesis_data.get("executive_brief", []), capped_articles
        )
        synthesis_data["sections"] = filter_unsourced_sections(
            synthesis_data.get("sections", []), capped_articles
        )
        synthesis_text = json.dumps(synthesis_data)
    else:
        assert isinstance(synthesis_result, str)
        synthesis_text = synthesis_result

    # STORE DIGEST (store-only — the Investment Brief consumes this; no email)
    digest = Digest(
        digest_type=run_type,
        created_at=start_time,
        article_count=len(all_recent),
        synthesis_text=synthesis_text,
        html_output="",
        sent_at=None,
        pipeline="market",
    )
    insert_digest(conn, digest)
    conn.close()

    duration = (datetime.now(UTC) - start_time).total_seconds()
    # Market is store-only by default. NEWS_MARKET_RECIPIENT opts in to a standalone
    # email. "Not configured" is detected by the env var being absent rather than by
    # comparing against the YAML placeholder string ("user@example.com"), which would
    # break if someone happened to set the var to the placeholder value.
    _market_recipient = os.environ.get("NEWS_MARKET_RECIPIENT")
    # sent_ok=None signals "no email" to log_run (store-only run, no attempt made).
    # sent_ok=False would log "send FAILED", implying an attempt was made and failed.
    log_run(
        log_path=str(config["run_log_path"]),
        run_type=f"market-{run_type}",
        article_count=len(processed_articles),
        new_count=process_stats["output_count"],
        synthesis_ok=synthesis_ok,
        sent_ok=None if not _market_recipient else False,
        duration_seconds=duration,
    )

    logger.info(f"Market pipeline complete in {duration:.1f}s")


async def run_topic_pipeline(
    query: str,
    hours: int = 24,
    print_only: bool = False,
) -> None:
    """Execute the ad-hoc topic-brief pipeline.

    Args:
        query: User's free-text topic query
        hours: Time window in hours (1–168)
        print_only: If True, print rendered HTML to stdout instead of emailing
    """
    start_time = datetime.now(UTC)
    logger = logging.getLogger(__name__)
    logger.info(f"Starting topic pipeline run | query={query!r} hours={hours}")

    # Load topic-specific configuration
    settings = get_settings(profile="topic")
    pipeline_config = settings["pipeline"]
    email_config = settings["email"]
    storage_config = settings["storage"]
    synthesis_config = settings.get("synthesis", {})
    scoring_config = settings.get("scoring", {})

    db_path = Path(storage_config["db_path"]).expanduser()
    run_log_path = Path(storage_config["run_log_path"]).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Connect to database (shared with digest/monitor)
    conn = get_connection(db_path)
    init_db(conn)

    # FETCH: build a single Google News RSS source dict and fetch via existing helper
    google_url = build_google_news_url(query, hours=hours)
    source_name = f"Google News: {query[:60]}"
    source_tier = 2
    source_config: dict[str, Any] = {
        "url": google_url,
        "name": source_name,
        "category": "topic",
        "tier": source_tier,
        "language": "en",
    }
    logger.info(f"Fetching Google News RSS for topic | url={google_url}")
    raw_articles, fetch_errors = await fetch_rss_feeds([source_config])
    logger.info(f"Fetched {len(raw_articles)} articles")
    if fetch_errors:
        for error in fetch_errors:
            logger.warning(f"  fetch error: {error}")

    # PROCESS: dedup + quality filter only — no categorization (no relevant categories)
    existing_hashes: set[str] = set()
    for article in raw_articles:
        article.pipeline = "topic"
        article.compute_hash()
        if get_article_by_hash(conn, article.content_hash):
            existing_hashes.add(article.content_hash)

    processed_articles, process_stats = process_articles(
        articles=raw_articles,
        existing_hashes=existing_hashes,
        categories_config={"categories": {}},  # no categories for ad-hoc topic
        scoring_config=scoring_config,
        source_tiers={source_name: source_tier},
        min_words=pipeline_config["min_article_length_words"],
        max_age_hours=pipeline_config["max_article_age_hours"],
    )

    for article in processed_articles:
        article.pipeline = "topic"

    logger.info(
        f"Processing complete: {process_stats['output_count']} new "
        f"({process_stats['duplicates']} duplicates, "
        f"{process_stats['quality_dropped']} quality drops)"
    )

    # STORE: insert new articles
    for article in processed_articles:
        insert_article(conn, article)

    # SELECT: top N by score for synthesis input
    max_articles = pipeline_config.get("max_digest_articles", 100)
    candidates = sorted(processed_articles, key=lambda a: a.relevance_score, reverse=True)[
        :max_articles
    ]

    # SYNTHESIZE
    auth_ok = _preflight_auth_ok(synthesis_config)
    if auth_ok and candidates:
        synthesis_result, synthesis_ok = synthesize_topic(
            articles=candidates,
            query=query,
            hours=hours,
            max_retries=synthesis_config.get("max_retries", 2),
            timeout=synthesis_config.get("timeout", 300),
            claude_command=synthesis_config.get("claude_command", "claude"),
            claude_args=synthesis_config.get("claude_args", []),
        )
    else:
        if not auth_ok:
            logger.warning("gcloud auth expired — skipping synthesis, using fallback")
        elif not candidates:
            logger.warning("No articles fetched — using fallback")
        synthesis_result = build_topic_fallback(candidates)
        synthesis_ok = False

    synthesis_data: dict
    if synthesis_ok:
        assert isinstance(synthesis_result, dict)
        synthesis_data = synthesis_result
        synthesis_text = json.dumps(synthesis_result)
    else:
        assert isinstance(synthesis_result, str)
        synthesis_data = {"fallback_text": synthesis_result}
        synthesis_text = synthesis_result

    # DELIVER: render HTML; either print or email
    now_athens = start_time.astimezone(_ATHENS_TZ)
    time_display = now_athens.strftime("%H:%M")
    date_display = now_athens.strftime("%a %-d %b").lower()
    source_count = len(candidates)

    subject = build_topic_subject(
        dt=now_athens,
        query=query,
        is_adhoc=True,
        synthesis_failed=(not synthesis_ok),
    )

    html_output = render_topic_html(
        synthesis=synthesis_data,
        query=query,
        hours=hours,
        source_count=source_count,
        time_display=time_display,
        date_display=date_display,
        subject=subject,
    )

    email_sent = False
    if print_only:
        print(html_output)
        logger.info("Printed topic brief to stdout (--print)")
    else:
        email_sent = send_email(
            subject=subject,
            html_body=html_output,
            recipient=email_config["recipient"],
        )
        if not email_sent:
            logger.error("Topic email send failed - saving fallback")
            fallback_path = save_fallback(html_output, label="topic")
            notify_macos(
                title="Topic Brief Send Failed",
                message=f"Saved to {fallback_path}",
            )

    # Record topic digest in database
    digest = Digest(
        digest_type="adhoc",
        created_at=start_time,
        article_count=source_count,
        synthesis_text=synthesis_text,
        html_output=html_output,
        sent_at=None,
        pipeline="topic",
    )
    digest_id = insert_digest(conn, digest)

    if email_sent:
        update_digest_sent(conn, digest_id)

    conn.close()

    duration = (datetime.now(UTC) - start_time).total_seconds()
    log_run(
        log_path=str(run_log_path),
        run_type="topic-adhoc",
        article_count=len(processed_articles),
        new_count=process_stats["output_count"],
        synthesis_ok=synthesis_ok,
        sent_ok=(email_sent or print_only),
        duration_seconds=duration,
    )

    logger.info(f"Topic pipeline complete in {duration:.1f}s")


def main() -> None:
    """Main entry point with CLI parsing and lock management."""
    setup_logging()
    logger = logging.getLogger(__name__)

    # Parse arguments
    parser = argparse.ArgumentParser(description="News Reader Pipeline")
    parser.add_argument(
        "--scheduled",
        action="store_const",
        const="scheduled",
        dest="run_type",
        help="Scheduled run (default)",
    )
    parser.add_argument(
        "--adhoc",
        action="store_const",
        const="adhoc",
        dest="run_type",
        help="Ad-hoc run",
    )
    parser.add_argument(
        "--profile",
        choices=VALID_PROFILES,
        default="digest",
        help=(
            "Pipeline profile: 'digest' (default), 'monitor' (brand monitoring), "
            "'stack' (AI/dev intelligence), or 'topic' (ad-hoc topical brief — requires --query)"
        ),
    )
    parser.add_argument(
        "--query",
        type=str,
        default=None,
        help="Free-text topic query (REQUIRED with --profile topic)",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=24,
        help="Time window in hours for --profile topic (1–168, default 24)",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        dest="print_only",
        help="Print rendered HTML to stdout instead of emailing (topic profile)",
    )
    parser.set_defaults(run_type="scheduled")

    args = parser.parse_args()

    # Validation: --query and --profile topic are mutually required.
    # These errors must be raised BEFORE lock acquisition so they exit cleanly.
    if args.profile == "topic" and not args.query:
        parser.error("--profile topic requires --query 'your topic'")
    if args.query is not None and args.profile != "topic":
        parser.error("--query is only valid with --profile topic")
    if args.profile == "topic" and not (1 <= args.hours <= 168):
        parser.error("--hours must be between 1 and 168 (1 week max)")

    # Bound the policy loop to this unit's budget BEFORE any LLM call. Ahead of the
    # lock, so a refusal exits without having taken one.
    try:
        install_llm_deadline(args.profile)
    except RuntimeError as e:
        logger.error(str(e))
        sys.exit(1)

    # Use profile-specific lock to allow digest and monitor to run concurrently
    lock_path = _PROJECT_ROOT / "data" / f"pipeline-{args.profile}.lock"

    # Acquire lock
    if not acquire_lock(str(lock_path)):
        logger.error(f"Failed to acquire lock for {args.profile} - another instance running?")
        sys.exit(1)

    try:
        # Run pipeline
        asyncio.run(
            run_pipeline(
                run_type=args.run_type,
                profile=args.profile,
                query=args.query,
                hours=args.hours,
                print_only=args.print_only,
            )
        )
    except Exception as e:
        logger.error(f"Pipeline failed with exception: {e}", exc_info=True)
        sys.exit(1)
    finally:
        # Always release lock
        release_lock(str(lock_path))


if __name__ == "__main__":
    main()
