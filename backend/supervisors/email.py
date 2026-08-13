"""
Round-robin supervisor email dispatcher.

When the agent calls `handoff_to_human`, this module picks the next active
supervisor for the tenant and sends them an SES email with the caller's details.
Falls back gracefully if no supervisors are registered or SES is not configured.
"""
from __future__ import annotations

import logging
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from backend.config import settings

logger = logging.getLogger(__name__)


async def _pick_next_supervisor(tenant_id: str) -> Optional[dict]:
    """
    Round-robin: returns the next active supervisor for this tenant.
    Tracks position in `supervisor_rr` collection: {tenant_id, counter}.
    Returns None if no active supervisors are registered.
    """
    from backend.database import get_db

    db = get_db()

    # Fetch all active supervisors sorted by creation time (stable order)
    supervisors = []
    async for doc in db.supervisors.find(
        {"tenant_id": tenant_id, "active": True}
    ).sort("created_at", 1):
        supervisors.append(doc)

    if not supervisors:
        return None

    from pymongo import ReturnDocument

    # Atomically increment the round-robin counter and get the updated value
    rr_doc = await db.supervisor_rr.find_one_and_update(
        {"tenant_id": tenant_id},
        {"$inc": {"counter": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    # The counter starts at 0 on first upsert; after increment it's 1.
    # We want index 0 on first call, so use (counter - 1) % len.
    counter_val = (rr_doc or {}).get("counter", 1)
    counter = (counter_val - 1) % len(supervisors)
    return supervisors[counter]


async def send_supervisor_handoff_email(
    tenant_id: str,
    thread_id: str,
    caller_name: str,
    caller_phone: str,
    caller_email: str,
    reason: str,
) -> bool:
    """
    Pick the next supervisor via round-robin and send them an SES handoff email.
    Returns True if email was sent (or printed in dev), False if skipped/failed.
    """
    supervisor = await _pick_next_supervisor(tenant_id)
    if not supervisor:
        logger.warning(
            "handoff_to_human fired for tenant %s but no active supervisors are registered. "
            "Add supervisors in the dashboard → Supervisors tab.",
            tenant_id,
        )
        return False

    to_email = supervisor["email"]
    supervisor_name = supervisor.get("name", "Supervisor")
    department = supervisor.get("department", "")
    dept_label = f" ({department})" if department else ""

    subject = f"🔔 Human Follow-Up Requested — {caller_name or 'Unknown caller'}"

    body_text = (
        f"Hi {supervisor_name}{dept_label},\n\n"
        f"A caller has requested to speak with a human representative.\n\n"
        f"Caller Name:  {caller_name or 'Not provided'}\n"
        f"Caller Email: {caller_email or 'Not provided'}\n"
        f"Caller Phone: {caller_phone or 'Not provided'}\n"
        f"Reason:       {reason or 'Not specified'}\n"
        f"Thread ID:    {thread_id}\n\n"
        f"Open the console to review: {settings.DASHBOARD_URL}\n\n"
        f"This is an automated message from the AI Sales Agent."
    )

    body_html = f"""
<html>
<head></head>
<body style="font-family: Arial, sans-serif; color: #333;">
  <h2 style="color: #4f46e5;">🔔 Human Follow-Up Requested</h2>
  <p>Hi <strong>{supervisor_name}</strong>{dept_label},</p>
  <p>A caller has requested to speak with a human representative.</p>
  <table style="border-collapse: collapse; width: 100%; max-width: 480px;">
    <tr><td style="padding: 6px 12px; font-weight: bold; background: #f5f5f5;">Caller Name</td>
        <td style="padding: 6px 12px;">{caller_name or 'Not provided'}</td></tr>
    <tr><td style="padding: 6px 12px; font-weight: bold; background: #f5f5f5;">Caller Email</td>
        <td style="padding: 6px 12px;">{caller_email or 'Not provided'}</td></tr>
    <tr><td style="padding: 6px 12px; font-weight: bold; background: #f5f5f5;">Caller Phone</td>
        <td style="padding: 6px 12px;">{caller_phone or 'Not provided'}</td></tr>
    <tr><td style="padding: 6px 12px; font-weight: bold; background: #f5f5f5;">Reason</td>
        <td style="padding: 6px 12px;">{reason or 'Not specified'}</td></tr>
    <tr><td style="padding: 6px 12px; font-weight: bold; background: #f5f5f5;">Thread ID</td>
        <td style="padding: 6px 12px;"><code>{thread_id}</code></td></tr>
  </table>
  <br/>
  <a href="{settings.DASHBOARD_URL}" style="background:#4f46e5;color:#fff;padding:10px 20px;text-decoration:none;border-radius:6px;">
    Open Console
  </a>
  <p style="margin-top:24px;color:#888;font-size:12px;">
    This is an automated message from the AI Sales Agent platform.
  </p>
</body>
</html>
"""

    # Dev fallback — no SES configured
    if (
        not settings.AWS_ACCESS_KEY_ID
        or not settings.AWS_SECRET_ACCESS_KEY
        or not settings.SES_SENDER_EMAIL
    ):
        if settings.is_production:
            logger.error(
                "SES is not configured — cannot send supervisor handoff email in production. "
                "Set AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, and SES_SENDER_EMAIL."
            )
            return False
        print(
            f"\n📧 SUPERVISOR HANDOFF EMAIL (dev only, not sent):\n"
            f"  To:      {to_email}\n"
            f"  Subject: {subject}\n"
            f"  Caller:  {caller_name} | {caller_email} | {caller_phone}\n"
            f"  Reason:  {reason}\n"
        )
        return True

    try:
        client = boto3.client(
            "ses",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_REGION_NAME,
        )
        response = client.send_email(
            Destination={"ToAddresses": [to_email]},
            Message={
                "Body": {
                    "Html": {"Charset": "UTF-8", "Data": body_html},
                    "Text": {"Charset": "UTF-8", "Data": body_text},
                },
                "Subject": {"Charset": "UTF-8", "Data": subject},
            },
            Source=settings.SES_SENDER_EMAIL,
        )
        logger.info(
            "Supervisor handoff email sent to %s (%s) for thread %s. MessageId: %s",
            supervisor_name,
            to_email,
            thread_id,
            response.get("MessageId", "?"),
        )
        return True
    except ClientError as e:
        logger.error(
            "SES failed to send supervisor handoff email: %s",
            e.response["Error"]["Message"],
            exc_info=True,
        )
        return False
