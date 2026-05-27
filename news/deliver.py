"""Email delivery with Jinja2 rendering and outlook-cli sending."""

import logging
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

logger = logging.getLogger(__name__)

# Template directory relative to this file
_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
_ATHENS_TZ = ZoneInfo("Europe/Athens")

# HTML entity constants for escaping
_HTML_AMP = "&amp;"
_HTML_BR_DOUBLE = "<br><br>"


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
    env = Environment(  # nosemgrep
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
            text = (
                section["synthesis"]
                .replace("&", _HTML_AMP)
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            section["synthesis"] = Markup(  # nosemgrep
                text.replace("\n\n", _HTML_BR_DOUBLE).replace("\n", "<br>")
            )
    if fallback_text:
        text = (
            fallback_text.replace("&", _HTML_AMP)
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        fallback_text = Markup(
            text.replace("\n\n", _HTML_BR_DOUBLE).replace("\n", "<br>")
        )  # nosemgrep

    return template.render(  # nosemgrep
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
    stats = (
        f"{article_count} articles from {source_count} sources" if article_count else ""
    )
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
) -> bool:
    """Send email via outlook-cli (Microsoft Graph API).

    Uses the user's existing outlook-cli auth (no separate Gmail OAuth).
    Writes the HTML body to a temp file because outlook-cli's --html flag
    expects a file path, not an inline string.

    Args:
        subject: Email subject line
        html_body: HTML email body
        recipient: Recipient email address

    Returns:
        True if email sent successfully, False otherwise
    """
    html_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False, encoding="utf-8"
        ) as f:
            f.write(html_body)
            html_path = f.name

        cmd = [
            "outlook-cli",
            "send-mail",
            "--to",
            recipient,
            "--subject",
            subject,
            "--html",
            html_path,
            "--send-now",
            "--no-cc-self",
            "--no-signature",
        ]

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

    finally:
        if html_path:
            try:
                os.unlink(html_path)
            except OSError:
                pass


def render_monitor_html(
    synthesis: dict,
    mention_count: int,
    source_count: int,
    time_display: str,
    date_display: str,
    keywords_config: dict,
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
        keywords_config: Brand keywords config (provides display block for labels)
        next_scan: Optional next scan time string
        subject: Email subject line

    Returns:
        Rendered HTML string
    """
    env = Environment(  # nosemgrep
        loader=FileSystemLoader(_TEMPLATES_DIR),
        autoescape=select_autoescape(default_for_string=True, default=True),
    )
    template = env.get_template("monitor.html")

    alerts = synthesis.get("alerts", [])
    executive_brief = synthesis.get("executive_brief", [])
    sentiment = synthesis.get("sentiment_summary")
    company_mentions = synthesis.get("company_mentions", [])
    sector_context = synthesis.get("sector_context", "")
    competitor_watch = synthesis.get("competitor_watch")
    fallback_text = synthesis.get("fallback_text", "")

    # Convert sector_context newlines to <br> for HTML
    if sector_context:
        text = (
            sector_context.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        sector_context = Markup(
            text.replace("\n\n", "<br><br>").replace("\n", "<br>")
        )  # nosemgrep
    if fallback_text:
        text = (
            fallback_text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        fallback_text = Markup(
            text.replace("\n\n", "<br><br>").replace("\n", "<br>")
        )  # nosemgrep

    competitors = keywords_config.get("competitors", {})
    competitor_display_names = {
        key: comp.get("names", [key.title()])[0] for key, comp in competitors.items()
    }

    return template.render(  # nosemgrep
        subject=subject,
        date_display=date_display,
        time_display=time_display,
        mention_count=mention_count,
        source_count=source_count,
        alerts=alerts,
        executive_brief=executive_brief,
        sentiment=sentiment,
        company_mentions=company_mentions,
        display=keywords_config.get("display", {}),
        sector_context=sector_context,
        competitor_watch=competitor_watch,
        competitor_display_names=competitor_display_names,
        fallback_text=fallback_text,
        next_scan=next_scan,
    )


def build_monitor_subject(
    dt: datetime,
    keywords_config: dict,
    is_adhoc: bool = False,
    mention_count: int = 0,
    source_count: int = 0,
    has_alerts: bool = False,
    synthesis_failed: bool = False,
) -> str:
    """Build monitor email subject line.

    Args:
        dt: Datetime object (should have Athens timezone)
        keywords_config: Brand keywords config (provides display.monitor_label)
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

    label = keywords_config.get("display", {}).get("monitor_label", "BRAND MONITOR")
    base = f"{label} | {day_name} {day_num} {month_name} {time_str}"

    if mention_count:
        base += f" | {mention_count} mentions"

    if has_alerts:
        base += " | ALERT"
    if synthesis_failed:
        base += " | raw data"

    return base


def render_topic_html(
    synthesis: dict,
    query: str,
    hours: int,
    source_count: int,
    time_display: str,
    date_display: str,
    subject: str = "",
) -> str:
    """Render the topic HTML using Jinja2 template.

    Args:
        synthesis: Parsed topic synthesis dict (executive_brief + sections)
        query: User's free-text topic query
        hours: Time window in hours
        source_count: Total number of source articles fetched
        time_display: Time string (e.g. "15:00")
        date_display: Date string (e.g. "sun 4 may")
        subject: Email subject line

    Returns:
        Rendered HTML string
    """
    env = Environment(  # nosemgrep
        loader=FileSystemLoader(_TEMPLATES_DIR),
        autoescape=select_autoescape(default_for_string=True, default=True),
    )
    template = env.get_template("topic.html")

    executive_brief = synthesis.get("executive_brief", [])
    sections = synthesis.get("sections", [])
    fallback_text = synthesis.get("fallback_text", "")

    # Pre-convert newlines to <br> in synthesis text and mark as safe HTML
    for section in sections:
        if "synthesis" in section and section["synthesis"]:
            text = (
                section["synthesis"]
                .replace("&", _HTML_AMP)
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            section["synthesis"] = Markup(  # nosemgrep
                text.replace("\n\n", _HTML_BR_DOUBLE).replace("\n", "<br>")
            )
    if fallback_text:
        text = (
            fallback_text.replace("&", _HTML_AMP)
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        fallback_text = Markup(
            text.replace("\n\n", _HTML_BR_DOUBLE).replace("\n", "<br>")
        )  # nosemgrep

    return template.render(  # nosemgrep
        subject=subject,
        query=query,
        hours=hours,
        date_display=date_display,
        time_display=time_display,
        source_count=source_count,
        executive_brief=executive_brief,
        sections=sections,
        fallback_text=fallback_text,
    )


def build_topic_subject(
    dt: datetime,
    query: str,
    is_adhoc: bool = True,
    synthesis_failed: bool = False,
) -> str:
    """Build topic email subject line.

    Format: "Topic Brief | {query[:50]}[…] | {Day} {DD} {MON} {HH:MM}"

    Args:
        dt: Datetime object (should have Athens timezone)
        query: User's free-text query (truncated to 50 chars in subject)
        is_adhoc: Whether this is an ad-hoc run (always True for topic; accepted for symmetry)
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

    # Truncate query to 50 chars; append … when truncated
    if len(query) > 50:
        query_display = query[:50] + "…"
    else:
        query_display = query

    base = (
        f"Topic Brief | {query_display} | {day_name} {day_num} {month_name} {time_str}"
    )

    if synthesis_failed:
        base += " | headlines only"

    return base


def render_stack_html(
    synthesis: dict,
    article_count: int,
    source_count: int,
    time_display: str,
    date_display: str,
    next_run: str | None = None,
    subject: str = "",
) -> str:
    """Render the stack HTML using Jinja2 template."""
    env = Environment(  # nosemgrep
        loader=FileSystemLoader(_TEMPLATES_DIR),
        autoescape=select_autoescape(default_for_string=True, default=True),
    )
    template = env.get_template("stack.html")

    executive_brief = synthesis.get("executive_brief", [])
    try_this = synthesis.get("try_this", [])
    recommendations = synthesis.get("recommendations", [])
    sections = synthesis.get("sections", [])
    fallback_text = synthesis.get("fallback_text", "")

    # Extract text from citation-filtered bullets (same pattern as digest)
    if executive_brief and isinstance(executive_brief[0], dict):
        executive_brief = [b.get("text", str(b)) for b in executive_brief]
    if try_this and isinstance(try_this[0], dict):
        try_this = [t.get("text", str(t)) for t in try_this]
    if recommendations and isinstance(recommendations[0], dict):
        recommendations = [r.get("text", str(r)) for r in recommendations]

    for section in sections:
        if "synthesis" in section and section["synthesis"]:
            text = (
                section["synthesis"]
                .replace("&", _HTML_AMP)
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            section["synthesis"] = Markup(  # nosemgrep
                text.replace("\n\n", _HTML_BR_DOUBLE).replace("\n", "<br>")
            )
    if fallback_text:
        text = (
            fallback_text.replace("&", _HTML_AMP)
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        fallback_text = Markup(
            text.replace("\n\n", _HTML_BR_DOUBLE).replace("\n", "<br>")
        )  # nosemgrep

    return template.render(  # nosemgrep
        subject=subject,
        date_display=date_display,
        time_display=time_display,
        article_count=article_count,
        source_count=source_count,
        executive_brief=executive_brief,
        try_this=try_this,
        recommendations=recommendations,
        sections=sections,
        fallback_text=fallback_text,
        next_run=next_run,
    )


def build_stack_subject(
    dt: datetime,
    is_adhoc: bool = False,
    synthesis_failed: bool = False,
    article_count: int = 0,
    source_count: int = 0,
) -> str:
    """Build stack email subject line.

    Format: "Stack | {article_count} articles | Day D MON HH:MM"
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_ATHENS_TZ)
    else:
        dt = dt.astimezone(_ATHENS_TZ)

    time_str = dt.strftime("%H:%M")
    day_name = dt.strftime("%a").capitalize()
    day_num = dt.strftime("%-d")
    month_name = dt.strftime("%b").upper()

    base = f"Stack | {article_count} articles | {day_name} {day_num} {month_name} {time_str}"

    if is_adhoc:
        base = f"[adhoc] {base}"
    if synthesis_failed:
        base += " | headlines only"

    return base


def save_fallback(
    html: str, output_dir: str = "~/Downloads", label: str = "digest"
) -> str:
    """Save HTML to timestamped file as fallback.

    Args:
        html: HTML content to save
        output_dir: Directory to save file (default ~/Downloads)
        label: Pipeline label used in filename to avoid collisions
            between concurrent pipelines (e.g. "digest", "monitor")

    Returns:
        Absolute path to saved file
    """
    output_path = Path(output_dir).expanduser()
    output_path.mkdir(parents=True, exist_ok=True)

    now = datetime.now(_ATHENS_TZ)
    timestamp = now.strftime("%Y%m%d%H%M")

    filename = f"{timestamp}_news_{label}.html"
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
