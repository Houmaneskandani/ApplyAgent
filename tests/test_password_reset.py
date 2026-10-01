"""
Password reset: the code is actually delivered, brute-force is capped per
account (not just per IP), and a reset invalidates existing sessions.
"""
import inspect
import re
from datetime import datetime, timezone, timedelta


def _flat(src: str) -> str:
    return re.sub(r"\s+", " ", src)


def test_forgot_password_emails_the_code_off_the_event_loop():
    from api import auth
    src = inspect.getsource(auth.forgot_password)
    assert "_send_email" in src
    assert "asyncio.to_thread(_send_email" in src
    # Never leak the code into production logs.
    assert "if not IS_PROD:" in src
    # A fresh code resets the wrong-guess counter.
    assert "reset_attempts = 0" in _flat(src)


def test_reset_password_caps_attempts_per_account():
    from api import auth
    src = _flat(inspect.getsource(auth.reset_password))
    assert auth.MAX_RESET_ATTEMPTS == 5
    assert "reset_attempts = COALESCE(reset_attempts, 0) + 1" in src
    assert "attempts >= MAX_RESET_ATTEMPTS" in src
    # Burn the code once the cap is hit.
    assert "SET reset_token = NULL, reset_token_expires = NULL, reset_attempts = 0" in src
    # Still constant-time on the compare.
    assert "_secrets.compare_digest" in src


def test_reset_password_stamps_password_changed_at():
    from api import auth
    src = _flat(inspect.getsource(auth.reset_password))
    assert "password_changed_at = $3" in src
    assert "_remember_password_changed(" in src


def test_tokens_carry_iat_and_stale_ones_are_rejected():
    from api import auth
    from jose import jwt
    tok = auth.create_token(42, "a@b.c")
    payload = jwt.decode(tok, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])
    assert isinstance(payload["iat"], int)
    assert payload["exp"] > payload["iat"]
    # Decision helper: strictly-before → stale; same second or later → fine.
    assert auth.token_issued_before_password_change(100, 101) is True
    assert auth.token_issued_before_password_change(101, 101) is False
    assert auth.token_issued_before_password_change(102, 101) is False
    # Legacy tokens (no iat) and users who never reset are never rejected.
    assert auth.token_issued_before_password_change(None, 101) is False
    assert auth.token_issued_before_password_change(100, None) is False


def test_password_changed_cache_treats_naive_timestamps_as_utc():
    from api import auth
    naive = datetime(2026, 1, 1, 12, 0, 0, 500_000)  # what asyncpg returns for TIMESTAMP
    ts = auth._remember_password_changed(7, naive)
    assert ts == int(naive.replace(tzinfo=timezone.utc).timestamp())
    # A login one second later is accepted; one issued a second earlier is not.
    assert auth.token_issued_before_password_change(ts + 1, ts) is False
    assert auth.token_issued_before_password_change(ts - 1, ts) is True
    auth._pw_changed_cache.pop(7, None)


def test_get_current_user_checks_password_change_and_fails_open():
    from api import auth
    src = inspect.getsource(auth.get_current_user)
    assert "token_issued_before_password_change" in src
    assert "except Exception" in src  # DB blip must not read as "logged out"
