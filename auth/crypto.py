"""API key encryption/decryption using Fernet (AES-128-CBC + HMAC-SHA256)."""
import os
import logging

logger = logging.getLogger(__name__)

_fernet = None


def init_fernet(key: str = None) -> None:
    """Initialize Fernet cipher with key from env or parameter."""
    global _fernet
    from cryptography.fernet import Fernet

    key = key or os.getenv("FERNET_KEY")
    if not key:
        # Auto-generate for development (NOT production-safe)
        key = Fernet.generate_key().decode()
        logger.warning("FERNET_KEY not set — auto-generated (NOT SAFE FOR PRODUCTION): %s", key[:8] + "...")

    _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    logger.info("Fernet encryption initialized")


def encrypt_api_key(plaintext: str) -> bytes:
    """Encrypt an API key for storage."""
    if _fernet is None:
        raise RuntimeError("Fernet not initialized. Call init_fernet() first.")
    return _fernet.encrypt(plaintext.encode())


def decrypt_api_key(ciphertext: bytes) -> str:
    """Decrypt an API key from storage."""
    if _fernet is None:
        raise RuntimeError("Fernet not initialized. Call init_fernet() first.")
    return _fernet.decrypt(ciphertext).decode()


def generate_fernet_key() -> str:
    """Generate a new Fernet key (run once, save to .env)."""
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def mask_api_key(key: str) -> str:
    """Mask an API key for display (show last 4 chars only)."""
    if not key or len(key) < 8:
        return "****"
    return "****" + key[-4:]
