import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import dns.resolver
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    MONGODB_URI: str
    DATABASE_NAME: str = "salesagent"
    GEMINI_API_KEY: str
    GOOGLE_CLOUD_PROJECT: str = ""
    PORT: int = 8000
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_API_KEY_SID: str = ""
    TWILIO_API_KEY_SECRET: str = ""
    TWILIO_WHATSAPP_FROM: str = ""
    TWILIO_WHATSAPP_TO: str = ""
    ENABLE_WHATSAPP_ALERTS: bool = False
    VAPI_PRIVATE_KEY: str = ""
    VAPI_PUBLIC_KEY: str = ""
    VAPI_ASSISTANT_ID: str = ""
    ENCRYPTION_KEY: str = ""
    REDIS_URL: str = ""
    DEFAULT_TENANT_ID: str = "alpha_default"
    GEMINI_MODEL: str = "gemini-2.5-flash-lite"
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION_NAME: str = "us-east-1"
    SES_SENDER_EMAIL: str = ""
    DASHBOARD_URL: str = "http://localhost:3000"
    STRIPE_API_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY: str = ""
    JWT_SECRET: str = "change-me-in-production-use-long-random-string"
    SUPER_ADMIN_EMAIL: str = "admin@alpha.dev"
    SUPER_ADMIN_PASSWORD: str = "Admin123!change"
    ADAPTER_HUB_URL: str = "http://127.0.0.1:8001"
    ADAPTER_HUB_MASTER_KEY: str = "adapter-hub-super-secret-key"
    ADAPTER_HUB_ENABLED: bool = True

    model_config = SettingsConfigDict(
        env_file=os.path.join(os.path.dirname(__file__), ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()


def get_mongodb_connection_uri() -> str:
    """Return a MongoDB URI that avoids Atlas TXT lookups on unreliable DNS."""
    parsed_uri = urlsplit(settings.MONGODB_URI)
    if parsed_uri.scheme != "mongodb+srv":
        return settings.MONGODB_URI

    if not parsed_uri.hostname:
        return settings.MONGODB_URI

    records = dns.resolver.resolve(
        f"_mongodb._tcp.{parsed_uri.hostname}", "SRV", lifetime=10
    )
    hosts = ",".join(
        f"{str(record.target).rstrip('.')}:{record.port}" for record in records
    )
    user_info, separator, _ = parsed_uri.netloc.rpartition("@")
    netloc = f"{user_info}@{hosts}" if separator else hosts
    query = dict(parse_qsl(parsed_uri.query, keep_blank_values=True))
    if "tls" not in query and "ssl" not in query:
        query["tls"] = "true"

    return urlunsplit(
        ("mongodb", netloc, parsed_uri.path, urlencode(query), "")
    )
