"""
Shared pytest fixtures.

The test suite is INTENTIONALLY shallow — it covers the things that are
worth catching in CI: security invariants, cost-impacting logic (deduct
credits, answer cache), and contract-level API shape. It does NOT run
real Playwright applies or hit the live DB.
"""
import os
import secrets
import sys
import pytest

# Make sure the repo root is on the import path so `from api.main import app`
# works regardless of where pytest is invoked from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _gen_fernet_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def _ensure_env() -> None:
    """
    Populate the minimal env our modules need so import-time validation passes.
    We use unconditional assignment (not setdefault) because the developer's
    shell or .env file may have an EMPTY value set, which setdefault treats as
    "already set" but config.validate_config treats as missing.
    """
    os.environ["APP_ENV"] = "development"
    if not os.environ.get("DATABASE_URL"):
        os.environ["DATABASE_URL"] = "postgresql://test:test@localhost/test"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = "sk-test-not-real"
    if not os.environ.get("SECRET_KEY") or len(os.environ.get("SECRET_KEY", "")) < 32:
        os.environ["SECRET_KEY"] = secrets.token_urlsafe(64)
    if not os.environ.get("SECRETS_ENCRYPTION_KEY"):
        os.environ["SECRETS_ENCRYPTION_KEY"] = _gen_fernet_key()
    # NOTE: load_dotenv() in config.py defaults to override=False, so as long
    # as we set these env vars BEFORE config is imported, our values win even
    # if the user's local .env contains different ones.


_ensure_env()
