import logging
import boto3
from botocore.exceptions import ClientError
from backend.config import settings

logger = logging.getLogger(__name__)

async def send_reset_password_email(email: str, token: str) -> bool:
    """
    Send a reset password email to the user via AWS SES.
    In non-production environments with no SES configured, prints the link to
    the local console only (never through the logger) as a dev convenience.
    """
    reset_link = f"{settings.DASHBOARD_URL}/reset-password?token={token}"

    # S12: the reset token used to be written through logger.warning, and
    # container logs are frequently aggregated/retained — that's an
    # account-takeover oracle. Never log the token; the console fallback is
    # for local development only.
    if not settings.AWS_ACCESS_KEY_ID or not settings.AWS_SECRET_ACCESS_KEY or not settings.SES_SENDER_EMAIL:
        if settings.is_production:
            logger.error(
                "SES is not configured — cannot send password reset email in production."
            )
            return False
        print(f"\n🔗 RESET PASSWORD LINK (dev only, not sent): {reset_link}\n")
        return True

    try:
        # Initialize boto3 SES client
        client = boto3.client(
            "ses",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_REGION_NAME
        )
        
        body_html = f"""
        <html>
        <head></head>
        <body>
          <h1>Reset Your Password</h1>
          <p>Please click the link below to reset your B2B SDR platform password:</p>
          <a href="{reset_link}">{reset_link}</a>
          <p>This link will expire in 1 hour.</p>
        </body>
        </html>
        """
        
        body_text = f"Reset your password by visiting this link: {reset_link}\nThis link will expire in 1 hour."
        
        response = client.send_email(
            Destination={
                'ToAddresses': [email],
            },
            Message={
                'Body': {
                    'Html': {
                        'Charset': 'UTF-8',
                        'Data': body_html,
                    },
                    'Text': {
                        'Charset': 'UTF-8',
                        'Data': body_text,
                    },
                },
                'Subject': {
                    'Charset': 'UTF-8',
                    'Data': 'Reset your B2B SDR Platform Password',
                },
            },
            Source=settings.SES_SENDER_EMAIL,
        )
        logger.info(f"SES Reset password email sent successfully to {email}, Message ID: {response['MessageId']}")
        return True
    except ClientError as e:
        logger.error(f"Failed to send email via AWS SES: {e.response['Error']['Message']}", exc_info=True)
        return False


async def send_verification_email(email: str, token: str) -> bool:
    """S24: send the single-use email-verification link. Same dev/prod
    fallback behavior as send_reset_password_email — never log the token."""
    verify_link = f"{settings.DASHBOARD_URL}/verify-email?token={token}"

    if not settings.AWS_ACCESS_KEY_ID or not settings.AWS_SECRET_ACCESS_KEY or not settings.SES_SENDER_EMAIL:
        if settings.is_production:
            logger.error("SES is not configured — cannot send verification email in production.")
            return False
        print(f"\n✉️  VERIFY EMAIL LINK (dev only, not sent): {verify_link}\n")
        return True

    try:
        client = boto3.client(
            "ses",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_REGION_NAME,
        )
        body_html = f"""
        <html>
        <head></head>
        <body>
          <h1>Verify Your Email</h1>
          <p>Please click the link below to verify your B2B SDR platform account:</p>
          <a href="{verify_link}">{verify_link}</a>
        </body>
        </html>
        """
        body_text = f"Verify your account by visiting this link: {verify_link}"
        response = client.send_email(
            Destination={"ToAddresses": [email]},
            Message={
                "Body": {
                    "Html": {"Charset": "UTF-8", "Data": body_html},
                    "Text": {"Charset": "UTF-8", "Data": body_text},
                },
                "Subject": {"Charset": "UTF-8", "Data": "Verify your B2B SDR Platform account"},
            },
            Source=settings.SES_SENDER_EMAIL,
        )
        logger.info(f"SES verification email sent successfully to {email}, Message ID: {response['MessageId']}")
        return True
    except ClientError as e:
        logger.error(f"Failed to send verification email via AWS SES: {e.response['Error']['Message']}", exc_info=True)
        return False
