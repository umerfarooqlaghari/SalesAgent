import os
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
    JWT_SECRET: str = "change-me-in-production-use-long-random-string"
    SUPER_ADMIN_EMAIL: str = "admin@alpha.dev"
    SUPER_ADMIN_PASSWORD: str = "Admin123!change"

    model_config = SettingsConfigDict(
        env_file=os.path.join(os.path.dirname(__file__), ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()
