from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from passlib.context import CryptContext
from jose import jwt, JWTError
from datetime import datetime, timedelta, timezone
from db import get_pool
import asyncio
import os
import time as _time
import secrets as _secrets

# SECURITY: rate-limit auth endpoints. Without these limits, a 6-digit reset
# code (~1M space) is brute-forceable in ~17 minutes within its 15-min window,
# and there's no defense against credential stuffing on /login.
try:
    from slowapi import Limiter
    from slowapi.util import get_remote_address
    limiter = Limiter(key_func=get_remote_address)
    _HAS_LIMITER = True
except ImportError:
    limiter = None
    _HAS_LIMITER = False


def _rate_limit(rule: str):
    """Decorator that applies a rate limit if slowapi is installed, else no-ops."""
    def deco(fn):
        return limiter.limit(rule)(fn) if _HAS_LIMITER else fn
    return deco


router = APIRouter()
security = HTTPBearer()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# SECURITY: SECRET_KEY is REQUIRED. No fallback default — a missing or default
# secret means every JWT in the world is forgeable. Fail loud at import time.
SECRET_KEY = os.getenv("SECRET_KEY")
_INSECURE_DEFAULTS = {"", "your-secret-key-change-this", "change-me", "secret"}
if not SECRET_KEY or SECRET_KEY in _INSECURE_DEFAULTS:
    raise RuntimeError(
        "SECRET_KEY environment variable is required and must be a strong random value.\n"
        "Generate one with:  python -c \"import secrets; print(secrets.token_urlsafe(64))\"\n"
        "Then set it in your environment (Railway → Variables, or local .env)."
    )
if len(SECRET_KEY) < 32:
    raise RuntimeError(
        f"SECRET_KEY is only {len(SECRET_KEY)} chars; require at least 32 for HS256 safety."
    )

ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 24 * 7
BCRYPT_MAX_BYTES = 72  # bcrypt truncates beyond this; apply the SAME truncation everywhere


def _bcrypt_prep(password: str) -> bytes:
    """bcrypt hashes at most 72 BYTES. Truncate at the BYTE level (not chars) —
    `password[:72]` slices 72 characters, which for multibyte/unicode passwords
    can exceed 72 bytes and make passlib raise (a 500). Returning bytes also
    sidesteps passlib's own length check. Must be applied IDENTICALLY at hash
    and verify time, or logins would never match."""
    return password.encode("utf-8")[:BCRYPT_MAX_BYTES]

class SignupRequest(BaseModel):
    email: str
    password: str
    name: str

class LoginRequest(BaseModel):
    email: str
    password: str

def create_token(user_id: int, email: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "user_id": user_id,
        "email": email,
        # Whole seconds. Compared against users.password_changed_at (floored
        # to seconds too) so a login in the same second as a reset still works.
        "iat": int(now.timestamp()),
        "exp": now + timedelta(hours=TOKEN_EXPIRE_HOURS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


# users.password_changed_at, cached per user_id for a short TTL so token
# validation doesn't add a DB round-trip to EVERY request. reset_password
# updates the cache in-process immediately; other processes converge within
# the TTL (worst case: an old token survives ~60s longer on another worker).
_PW_CHANGED_TTL = 60.0
_pw_changed_cache: dict[int, tuple[int | None, float]] = {}


def _remember_password_changed(user_id: int, changed_at: datetime | None) -> int | None:
    ts = None
    if changed_at is not None:
        if changed_at.tzinfo is None:
            changed_at = changed_at.replace(tzinfo=timezone.utc)
        ts = int(changed_at.timestamp())
    _pw_changed_cache[user_id] = (ts, _time.monotonic())
    return ts


async def _password_changed_ts(user_id: int) -> int | None:
    hit = _pw_changed_cache.get(user_id)
    if hit and _time.monotonic() - hit[1] < _PW_CHANGED_TTL:
        return hit[0]
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT password_changed_at FROM users WHERE id = $1", user_id
        )
    return _remember_password_changed(user_id, row["password_changed_at"] if row else None)


def token_issued_before_password_change(iat, changed_ts) -> bool:
    """Pure helper (unit-tested): a token is stale when it was issued strictly
    before the (second-floored) password change."""
    if iat is None or changed_ts is None:
        return False
    try:
        return int(iat) < int(changed_ts)
    except (TypeError, ValueError):
        return False


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        user = {"user_id": payload["user_id"], "email": payload["email"]}
    except (JWTError, KeyError):
        raise HTTPException(status_code=401, detail="Invalid token")
    # Reject tokens minted before the user's last password reset. Tokens from
    # before this check shipped carry no `iat` and are accepted until they
    # expire (7 days) — no forced re-login on deploy.
    iat = payload.get("iat")
    if iat is not None:
        try:
            changed_ts = await _password_changed_ts(user["user_id"])
        except Exception as e:
            # Fail OPEN on a DB hiccup: the request itself will surface the
            # DB error; we don't want a blip to read as "logged out".
            print(f"  ⚠ password_changed_at lookup failed user={user['user_id']}: {type(e).__name__}: {e}")
            changed_ts = None
        if token_issued_before_password_change(iat, changed_ts):
            raise HTTPException(status_code=401, detail="Session expired — please log in again")
    return user

@router.post("/signup")
@_rate_limit("5/minute")
async def signup(request: Request, req: SignupRequest):
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT id FROM users WHERE email = $1", req.email)
        if existing:
            raise HTTPException(status_code=400, detail="Email already registered")
        hashed = pwd_context.hash(_bcrypt_prep(req.password))
        row = await conn.fetchrow("""
            INSERT INTO users (email, name, password_hash)
            VALUES ($1, $2, $3) RETURNING id
        """, req.email, req.name, hashed)
        token = create_token(row["id"], req.email)
        return {"token": token, "user_id": row["id"], "name": req.name}

@router.post("/login")
@_rate_limit("10/minute")
async def login(request: Request, req: LoginRequest):
    pool = await get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, name, password_hash FROM users WHERE email = $1", req.email
        )
        if not user or not user["password_hash"]:
            raise HTTPException(status_code=401, detail="Invalid email or password")
        password = _bcrypt_prep(req.password)
        if not pwd_context.verify(password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid email or password")
        token = create_token(user["id"], req.email)
        return {"token": token, "user_id": user["id"], "name": user["name"]}

@router.get("/me")
async def me(user=Depends(get_current_user)):
    return user


class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordRequest(BaseModel):
    email: str
    code: str
    new_password: str

@router.post("/forgot-password")
@_rate_limit("3/minute")
async def forgot_password(request: Request, req: ForgotPasswordRequest):
    pool = await get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT id, name FROM users WHERE email = $1", req.email)
        # Always return success to avoid revealing whether email exists
        if not user:
            return {"status": "If that email exists, a reset code has been sent"}

        # SECURITY: use secrets.SystemRandom (CSPRNG), not random.randint (Mersenne
        # Twister — predictable from a few outputs). 6 digits is still only ~20 bits
        # of entropy; the rate limit on /reset-password is what makes brute-force
        # infeasible. Consider migrating to a 128-bit URL token in a follow-up.
        code = f"{_secrets.SystemRandom().randrange(1_000_000):06d}"
        expires = datetime.utcnow() + timedelta(minutes=15)

        await conn.execute(
            "UPDATE users SET reset_token = $1, reset_token_expires = $2, reset_attempts = 0 WHERE id = $3",
            code, expires, user["id"]
        )

        # Print to stdout only OUTSIDE production. Logging reset codes to Railway
        # makes them visible to anyone with log access.
        from config import IS_PROD, SMTP_USER, SMTP_PASS
        if not IS_PROD:
            print(f"\n{'='*40}")
            print(f"  PASSWORD RESET CODE for {req.email}")
            print(f"  Code: {code}  (expires in 15 min)")
            print(f"{'='*40}\n")

    # Email the code (the only delivery channel in production). _send_email is
    # a blocking smtplib call with its own 20s timeout — run it in a thread so
    # the request returns immediately and the event loop isn't held.
    if SMTP_USER and SMTP_PASS:
        from notifications import _send_email
        first = (user["name"] or "there").split(" ")[0]
        body = (
            f"Hi {first},\n\n"
            f"Your ApplyAgent password reset code is:\n\n"
            f"    {code}\n\n"
            f"It expires in 15 minutes. If you didn't request a reset, you can "
            f"ignore this email — your password won't change.\n\n"
            f"— ApplyAgent"
        )
        _spawn_bg(asyncio.to_thread(_send_email, "Your ApplyAgent password reset code", body, req.email))
    else:
        print("  ⚠ /forgot-password: SMTP_USER/SMTP_PASS not set — reset code was NOT emailed")

    return {"status": "If that email exists, a reset code has been sent"}


MAX_RESET_ATTEMPTS = 5

# Strong references for fire-and-forget tasks: asyncio only keeps a weak ref
# to a task, so a bare create_task() can be garbage-collected mid-flight.
_bg_tasks: set = set()


def _spawn_bg(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


@router.post("/reset-password")
@_rate_limit("5/minute")
async def reset_password(request: Request, req: ResetPasswordRequest):
    pool = await get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, reset_token, reset_token_expires FROM users WHERE email = $1",
            req.email
        )
        if not user or not user["reset_token"]:
            raise HTTPException(status_code=400, detail="Invalid or expired reset code")

        if user["reset_token_expires"] is None or datetime.utcnow() > user["reset_token_expires"]:
            raise HTTPException(status_code=400, detail="Reset code has expired. Please request a new one.")

        # Constant-time compare so the 6-digit code can't be brute-forced via
        # response-timing (the != short-circuits on the first wrong digit).
        if not _secrets.compare_digest(str(user["reset_token"]), str(req.code or "")):
            # Per-ACCOUNT attempt cap. The per-IP rate limit alone is not a
            # defense (IPs rotate); after MAX_RESET_ATTEMPTS wrong guesses the
            # code is burned and the user must request a new one.
            row = await conn.fetchrow(
                """
                UPDATE users SET reset_attempts = COALESCE(reset_attempts, 0) + 1
                 WHERE id = $1
                RETURNING reset_attempts
                """,
                user["id"],
            )
            attempts = (row["reset_attempts"] if row else 0) or 0
            if attempts >= MAX_RESET_ATTEMPTS:
                await conn.execute(
                    "UPDATE users SET reset_token = NULL, reset_token_expires = NULL, reset_attempts = 0 WHERE id = $1",
                    user["id"],
                )
                raise HTTPException(
                    status_code=400,
                    detail="Too many incorrect attempts. Please request a new reset code.",
                )
            raise HTTPException(status_code=400, detail="Invalid or expired reset code")

        if len(req.new_password or "") < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

        # SECURITY: apply the SAME 72-byte truncation as signup/login.
        # Without this, a long password set via reset would be saved as bcrypt(full)
        # but login truncates to 72 bytes and the comparison would fail.
        hashed = pwd_context.hash(_bcrypt_prep(req.new_password))
        # Python-side UTC timestamp (like reset_token_expires), NOT NOW(): the
        # column is a naive TIMESTAMP and NOW() would be rendered in the DB
        # session's timezone, which get_current_user then reads as UTC.
        changed_at = datetime.utcnow()
        await conn.execute(
            """
            UPDATE users
               SET password_hash = $1,
                   reset_token = NULL,
                   reset_token_expires = NULL,
                   reset_attempts = 0,
                   password_changed_at = $3
             WHERE id = $2
            """,
            hashed, user["id"], changed_at,
        )
        # Invalidate every JWT issued before now (see get_current_user), and
        # prime the in-process cache so it takes effect on the next request.
        _remember_password_changed(user["id"], changed_at)

    return {"status": "Password updated successfully"}