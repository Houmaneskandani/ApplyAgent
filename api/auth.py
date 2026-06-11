from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from passlib.context import CryptContext
from jose import jwt, JWTError
from datetime import datetime, timedelta
from db import get_pool
import os
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
    payload = {
        "user_id": user_id,
        "email": email,
        "exp": datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        return {"user_id": payload["user_id"], "email": payload["email"]}
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

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
            "UPDATE users SET reset_token = $1, reset_token_expires = $2 WHERE id = $3",
            code, expires, user["id"]
        )

        # Print to stdout only OUTSIDE production. Logging reset codes to Railway
        # makes them visible to anyone with log access.
        from config import IS_PROD
        if not IS_PROD:
            print(f"\n{'='*40}")
            print(f"  PASSWORD RESET CODE for {req.email}")
            print(f"  Code: {code}  (expires in 15 min)")
            print(f"{'='*40}\n")
        # TODO: send via SMTP/Twilio in production.

    return {"status": "If that email exists, a reset code has been sent"}


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

        # Constant-time compare so the 6-digit code can't be brute-forced via
        # response-timing (the != short-circuits on the first wrong digit).
        if not _secrets.compare_digest(str(user["reset_token"]), str(req.code)):
            raise HTTPException(status_code=400, detail="Invalid or expired reset code")

        if datetime.utcnow() > user["reset_token_expires"]:
            raise HTTPException(status_code=400, detail="Reset code has expired. Please request a new one.")

        # SECURITY: apply the SAME 72-byte truncation as signup/login.
        # Without this, a long password set via reset would be saved as bcrypt(full)
        # but login truncates to 72 bytes and the comparison would fail.
        hashed = pwd_context.hash(_bcrypt_prep(req.new_password))
        await conn.execute(
            "UPDATE users SET password_hash = $1, reset_token = NULL, reset_token_expires = NULL WHERE id = $2",
            hashed, user["id"]
        )

    return {"status": "Password updated successfully"}