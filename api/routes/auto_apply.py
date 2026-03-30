import json
from fastapi import APIRouter, Depends
from api.auth import get_current_user
from db import get_pool

router = APIRouter()
DAILY_LIMIT = 10


@router.get("/")
async def get_auto_apply_status(user=Depends(get_current_user)):
    user_id = user["user_id"]
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT preferences FROM users WHERE id = $1", user_id)
        prefs = row["preferences"] or {}
        if isinstance(prefs, str):
            prefs = json.loads(prefs)

        applied_today = await conn.fetchval("""
            SELECT COUNT(*) FROM applications
            WHERE user_id = $1 AND status = 'applied' AND dry_run = false
            AND applied_at >= CURRENT_DATE
        """, user_id)

    return {
        "enabled": bool(prefs.get("auto_apply", False)),
        "applied_today": int(applied_today or 0),
        "daily_limit": DAILY_LIMIT,
    }


@router.post("/toggle")
async def toggle_auto_apply(user=Depends(get_current_user)):
    user_id = user["user_id"]
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT preferences FROM users WHERE id = $1", user_id)
        prefs = row["preferences"] or {}
        if isinstance(prefs, str):
            prefs = json.loads(prefs)

        new_val = not bool(prefs.get("auto_apply", False))
        prefs["auto_apply"] = new_val

        await conn.execute(
            "UPDATE users SET preferences = $1::jsonb WHERE id = $2",
            json.dumps(prefs), user_id,
        )

    if new_val:
        import asyncio
        from scheduler import auto_apply_for_user
        asyncio.create_task(auto_apply_for_user(user_id))

    return {
        "enabled": new_val,
        "applied_today": 0,
        "daily_limit": DAILY_LIMIT,
    }
