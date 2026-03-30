from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from passlib.context import CryptContext
from jose import jwt, JWTError
from datetime import datetime, timedelta
from db import get_pool
import os
import random

router = APIRouter()
security = HTTPBearer()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-this")
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 24 * 7

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
async def signup(req: SignupRequest):
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT id FROM users WHERE email = $1", req.email)
        if existing:
            raise HTTPException(status_code=400, detail="Email already registered")
        hashed = pwd_context.hash(req.password[:72])
        row = await conn.fetchrow("""
            INSERT INTO users (email, name, password_hash)
            VALUES ($1, $2, $3) RETURNING id
        """, req.email, req.name, hashed)
        token = create_token(row["id"], req.email)
        return {"token": token, "user_id": row["id"], "name": req.name}

@router.post("/login")
async def login(req: LoginRequest):
    pool = await get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, name, password_hash FROM users WHERE email = $1", req.email
        )
        if not user or not user["password_hash"]:
            raise HTTPException(status_code=401, detail="Invalid email or password")
        password = req.password[:72]  # bcrypt max is 72 bytes
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
async def forgot_password(req: ForgotPasswordRequest):
    pool = await get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT id, name FROM users WHERE email = $1", req.email)
        # Always return success to avoid revealing whether email exists
        if not user:
            return {"status": "If that email exists, a reset code has been sent"}

        code = f"{random.randint(0, 999999):06d}"
        expires = datetime.utcnow() + timedelta(minutes=15)

        await conn.execute(
            "UPDATE users SET reset_token = $1, reset_token_expires = $2 WHERE id = $3",
            code, expires, user["id"]
        )

        # TODO: Send via Twilio when configured
        # For now, print to terminal so you can test
        print(f"\n{'='*40}")
        print(f"  PASSWORD RESET CODE for {req.email}")
        print(f"  Code: {code}  (expires in 15 min)")
        print(f"{'='*40}\n")

    return {"status": "If that email exists, a reset code has been sent"}


@router.post("/reset-password")
async def reset_password(req: ResetPasswordRequest):
    pool = await get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, reset_token, reset_token_expires FROM users WHERE email = $1",
            req.email
        )
        if not user or not user["reset_token"]:
            raise HTTPException(status_code=400, detail="Invalid or expired reset code")

        if user["reset_token"] != req.code:
            raise HTTPException(status_code=400, detail="Invalid or expired reset code")

        if datetime.utcnow() > user["reset_token_expires"]:
            raise HTTPException(status_code=400, detail="Reset code has expired. Please request a new one.")

        hashed = pwd_context.hash(req.new_password)
        await conn.execute(
            "UPDATE users SET password_hash = $1, reset_token = NULL, reset_token_expires = NULL WHERE id = $2",
            hashed, user["id"]
        )

    return {"status": "Password updated successfully"}