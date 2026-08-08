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

# Only these exact labels turn the production hardening OFF. Anything else —
# "live", "staging", "prod-eu", a typo, an empty string — is treated as
# production. The previous check allowlisted PRODUCTION instead, so
# ENVIRONMENT=live ran completely unhardened.
#
# Module-level, not a class attribute: a leading-underscore name in a pydantic
# BaseSettings body becomes a ModelPrivateAttr rather than the tuple itself.
DEV_LABELS = ("development", "dev", "local", "test", "testing")


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

    # Every production guard in this file is gated on `is_production`. While this
    # defaulted to "development", a container deployed without the variable set —
    # which is exactly what the Dockerfile produced — silently ran with the
    # placeholder JWT secret, the seeded admin@alpha.dev super-admin, the
    # well-known test API key, the hardcoded Adapter-Hub Fernet key, mock billing
    # enabled and Vapi signature verification skipped. Forgetting a variable must
    # not be the thing that unlocks all of that.
    #
    # It now defaults to production and DEVELOPMENT IS OPT-IN: set
    # ENVIRONMENT=development explicitly for local work (backend/.env) and in the
    # test conftest. Failing closed here means a misconfigured deploy refuses to
    # boot with a precise list of what is missing, instead of coming up insecure.
    ENVIRONMENT: str = "production"
    ALLOWED_ORIGINS: str = ""  # comma-separated; required in production for credentialed routes
    # S07: with this on, /api/billing/checkout writes tier + allowed_minutes
    # directly and any tenant can self-grant Enterprise. Opt-in only.
    ALLOW_MOCK_BILLING: bool = False
    # S13: how many trailing X-Forwarded-For entries OUR infrastructure appends.
    # Render appends exactly 1. Left at 0 the header is ignored, which is the
    # safe default for a directly-exposed service — but behind a proxy that
    # makes the auth limiter a single global bucket for every visitor.
    TRUSTED_PROXY_HOPS: int = 0

    model_config = SettingsConfigDict(
        env_file=os.path.join(os.path.dirname(__file__), ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.strip().lower() not in DEV_LABELS

    @property
    def is_development(self) -> bool:
        return not self.is_production

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
                "but set a real secret before deploying."
            )
        # Not fatal in dev, but say it clearly here rather than letting every
        # integration save fail later with a confusing runtime error.
        if settings.ENCRYPTION_KEY:
            try:
                from cryptography.fernet import Fernet

                Fernet(settings.ENCRYPTION_KEY.encode())
            except Exception as e:
                logger.error(
                    "ENCRYPTION_KEY is set but is NOT a valid Fernet key (%s). Every "
                    "integration secret save and read will fail. Generate one with: "
                    "python -c \"from cryptography.fernet import Fernet; "
                    "print(Fernet.generate_key().decode())\"",
                    type(e).__name__,
                )
        else:
            logger.warning(
                "ENCRYPTION_KEY is not set — integration secrets cannot be saved."
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
    # S08: checking only that this is non-empty was not enough. A malformed key
    # booted cleanly and then failed at RUNTIME on every integration read and
    # write — which is precisely the failure that surfaced to callers as
    # "Sorry, I hit a small snag" on every products/services question. Validate
    # that it actually constructs a Fernet, at startup, where it is cheap to see.
    if not settings.ENCRYPTION_KEY:
        problems.append("ENCRYPTION_KEY must be set — tenant secrets cannot be encrypted without it")
    else:
        try:
            from cryptography.fernet import Fernet

            Fernet(settings.ENCRYPTION_KEY.encode())
        except Exception as e:
            problems.append(
                "ENCRYPTION_KEY is not a valid Fernet key (%s). Generate one with: "
                "python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\"" % type(e).__name__
            )
    if not settings.allowed_origins_list:
        problems.append("ALLOWED_ORIGINS must list the real dashboard/embed origins")
    if not settings.VAPI_WEBHOOK_SECRET:
        problems.append(
            "VAPI_WEBHOOK_SECRET must be set — without it the voice webhook and "
            "/chat/completions cannot be authenticated"
        )
    if problems:
        raise RuntimeError(
            "Refusing to start: ENVIRONMENT is %r (anything other than %s is "
            "treated as production) and the following are insecure or missing:\n  - %s\n"
            "For local development set ENVIRONMENT=development in backend/.env."
            % (settings.ENVIRONMENT, "/".join(DEV_LABELS), "\n  - ".join(problems))
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
