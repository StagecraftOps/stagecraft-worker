import logging

import boto3

from app.core.config import settings

logger = logging.getLogger(__name__)

_SUBJECT = "Stagecraft suggested a fix for {repo}"

_BODY_TEXT = """Stagecraft analyzed a failed workflow run in {repo} and suggested a fix.

Failure category: {category}
Root cause: {root_cause}

Review and raise a PR for this fix:
{link}

This is an automated notification — reply-to is not monitored."""

_BODY_HTML = """<html><body>
<p>Stagecraft analyzed a failed workflow run in <strong>{repo}</strong> and suggested a fix.</p>
<p><strong>Failure category:</strong> {category}<br>
<strong>Root cause:</strong> {root_cause}</p>
<p><a href="{link}">Review and raise a PR for this fix</a></p>
<p style="color:#888;font-size:12px">This is an automated notification — reply-to is not monitored.</p>
</body></html>"""

def send_fix_notification(
    to_email: str,
    repo_name: str,
    failure_category: str | None,
    root_cause: str,
    remediation_id: str,
) -> None:
    if not settings.SES_ENABLED:
        return
    if not settings.SES_FROM_EMAIL:
        logger.warning("SES_ENABLED is true but SES_FROM_EMAIL is unset — skipping notification")
        return

    link = f"{settings.FRONTEND_URL}/remediations/{remediation_id}"
    category = failure_category or "UNKNOWN"
    subject = _SUBJECT.format(repo=repo_name)
    text_body = _BODY_TEXT.format(repo=repo_name, category=category, root_cause=root_cause, link=link)
    html_body = _BODY_HTML.format(repo=repo_name, category=category, root_cause=root_cause, link=link)

    client = boto3.client("ses", region_name=settings.AWS_REGION)
    client.send_email(
        Source=settings.SES_FROM_EMAIL,
        Destination={"ToAddresses": [to_email]},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {
                "Text": {"Data": text_body, "Charset": "UTF-8"},
                "Html": {"Data": html_body, "Charset": "UTF-8"},
            },
        },
    )
