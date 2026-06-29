import logging
import boto3
from botocore.exceptions import ClientError
from backend.config import settings

logger = logging.getLogger(__name__)

async def send_reset_password_email(email: str, token: str) -> bool:
    """
    Send a reset password email to the user via AWS SES.
    If AWS/SES config is missing, logs the link in local console as a fallback.
    """
    reset_link = f"{settings.DASHBOARD_URL}/reset-password?token={token}"
    
    # Check if SES configs are missing (Fallback to console logging for local testing)
    if not settings.AWS_ACCESS_KEY_ID or not settings.AWS_SECRET_ACCESS_KEY or not settings.SES_SENDER_EMAIL:
        logger.warning(
            "\nAWS SES credentials not fully configured. Reset link printed to server console:\n"
            f"🔗 RESET PASSWORD LINK: {reset_link}\n"
        )
        print(f"\n🔗 RESET PASSWORD LINK: {reset_link}\n")
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
