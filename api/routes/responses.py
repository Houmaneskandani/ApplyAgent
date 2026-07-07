"""
Response Inbox API — recruiter replies / interview invites found in the
user's Gmail. Scanning itself lives in response_scanner.py.
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from api.auth import get_current_user, _rate_limit
from db import get_pool

router = APIRouter()


@router.get("/")
async def list_responses(user=Depends(get_current_user)):
    """Newest-first responses + the job they matched (when we could link it)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT r.id, r.job_id, r.sender, r.subject, r.snippet, r.kind,
                   r.received_at, r.seen,
                   j.title AS job_title, j.company AS job_company, j.url AS job_url
              FROM responses r
              LEFT JOIN jobs j ON j.id = r.job_id
             WHERE r.user_id = $1
             ORDER BY r.received_at DESC
             LIMIT 200""", user["user_id"])
        unseen = await conn.fetchval(
            "SELECT COUNT(*) FROM responses WHERE user_id = $1 AND NOT seen",
            user["user_id"])
    out = []
    for r in rows:
        d = dict(r)
        d["received_at"] = str(d["received_at"]) if d["received_at"] else None
        out.append(d)
    return {"responses": out, "unseen": int(unseen or 0)}


@router.post("/scan")
@_rate_limit("3/minute")
async def trigger_scan(request: Request, user=Depends(get_current_user)):
    """On-demand inbox scan. Rate-limited — Google flags rapid IMAP logins."""
    from response_scanner import scan_user_responses
    try:
        new = await scan_user_responses(user["user_id"])
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Scan failed: {type(e).__name__}")
    return {"new_responses": new}


@router.put("/{response_id}/seen")
async def mark_seen(response_id: int, user=Depends(get_current_user)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE responses SET seen = TRUE WHERE id = $1 AND user_id = $2",
            response_id, user["user_id"])
    return {"ok": True}


@router.put("/seen-all")
async def mark_all_seen(user=Depends(get_current_user)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE responses SET seen = TRUE WHERE user_id = $1", user["user_id"])
    return {"ok": True}
