"""Email delivery with Jinja2 rendering and Gmail sending."""

import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader

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
    env = Environment(loader=FileSystemLoader(_TEMPLATES_DIR))
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
) -> str:
    """Build email subject line from datetime and flags.

    Args:
        dt: Datetime object (should have Athens timezone)
        is_adhoc: Whether this is an ad-hoc digest
        partial_sources: Whether some sources failed
        synthesis_failed: Whether synthesis failed

    Returns:
        Formatted subject line (all lowercase)
    """
    # Ensure Athens timezone
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_ATHENS_TZ)
    else:
        dt = dt.astimezone(_ATHENS_TZ)

    # Format: "HH:MM day D mon"
    time_str = dt.strftime("%H:%M")
    day_name = dt.strftime("%a").lower()
    day_num = dt.strftime("%-d")  # No leading zero
    month_name = dt.strftime("%b").lower()

    base = f"news digest — {time_str} {day_name} {day_num} {month_name}"

    if is_adhoc:
        base = base.replace("news digest", "news digest — ad hoc")

    # Add suffixes
    if synthesis_failed:
        base += " — synthesis unavailable"
    elif partial_sources:
        base += " — partial sources"

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
        script = f'display notification "{message}" with title "{title}"'
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except Exception:
        # Silently ignore all notification errors
        pass
