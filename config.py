"""
Configuration + env-var validation.

This module loads all env vars and validates the required ones. It is designed to
fail LOUD at import time if a required production secret is missing or insecure —
the worst case is a silently misconfigured deploy that exposes the API.
"""
from dotenv import load_dotenv
import os
import sys

load_dotenv()


# ─── Environment detection ──────────────────────────────────────────
# "production" enables strict validation. Local dev / scripts can run with
# placeholder values for non-essential secrets.
APP_ENV = os.getenv("APP_ENV", "development").lower()
IS_PROD = APP_ENV in ("production", "prod")


# ─── Required everywhere ────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")


# ─── Stripe (required only if Stripe is enabled) ────────────────────
STRIPE_SECRET_KEY     = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_ENABLED        = bool(STRIPE_SECRET_KEY)


# ─── Optional integrations ──────────────────────────────────────────
CAPSOLVER_API_KEY = os.getenv("CAPSOLVER_API_KEY", "")
RAPIDAPI_KEY      = os.getenv("RAPIDAPI_KEY", "")
JOB_KEYWORDS      = os.getenv("JOB_KEYWORDS", "").split(",")
JOB_LOCATION      = os.getenv("JOB_LOCATION", "")

USER_EMAIL = os.getenv("USER_EMAIL", "you@email.com")
USER_NAME  = os.getenv("USER_NAME",  "Your Name")

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM        = os.getenv("TWILIO_FROM", "")
TWILIO_TO          = os.getenv("TWILIO_TO", "")

SMTP_HOST    = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT    = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER    = os.getenv("SMTP_USER", "")
SMTP_PASS    = os.getenv("SMTP_PASS", "")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "")

# Encryption key for credentials stored at rest (Gmail App Passwords, etc).
# Generate with:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
SECRETS_ENCRYPTION_KEY = os.getenv("SECRETS_ENCRYPTION_KEY", "")

# Sentry error tracking. If SENTRY_DSN is empty (the default), Sentry init is
# a no-op and the rest of the app runs identically. Set in production.
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
SENTRY_TRACES_SAMPLE_RATE = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1"))


# ─── Validation ─────────────────────────────────────────────────────
def _fail(msg: str) -> None:
    print(f"\n[CONFIG ERROR] {msg}\n", file=sys.stderr)
    raise RuntimeError(msg)


def validate_config(strict: bool = None) -> None:
    """
    Verify required env vars are set. Called at module import for production
    and at API boot. Local scripts (main.py, worker.py) can call this manually
    if they need scoring/applying — but a plain `python -c` won't crash.
    """
    if strict is None:
        strict = IS_PROD

    errors: list[str] = []

    if not DATABASE_URL:
        errors.append("DATABASE_URL is not set (Supabase → Settings → Database → URI).")

    if not ANTHROPIC_API_KEY and strict:
        errors.append("ANTHROPIC_API_KEY is not set (required for scoring + form filling).")

    if STRIPE_ENABLED and not STRIPE_WEBHOOK_SECRET:
        # CRITICAL: without the webhook secret, the webhook endpoint accepts
        # unsigned bodies and anyone can grant themselves infinite credits.
        errors.append(
            "STRIPE_WEBHOOK_SECRET is required when STRIPE_SECRET_KEY is set. "
            "Without it, the /credits/webhook endpoint is exploitable."
        )

    if errors:
        joined = "\n  - ".join(errors)
        _fail(f"Required configuration missing:\n  - {joined}")


# Auto-validate in production. In dev, scripts can opt in.
if IS_PROD:
    validate_config(strict=True)
