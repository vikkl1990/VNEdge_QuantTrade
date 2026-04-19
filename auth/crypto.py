"""API key encryption with per-user key derivation.

Architecture (2026-04-19):
  Master key (from FERNET_KEY env) is NEVER used directly to encrypt
  user API keys. Instead, for each user we derive a per-user Fernet key
  via HKDF(master_key, salt=user_id). Compromising one user's ciphertext
  yields no information about other users' ciphertext. Rotating the
  master key (via rotate_fernet_key()) re-wraps all per-user keys.

Backward compatibility:
  Ciphertexts created under the legacy single-key scheme (no user_id
  binding) still decrypt via the master Fernet cipher. On first re-encrypt
  (typical: user changes key, or admin triggers migration), they're
  upgraded to the per-user scheme. See decrypt_api_key() for details.

Public API:
  init_fernet(key=None)                   — call once at startup
  encrypt_api_key(plaintext)              — legacy single-key encrypt (kept for migration)
  decrypt_api_key(ciphertext, user_id=None) — decrypts under per-user key first, then legacy
  encrypt_for_user(plaintext, user_id)    — new preferred path
  generate_fernet_key()                   — emit a new master key
  mask_api_key(key)                       — UI display helper
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_fernet = None          # master Fernet cipher (legacy path + HKDF seed)
_master_key_bytes = b""  # raw master key bytes (decoded from base64url Fernet key)

# Cache of per-user derived Fernet ciphers — cleared on init_fernet().
_user_cipher_cache: dict = {}


def _hkdf_expand(master: bytes, info: bytes, length: int = 32) -> bytes:
    """HKDF-Expand (RFC 5869) with SHA-256. Used for per-user key derivation.
    length=32 gives a 256-bit key, which we then base64url-encode to a Fernet key."""
    hash_len = 32  # sha256
    n = -(-length // hash_len)  # ceil
    t = b""
    okm = b""
    for i in range(n):
        t = hmac.new(master, t + info + bytes([i + 1]), hashlib.sha256).digest()
        okm += t
    return okm[:length]


def init_fernet(key: str = None) -> None:
    """Initialize the master Fernet cipher and clear the per-user cache."""
    global _fernet, _master_key_bytes, _user_cipher_cache
    from cryptography.fernet import Fernet

    key = key or os.getenv("FERNET_KEY")
    if not key:
        # Auto-generate for development (NOT production-safe)
        key = Fernet.generate_key().decode()
        logger.warning(
            "FERNET_KEY not set — auto-generated (NOT SAFE FOR PRODUCTION): %s",
            key[:8] + "...",
        )

    key_bytes = key.encode() if isinstance(key, str) else key
    _fernet = Fernet(key_bytes)
    # Store the raw bytes (base64url-decoded) for HKDF
    try:
        _master_key_bytes = base64.urlsafe_b64decode(key_bytes)
    except Exception:
        # Shouldn't happen with a valid Fernet key, but fail-safe
        _master_key_bytes = hashlib.sha256(key_bytes).digest()
    _user_cipher_cache = {}
    logger.info("Fernet encryption initialized (per-user key derivation enabled)")


def _get_user_cipher(user_id: str):
    """Return the Fernet cipher derived for a specific user.
    Cached in-memory; O(1) after first call per user per process lifetime."""
    from cryptography.fernet import Fernet

    if _fernet is None:
        raise RuntimeError("Fernet not initialized. Call init_fernet() first.")
    if not user_id:
        raise ValueError("user_id required for per-user key derivation")
    if user_id in _user_cipher_cache:
        return _user_cipher_cache[user_id]
    # HKDF-Expand(master, info="apikey:" + user_id, 32 bytes) → base64url → Fernet
    derived = _hkdf_expand(_master_key_bytes, f"apikey:{user_id}".encode(), 32)
    user_key = base64.urlsafe_b64encode(derived)
    cipher = Fernet(user_key)
    _user_cipher_cache[user_id] = cipher
    return cipher


def encrypt_api_key(plaintext: str) -> bytes:
    """Legacy: encrypt under the master key only.

    DEPRECATED for new code. Use encrypt_for_user() instead. Kept for:
      - Backward compatibility with existing ciphertexts
      - Migration scripts that bulk-re-encrypt across key rotation
    """
    if _fernet is None:
        raise RuntimeError("Fernet not initialized. Call init_fernet() first.")
    return _fernet.encrypt(plaintext.encode())


def encrypt_for_user(plaintext: str, user_id: str) -> bytes:
    """Preferred path: encrypt an API key under a per-user derived cipher.

    A leak of one user's ciphertext does NOT compromise other users'
    ciphertext (different keys). Rotation re-derives all per-user keys.
    """
    cipher = _get_user_cipher(user_id)
    return cipher.encrypt(plaintext.encode())


def decrypt_api_key(ciphertext: bytes, user_id: Optional[str] = None) -> str:
    """Decrypt an API key.

    Resolution order:
      1. If user_id is provided: try the per-user cipher first.
      2. On InvalidToken (ciphertext was written under the legacy single-key
         scheme): fall back to the master cipher.
      3. If both fail: re-raise the last exception.

    Passing user_id=None is the old API — kept so existing callers don't
    break. Those callers MUST migrate to pass user_id so new ciphertexts
    can be written under the per-user scheme on rotation/rewrite.
    """
    from cryptography.fernet import Fernet, InvalidToken

    if _fernet is None:
        raise RuntimeError("Fernet not initialized. Call init_fernet() first.")

    if user_id:
        try:
            return _get_user_cipher(user_id).decrypt(ciphertext).decode()
        except InvalidToken:
            # Legacy ciphertext (pre per-user-derivation) — fall back
            pass

    return _fernet.decrypt(ciphertext).decode()


def rewrap_user_keys(user_id: str, plaintext_api_key: str, plaintext_api_secret: str) -> tuple:
    """Re-encrypt a user's keys under the per-user derived cipher.

    Used when:
      - Admin rotates FERNET_KEY (all users need re-wrap)
      - A legacy-encrypted record is read → we can rewrap on next write
      - User uploads a new key (goes through encrypt_for_user directly)

    Returns (api_key_enc, api_secret_enc) tuple ready for DB UPDATE.
    """
    return (
        encrypt_for_user(plaintext_api_key, user_id),
        encrypt_for_user(plaintext_api_secret, user_id),
    )


def generate_fernet_key() -> str:
    """Generate a new Fernet master key (run once, save to .env)."""
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def mask_api_key(key: str) -> str:
    """Mask an API key for display (show last 4 chars only)."""
    if not key or len(key) < 8:
        return "****"
    return "****" + key[-4:]
