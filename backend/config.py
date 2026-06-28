import os
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    MONGODB_URI: str
    DATABASE_NAME: str = "salesagent"
    GEMINI_API_KEY: str
    GOOGLE_CLOUD_PROJECT: str = ""
    PORT: int = 8000
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_API_KEY_SID: str = ""
    TWILIO_API_KEY_SECRET: str = ""
    TWILIO_WHATSAPP_FROM: str = ""
    TWILIO_WHATSAPP_TO: str = ""
    ENABLE_WHATSAPP_ALERTS: bool = False
    VAPI_PRIVATE_KEY: str = ""
    VAPI_PUBLIC_KEY: str = ""

    model_config = SettingsConfigDict(
        env_file=os.path.join(os.path.dirname(__file__), ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()
