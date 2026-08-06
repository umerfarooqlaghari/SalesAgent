import os
import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import dns.resolver
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Placeholder values shipped in source — fine for local dev, must never reach
# production. Kept as module constants so other modules can compare against
# them (auth/service.py seed_super_admin, etc.) without re-declaring them.
DEFAULT_JWT_SECRET = "change-me-in-production-use-long-random-string"
DEFAULT_SUPER_ADMIN_EMAIL = "admin@alpha.dev"
DEFAULT_SUPER_ADMIN_PASSWORD = "Admin123!change"
DEFAULT_ADAPTER_HUB_MASTER_KEY = "adapter-hub-super-secret-key"


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
    VAPI_WEBHOOK_SECRET: str = ""
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
    JWT_SECRET: str = DEFAULT_JWT_SECRET
    SUPER_ADMIN_EMAIL: str = DEFAULT_SUPER_ADMIN_EMAIL
    SUPER_ADMIN_PASSWORD: str = DEFAULT_SUPER_ADMIN_PASSWORD
    ADAPTER_HUB_URL: str = "http://127.0.0.1:8001"
    ADAPTER_HUB_MASTER_KEY: str = DEFAULT_ADAPTER_HUB_MASTER_KEY
    ADAPTER_HUB_ENABLED: bool = True

    # S06/S07: ENVIRONMENT gates production-only hardening so local dev/tests
    # keep working off insecure-but-convenient defaults.
    ENVIRONMENT: str = "development"
    ALLOWED_ORIGINS: str = ""  # comma-separated; required in production for credentialed routes
    ALLOW_MOCK_BILLING: bool = True

    model_config = SettingsConfigDict(
        env_file=os.path.join(os.path.dirname(__file__), ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.strip().lower() in ("production", "prod")

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]


settings = Settings()


def validate_production_secrets() -> None:
    """
    S03/S04/S09: refuse to boot in production with placeholder secrets that
    ship in source. Development keeps the defaults working (with a warning)
    so local setup and the test suite are unaffected.
    """
    if not settings.is_production:
        if settings.JWT_SECRET == DEFAULT_JWT_SECRET:
            logger.warning(
                "JWT_SECRET is the placeholder value — fine for local dev, "
                "but set a real ENVIRONMENT=production secret before deploying."
            )
        return

    problems = []
    if settings.JWT_SECRET == DEFAULT_JWT_SECRET or len(settings.JWT_SECRET) < 32:
        problems.append("JWT_SECRET must be a random string of at least 32 characters")
    if settings.SUPER_ADMIN_EMAIL == DEFAULT_SUPER_ADMIN_EMAIL:
        problems.append("SUPER_ADMIN_EMAIL must not use the placeholder value")
    if settings.SUPER_ADMIN_PASSWORD == DEFAULT_SUPER_ADMIN_PASSWORD:
        problems.append("SUPER_ADMIN_PASSWORD must not use the placeholder value")
    if settings.ADAPTER_HUB_MASTER_KEY == DEFAULT_ADAPTER_HUB_MASTER_KEY:
        problems.append("ADAPTER_HUB_MASTER_KEY must not use the placeholder value")
    if not settings.ENCRYPTION_KEY:
        problems.append("ENCRYPTION_KEY must be set — tenant secrets cannot be encrypted without it")
    if not settings.allowed_origins_list:
        problems.append("ALLOWED_ORIGINS must list the real dashboard/embed origins")
    if problems:
        raise RuntimeError(
            "Refusing to start with ENVIRONMENT=production and insecure defaults: "
            + "; ".join(problems)
        )


validate_production_secrets()


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
