"""Email delivery with Jinja2 rendering and Gmail sending."""

import logging
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

logger = logging.getLogger(__name__)

# Template directory relative to this file
_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
_ATHENS_TZ = ZoneInfo("Europe/Athens")


def render_digest_html(
    synthesis: dict,
    article_count: int,
    source_count: int,
    time_display: str,
    date_display: str,
    next_digest: str | None = None,
    subject: str = "",
) -> str:
    """Render the digest HTML using Jinja2 template.

    Args:
        synthesis: Parsed synthesis dict with executive_brief, what_changed, sections
        article_count: Total number of articles
        source_count: Total number of sources
        time_display: Time string (e.g. "09:00")
        date_display: Date string (e.g. "sat 5 apr")
        next_digest: Optional next digest time string
        subject: Email subject line

    Returns:
        Rendered HTML string
    """
    env = Environment(
        loader=FileSystemLoader(_TEMPLATES_DIR),
        autoescape=select_autoescape(default_for_string=True, default=True),
    )
    template = env.get_template("digest.html")

    # Extract data from synthesis
    executive_brief = synthesis.get("executive_brief", [])
    what_changed = synthesis.get("what_changed", "")
    sections = synthesis.get("sections", [])

    # Handle what_changed - convert string to list or use as-is if already list
    if isinstance(what_changed, str):
        what_changed = [what_changed] if what_changed else []

    # Check if synthesis failed (fallback text present)
    fallback_text = synthesis.get("fallback_text", "")

    # Pre-convert newlines to <br> in synthesis text and mark as safe HTML
    for section in sections:
        if "synthesis" in section and section["synthesis"]:
            text = section["synthesis"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            section["synthesis"] = Markup(text.replace("\n\n", "<br><br>").replace("\n", "<br>"))
    if fallback_text:
        text = fallback_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        fallback_text = Markup(text.replace("\n\n", "<br><br>").replace("\n", "<br>"))

    return template.render(
        subject=subject,
        date_display=date_display,
        time_display=time_display,
        article_count=article_count,
        source_count=source_count,
        executive_brief=executive_brief,
        what_changed=what_changed,
        sections=sections,
        fallback_text=fallback_text,
        next_digest=next_digest,
    )


def build_subject(
    dt: datetime,
    is_adhoc: bool = False,
    partial_sources: bool = False,
    synthesis_failed: bool = False,
    article_count: int = 0,
    source_count: int = 0,
) -> str:
    """Build email subject line from datetime and flags.

    Args:
        dt: Datetime object (should have Athens timezone)
        is_adhoc: Whether this is an ad-hoc digest
        partial_sources: Whether some sources failed
        synthesis_failed: Whether synthesis failed
        article_count: Number of articles in digest
        source_count: Number of sources

    Returns:
        Formatted subject line
    """
    # Ensure Athens timezone
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_ATHENS_TZ)
    else:
        dt = dt.astimezone(_ATHENS_TZ)

    # Format: "Day D MON HH:MM"
    time_str = dt.strftime("%H:%M")
    day_name = dt.strftime("%a").capitalize()
    day_num = dt.strftime("%-d")  # No leading zero
    month_name = dt.strftime("%b").upper()

    label = "News Digest"
    stats = f"{article_count} articles from {source_count} sources" if article_count else ""
    base = f"{label} | {day_name} {day_num} {month_name} {time_str}"

    if stats:
        base += f" | {stats}"

    # Add suffixes
    if synthesis_failed:
        base += " | headlines only"
    elif partial_sources:
        base += " | partial sources"

    return base


def send_email(
    subject: str,
    html_body: str,
    recipient: str,
    gmail_script: str,
) -> bool:
    """Send email via Gmail script.

    Args:
        subject: Email subject line
        html_body: HTML email body
        recipient: Recipient email address
        gmail_script: Path to Gmail sending script

    Returns:
        True if email sent successfully, False otherwise
    """
    cmd = [
        "node",
        gmail_script,
        "send",
        "--to",
        recipient,
        "--subject",
        subject,
        "--body",
        "plain fallback",
        "--html",
        html_body,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        if result.returncode == 0:
            logger.info(f"Email sent successfully to {recipient}")
            return True
        else:
            logger.error(f"Failed to send email: {result.stderr}")
            return False

    except subprocess.TimeoutExpired:
        logger.error("Email send timed out after 60s")
        return False

    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False


def render_monitor_html(
    synthesis: dict,
    mention_count: int,
    source_count: int,
    time_display: str,
    date_display: str,
    next_scan: str | None = None,
    subject: str = "",
) -> str:
    """Render the monitor HTML using Jinja2 template.

    Args:
        synthesis: Parsed monitor synthesis dict
        mention_count: Total number of mentions
        source_count: Total number of sources
        time_display: Time string (e.g. "15:00")
        date_display: Date string (e.g. "tue 8 apr")
        next_scan: Optional next scan time string
        subject: Email subject line

    Returns:
        Rendered HTML string
    """
    env = Environment(
        loader=FileSystemLoader(_TEMPLATES_DIR),
        autoescape=select_autoescape(default_for_string=True, default=True),
    )
    template = env.get_template("monitor.html")

    alerts = synthesis.get("alerts", [])
    executive_brief = synthesis.get("executive_brief", [])
    sentiment = synthesis.get("sentiment_summary")
    nbg_mentions = synthesis.get("nbg_mentions", [])
    sector_context = synthesis.get("sector_context", "")
    competitor_watch = synthesis.get("competitor_watch")
    fallback_text = synthesis.get("fallback_text", "")

    # Convert sector_context newlines to <br> for HTML
    if sector_context:
        text = sector_context.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        sector_context = Markup(text.replace("\n\n", "<br><br>").replace("\n", "<br>"))
    if fallback_text:
        text = fallback_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        fallback_text = Markup(text.replace("\n\n", "<br><br>").replace("\n", "<br>"))

    return template.render(
        subject=subject,
        date_display=date_display,
        time_display=time_display,
        mention_count=mention_count,
        source_count=source_count,
        alerts=alerts,
        executive_brief=executive_brief,
        sentiment=sentiment,
        nbg_mentions=nbg_mentions,
        sector_context=sector_context,
        competitor_watch=competitor_watch,
        fallback_text=fallback_text,
        next_scan=next_scan,
    )


def build_monitor_subject(
    dt: datetime,
    is_adhoc: bool = False,
    mention_count: int = 0,
    source_count: int = 0,
    has_alerts: bool = False,
    synthesis_failed: bool = False,
) -> str:
    """Build monitor email subject line.

    Args:
        dt: Datetime object (should have Athens timezone)
        is_adhoc: Whether this is an ad-hoc run
        mention_count: Number of mentions
        source_count: Number of sources
        has_alerts: Whether there are critical alerts
        synthesis_failed: Whether synthesis failed

    Returns:
        Formatted subject line
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_ATHENS_TZ)
    else:
        dt = dt.astimezone(_ATHENS_TZ)

    time_str = dt.strftime("%H:%M")
    day_name = dt.strftime("%a").capitalize()
    day_num = dt.strftime("%-d")
    month_name = dt.strftime("%b").upper()

    label = "NBG Monitor"
    base = f"{label} | {day_name} {day_num} {month_name} {time_str}"

    if mention_count:
        base += f" | {mention_count} mentions"

    if has_alerts:
        base += " | ALERT"
    if synthesis_failed:
        base += " | raw data"

    return base


def save_fallback(html: str, output_dir: str = "~/Downloads") -> str:
    """Save HTML to timestamped file as fallback.

    Args:
        html: HTML content to save
        output_dir: Directory to save file (default ~/Downloads)

    Returns:
        Absolute path to saved file
    """
    # Expand and resolve output directory
    output_path = Path(output_dir).expanduser()
    output_path.mkdir(parents=True, exist_ok=True)

    # Generate timestamp in Athens timezone
    now = datetime.now(_ATHENS_TZ)
    timestamp = now.strftime("%Y%m%d%H%M")

    # Create filename
    filename = f"{timestamp}_news_digest.html"
    filepath = output_path / filename

    # Write file
    filepath.write_text(html, encoding="utf-8")
    logger.info(f"Fallback HTML saved to {filepath}")

    return str(filepath.absolute())


def notify_macos(title: str, message: str) -> None:
    """Send macOS notification (best-effort).

    Args:
        title: Notification title
        message: Notification message
    """
    try:
        safe_title = title.replace('"', '\\"')
        safe_message = message.replace('"', '\\"')
        script = f'display notification "{safe_message}" with title "{safe_title}"'
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except Exception:
        # Silently ignore all notification errors
        pass
