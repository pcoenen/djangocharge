"""Email notifications, each problem only once (deduplicated by key)."""

import logging

from django.conf import settings
from django.core.mail import send_mail

from .models import Notification

logger = logging.getLogger(__name__)


def notify(key, subject, body=""):
    """Email NOTIFY_EMAIL unless this key was already reported. Returns True if it's new."""
    key = key[:300]
    note, created = Notification.objects.get_or_create(key=key, defaults={"subject": subject[:300], "body": body})
    if not created:
        return False
    logger.warning("Notification: %s", subject)
    recipients = settings.NOTIFY_EMAIL
    if isinstance(recipients, str):
        recipients = [r.strip() for r in recipients.split(",") if r.strip()]
    if recipients:
        try:
            send_mail(f"[Dyncharge] {subject}", body or subject, None, recipients)
            note.sent = True
            note.save(update_fields=["sent"])
        except Exception:  # never let a mail problem stop the charging logic
            logger.exception("Sending notification email failed")
    return True
