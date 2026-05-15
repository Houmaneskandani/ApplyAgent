"""
Security invariants that we never want to regress.

These tests use isolated subprocesses to verify behavior at module import
time — that's where the env-var guards live, and a misconfigured import is
the entire risk surface.
"""
import os
import subprocess
import sys
import textwrap


def _run_isolated(env_overrides: dict, script: str) -> subprocess.CompletedProcess:
    """Run a script in a fresh Python with a controlled env (no .env inheritance)."""
    import tempfile

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    }
    env.update(env_overrides)
    with tempfile.TemporaryDirectory() as cwd:
        return subprocess.run(
            [sys.executable, "-c", textwrap.dedent(script)],
            capture_output=True, text=True, env=env, cwd=cwd,
            timeout=30,
        )


def test_secret_key_missing_crashes_at_import():
    r = _run_isolated(
        {"APP_ENV": "production", "DATABASE_URL": "postgresql://x:x@x/x",
         "ANTHROPIC_API_KEY": "sk-test"},
        "import api.auth",
    )
    assert r.returncode != 0
    assert "SECRET_KEY" in r.stderr


def test_secret_key_placeholder_rejected():
    r = _run_isolated(
        {"APP_ENV": "production", "DATABASE_URL": "postgresql://x:x@x/x",
         "ANTHROPIC_API_KEY": "sk-test", "SECRET_KEY": "your-secret-key-change-this"},
        "import api.auth",
    )
    assert r.returncode != 0


def test_secret_key_too_short_rejected():
    r = _run_isolated(
        {"APP_ENV": "development", "DATABASE_URL": "postgresql://x:x@x/x",
         "ANTHROPIC_API_KEY": "sk-test", "SECRET_KEY": "short"},
        "import api.auth",
    )
    assert r.returncode != 0


def test_production_without_database_url_rejected():
    r = _run_isolated(
        {"APP_ENV": "production", "SECRET_KEY": "a" * 64,
         "ANTHROPIC_API_KEY": "sk-test"},
        "import config; config.validate_config(strict=True)",
    )
    assert r.returncode != 0
    assert "DATABASE_URL" in r.stderr


def test_stripe_without_webhook_secret_rejected():
    """If STRIPE_SECRET_KEY is set, STRIPE_WEBHOOK_SECRET MUST also be set."""
    r = _run_isolated(
        {"APP_ENV": "production", "DATABASE_URL": "postgresql://x:x@x/x",
         "SECRET_KEY": "a" * 64, "ANTHROPIC_API_KEY": "sk-test",
         "STRIPE_SECRET_KEY": "sk_test_xxx"},
        "import config",
    )
    assert r.returncode != 0
    assert "STRIPE_WEBHOOK_SECRET" in r.stderr


def test_valid_production_config_loads():
    r = _run_isolated(
        {"APP_ENV": "production", "DATABASE_URL": "postgresql://x:x@x/x",
         "SECRET_KEY": "a" * 64, "ANTHROPIC_API_KEY": "sk-test"},
        "import config; print('OK')",
    )
    assert r.returncode == 0
    assert "OK" in r.stdout


def test_deduct_credits_is_single_atomic_statement():
    """The classic SELECT-then-UPDATE race must be impossible."""
    import inspect
    import re
    import db
    src = inspect.getsource(db.deduct_credits)
    # Single UPDATE with a predicate and RETURNING
    flat = re.sub(r"\s+", " ", src)
    assert "UPDATE users SET credits = credits - $1" in flat
    assert "WHERE id = $2 AND COALESCE(credits, 0) >= $1" in flat
    assert "RETURNING credits" in flat


def test_fernet_round_trip():
    from cryptography.fernet import Fernet
    # Reset module state with a known key
    os.environ["SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    import importlib
    import config
    import secrets_crypto
    importlib.reload(config)
    importlib.reload(secrets_crypto)

    ct = secrets_crypto.encrypt("hunter2-app-password")
    assert ct.startswith("enc:")
    assert secrets_crypto.decrypt(ct) == "hunter2-app-password"
    # Legacy plaintext returned as-is
    assert secrets_crypto.decrypt("legacy-plaintext") == "legacy-plaintext"
    # Empty handling
    assert secrets_crypto.encrypt("") == ""
    assert secrets_crypto.decrypt("") == ""


def test_fernet_invalid_ciphertext_returns_empty():
    """Corrupted ciphertext must NEVER crash — it returns ""."""
    from cryptography.fernet import Fernet
    os.environ["SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    import importlib
    import config
    import secrets_crypto
    importlib.reload(config)
    importlib.reload(secrets_crypto)
    assert secrets_crypto.decrypt("enc:not-real-base64-anything") == ""
