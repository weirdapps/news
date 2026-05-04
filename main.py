"""News Reader orchestrator - main entry point."""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    build_monitor_subject,
    build_subject,
    notify_macos,
    render_digest_html,
    render_monitor_html,
    save_fallback,
    send_email,
)
from news.fetcher import fetch_rss_feeds
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

# Constants
_ATHENS_TZ = ZoneInfo("Europe/Athens")
_PROJECT_ROOT = Path(__file__).resolve().parent
_DEFAULT_LOCK_PATH = _PROJECT_ROOT / "data" / "pipeline.lock"


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


def get_time_window(
    now: datetime, last_digest_at: datetime | None, tz_name: str
) -> str:
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
    sent_ok: bool,
    duration_seconds: float,
) -> None:
    """Append run summary to log file.

    Args:
        log_path: Path to log file
        run_type: "scheduled" or "adhoc"
        article_count: Total articles processed
        new_count: New articles (not duplicates)
        synthesis_ok: Whether synthesis succeeded
        sent_ok: Whether email sent successfully
        duration_seconds: Total execution time
    """
    log_file = Path(log_path).expanduser()
    log_file.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    timestamp = now.isoformat()

    synthesis_status = "synthesis OK" if synthesis_ok else "synthesis FAILED"
    email_status = "sent OK" if sent_ok else "send FAILED"

    line = (
        f"{timestamp} | {run_type} | "
        f"articles: {article_count} | new: {new_count} | "
        f"{synthesis_status} | {email_status} | "
        f"duration: {duration_seconds:.1f}s\n"
    )

    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line)


async def run_pipeline(run_type: str = "scheduled", profile: str = "digest") -> None:
    """Execute the appropriate pipeline based on profile.

    Args:
        run_type: "scheduled" or "adhoc"
        profile: "digest" or "monitor"
    """
    if profile == "monitor":
        await run_monitor_pipeline(run_type=run_type)
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

    source_tiers = {
        source["name"]: source.get("tier", 2) for source in sources["rss_feeds"]
    }

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
    start_time = datetime.now(timezone.utc)
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
            synthesis_data = json.loads(last_digest.synthesis_text)
            if isinstance(synthesis_data, dict):
                previous_highlights = synthesis_data.get("executive_brief", [])
        except json.JSONDecodeError:
            pass

    # FETCH: Get articles from RSS feeds
    logger.info(f"Fetching {len(sources['rss_feeds'])} RSS feeds")
    raw_articles, fetch_errors = await fetch_rss_feeds(sources["rss_feeds"])
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
    auth_ok = check_gcloud_auth()
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
    if synthesis_ok:
        synthesis_data = synthesis_result
        synthesis_text = json.dumps(synthesis_result)
    else:
        # Fallback case - plain text
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

    subject = build_subject(
        dt=now_athens,
        is_adhoc=(run_type == "adhoc"),
        partial_sources=(len(fetch_errors) > 0),
        synthesis_failed=(not synthesis_ok),
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
    duration = (datetime.now(timezone.utc) - start_time).total_seconds()
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
    start_time = datetime.now(timezone.utc)
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

    # FETCH: Get articles from monitor RSS feeds
    rss_feeds = sources.get("rss_feeds", [])
    logger.info(f"Fetching {len(rss_feeds)} monitor RSS feeds")
    raw_articles, fetch_errors = await fetch_rss_feeds(rss_feeds)
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
        duration = (datetime.now(timezone.utc) - start_time).total_seconds()
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

    logger.info(
        f"Monitor pool: {len(all_recent)} articles, selected top {len(capped_articles)}"
    )

    # SYNTHESIZE: Check auth first — skip synthesis if expired (avoid wasted retries)
    auth_ok = check_gcloud_auth()
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
        logger.warning(
            "gcloud auth expired — skipping monitor synthesis, using fallback"
        )
        from news.monitor_synth import build_monitor_fallback

        synthesis_result = build_monitor_fallback(capped_articles)
        synthesis_ok = False

    # Prepare synthesis data
    if synthesis_ok:
        synthesis_data = synthesis_result
        synthesis_text = json.dumps(synthesis_result)
    else:
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
        synthesis_data.get("mention_count", len(capped_articles))
        if synthesis_ok
        else len(capped_articles)
    )
    has_alerts = bool(synthesis_data.get("alerts")) if synthesis_ok else False

    subject = build_monitor_subject(
        dt=now_athens,
        keywords_config=keywords_config,
        is_adhoc=(run_type == "adhoc"),
        mention_count=mention_count,
        source_count=source_count,
        has_alerts=has_alerts,
        synthesis_failed=(not synthesis_ok),
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
    duration = (datetime.now(timezone.utc) - start_time).total_seconds()
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
        help="Pipeline profile: 'digest' (default) or 'monitor' (brand monitoring)",
    )
    parser.set_defaults(run_type="scheduled")

    args = parser.parse_args()

    # Use profile-specific lock to allow digest and monitor to run concurrently
    lock_path = _PROJECT_ROOT / "data" / f"pipeline-{args.profile}.lock"

    # Acquire lock
    if not acquire_lock(str(lock_path)):
        logger.error(
            f"Failed to acquire lock for {args.profile} - another instance running?"
        )
        sys.exit(1)

    try:
        # Run pipeline
        asyncio.run(run_pipeline(run_type=args.run_type, profile=args.profile))
    except Exception as e:
        logger.error(f"Pipeline failed with exception: {e}", exc_info=True)
        sys.exit(1)
    finally:
        # Always release lock
        release_lock(str(lock_path))


if __name__ == "__main__":
    main()
