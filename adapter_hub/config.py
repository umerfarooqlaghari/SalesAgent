import logging
import os
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

# S09: these ship in source so local dev works out of the box. They must never
# reach production — see validate_production_secrets() below.
_DEFAULT_ENCRYPTION_KEY = "u-j-l1oGg45gXfKSpF8yGvRjWp_vB1S1Zq5D3Q4P-fE="
_DEFAULT_MASTER_API_KEY = "adapter-hub-super-secret-key"


class Settings(BaseSettings):
    # Security keys
    ENCRYPTION_KEY: str = os.getenv("ENCRYPTION_KEY", _DEFAULT_ENCRYPTION_KEY) # 32-byte url-safe base64 key
    MASTER_API_KEY: str = os.getenv("ADAPTER_HUB_MASTER_KEY", _DEFAULT_MASTER_API_KEY)

    # Redis configuration
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # Vector DB settings (mocked or real Pinecone/Supabase)
    VECTOR_DB_URL: str = os.getenv("VECTOR_DB_URL", "http://localhost:8000")
    VECTOR_DB_API_KEY: str = os.getenv("VECTOR_DB_API_KEY", "mock-vector-key")

    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")

    class Config:
        env_prefix = "ADAPTER_HUB_"
        case_sensitive = True

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.strip().lower() in ("production", "prod")


settings = Settings()


def validate_production_secrets() -> None:
    if not settings.is_production:
        return
    problems = []
    if settings.ENCRYPTION_KEY == _DEFAULT_ENCRYPTION_KEY:
        problems.append("ADAPTER_HUB_ENCRYPTION_KEY must not use the placeholder value")
    if settings.MASTER_API_KEY == _DEFAULT_MASTER_API_KEY:
        problems.append("ADAPTER_HUB_MASTER_KEY must not use the placeholder value")
    if problems:
        raise RuntimeError(
            "Refusing to start adapter_hub with ENVIRONMENT=production and insecure defaults: "
            + "; ".join(problems)
        )


validate_production_secrets()
