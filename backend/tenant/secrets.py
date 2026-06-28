import hashlib
import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from backend.config import settings

logger = logging.getLogger(__name__)

_fernet: Optional[Fernet] = None


def hash_api_key(api_key: str) -> str:
    """One-way hash for API key lookup (stored in tenants collection)."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _get_fernet() -> Optional[Fernet]:
    global _fernet
    if _fernet is not None:
        return _fernet
    key = settings.ENCRYPTION_KEY
    if not key:
        logger.warning(
            "ENCRYPTION_KEY not set — integration secrets cannot be encrypted at rest."
        )
        return None
    try:
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as e:
        logger.error(f"Invalid ENCRYPTION_KEY: {e}")
        return None
    return _fernet


def encrypt_secret(plaintext: str) -> str:
    f = _get_fernet()
    if not f:
        raise RuntimeError("ENCRYPTION_KEY is required to encrypt integration secrets.")
    return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    f = _get_fernet()
    if not f:
        raise RuntimeError("ENCRYPTION_KEY is required to decrypt integration secrets.")
    try:
        return f.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("Failed to decrypt secret — invalid key or corrupted data.") from e
