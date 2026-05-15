"""
Symmetric encryption for credentials stored at rest in the DB.

Use cases:
- Gmail App Passwords (IMAP) used to read greenhouse verification codes.
- Any other per-user secret we don't want a DB-only attacker to read.

Generate a key with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Then set it in your environment as SECRETS_ENCRYPTION_KEY.

Stored values are prefixed with `enc:` so we can transparently migrate plaintext
data on first read without a separate migration job. A value that doesn't carry
the prefix is treated as legacy plaintext and re-encrypted on next write.
"""
from __future__ import annotations
from typing import Optional
import base64

from config import SECRETS_ENCRYPTION_KEY

_PREFIX = "enc:"
_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is not None:
        return _fernet
    if not SECRETS_ENCRYPTION_KEY:
        raise RuntimeError(
            "SECRETS_ENCRYPTION_KEY is required for encrypting credentials at rest.\n"
            "Generate one with: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    from cryptography.fernet import Fernet
    try:
        # Validate the key shape — Fernet requires 32 url-safe base64 bytes.
        _fernet = Fernet(SECRETS_ENCRYPTION_KEY.encode())
    except Exception as e:
        raise RuntimeError(
            "SECRETS_ENCRYPTION_KEY is not a valid Fernet key. "
            f"It must be url-safe base64-encoded 32 bytes. ({e})"
        )
    return _fernet


def encrypt(plaintext: Optional[str]) -> str:
    """Encrypt a string. Empty/None input returns empty string."""
    if not plaintext:
        return ""
    token = _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"{_PREFIX}{token}"


def decrypt(stored: Optional[str]) -> str:
    """
    Decrypt a value that may be encrypted (`enc:` prefix) or legacy plaintext.

    Returns the empty string for missing/invalid input — never raises, because
    a corrupted IMAP password should NOT take down the application flow.
    """
    if not stored:
        return ""
    if not stored.startswith(_PREFIX):
        # Legacy plaintext — return as-is. A subsequent save will re-encrypt.
        return stored
    try:
        return _get_fernet().decrypt(stored[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def is_encrypted(stored: Optional[str]) -> bool:
    return bool(stored) and stored.startswith(_PREFIX)
