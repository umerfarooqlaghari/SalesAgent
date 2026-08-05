import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Security keys
    ENCRYPTION_KEY: str = os.getenv("ENCRYPTION_KEY", "u-j-l1oGg45gXfKSpF8yGvRjWp_vB1S1Zq5D3Q4P-fE=") # 32-byte url-safe base64 key
    MASTER_API_KEY: str = os.getenv("ADAPTER_HUB_MASTER_KEY", "adapter-hub-super-secret-key")
    
    # Redis configuration
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    
    # Vector DB settings (mocked or real Pinecone/Supabase)
    VECTOR_DB_URL: str = os.getenv("VECTOR_DB_URL", "http://localhost:8000")
    VECTOR_DB_API_KEY: str = os.getenv("VECTOR_DB_API_KEY", "mock-vector-key")

    class Config:
        env_prefix = "ADAPTER_HUB_"
        case_sensitive = True

settings = Settings()
