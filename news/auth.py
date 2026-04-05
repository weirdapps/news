"""Google Cloud authentication checking and failure notifications."""

import logging
import subprocess

logger = logging.getLogger(__name__)


def check_gcloud_auth() -> bool:
    """Check if gcloud authentication is valid.

    Returns:
        True if authenticated with valid token, False otherwise
    """
    try:
        result = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

        # Success if returncode is 0 and stdout is non-empty
        if result.returncode == 0 and result.stdout.strip():
            logger.info("gcloud auth check: OK")
            return True
        else:
            logger.warning(f"gcloud auth check failed: {result.stderr.strip()}")
            return False

    except FileNotFoundError:
        logger.error("gcloud command not found - is Google Cloud SDK installed?")
        return False

    except subprocess.TimeoutExpired:
        logger.error("gcloud auth check timed out after 15s")
        return False

    except Exception as e:
        logger.error(f"gcloud auth check failed: {e}")
        return False


def send_auth_failure_notification(recipient: str, gmail_script: str) -> None:
    """Send notification email about auth failure.

    Args:
        recipient: Email address to send notification to
        gmail_script: Path to Gmail sending script
    """
    from news.deliver import send_email

    subject = "news reader — gcloud auth expired"

    html_body = """<html>
<body style="font-family: Aptos, sans-serif; font-size: 12pt; color: #404040;">
gcloud authentication has expired.<br><br>

please run:<br>
<code style="background: #f5f5f5; padding: 2px 6px;">gcloud auth login</code><br><br>

the news digest pipeline was skipped.
</body>
</html>"""

    success = send_email(
        subject=subject,
        html_body=html_body,
        recipient=recipient,
        gmail_script=gmail_script,
    )

    if success:
        logger.info(f"Auth failure notification sent to {recipient}")
    else:
        logger.error("Failed to send auth failure notification")
