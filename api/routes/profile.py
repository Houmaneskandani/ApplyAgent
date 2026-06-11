from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, BackgroundTasks, Request
from api.auth import get_current_user, _rate_limit
from db import get_pool
from matcher import score_jobs
from secrets_crypto import encrypt, decrypt
from pydantic import BaseModel
import json
import os
import re
import uuid

router = APIRouter()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

# Resume upload limits.
MAX_RESUME_BYTES = 5 * 1024 * 1024  # 5 MB
ALLOWED_RESUME_EXTS = (".pdf", ".doc", ".docx")
# Magic-byte sniffing — never trust the client's Content-Type or filename alone.
RESUME_MAGIC = (
    (b"%PDF-", ".pdf"),                                       # PDF
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", ".doc"),            # MS OLE (legacy .doc)
    (b"PK\x03\x04", ".docx"),                                 # ZIP container (modern .docx)
)

# Keys in user.preferences that hold secrets we want to encrypt at rest.
ENCRYPTED_PREF_KEYS = ("imap_pass",)


def get_supabase():
    from supabase import create_client
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def _safe_resume_filename(original: str) -> str:
    """
    Build a safe storage filename. We discard the client filename entirely
    (path traversal, special chars, unicode tricks) and keep only the
    extension after validating against the allow-list.
    """
    ext = os.path.splitext(original or "")[1].lower()
    if ext not in ALLOWED_RESUME_EXTS:
        ext = ".pdf"
    return f"resume_{uuid.uuid4().hex}{ext}"


def _sniff_extension(content: bytes) -> str | None:
    for magic, ext in RESUME_MAGIC:
        if content.startswith(magic):
            return ext
    return None


def _parse_prefs(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return raw


def _strip_secret_prefs(prefs: dict) -> dict:
    """
    Build the client-safe view of preferences: strip every secret field and
    replace it with a `<key>_set: bool` flag. The UI uses that flag to render
    a "✓ Saved — enter new to change" hint without ever seeing the plaintext.

    Previously this function (then `_redact_secret_prefs`) DECRYPTED the IMAP
    password and shipped it cleartext to the browser on every GET /profile/.
    That meant the password sat in:
      - HTTPS response body (visible in devtools Network tab)
      - React state in memory
      - Possibly the browser's password autofill / history
    and was exfiltratable by any future XSS. The new contract is: secrets are
    write-only over the API. To rotate, the user types a new value; to keep
    the existing value, they leave the field empty (PUT merge logic below).
    """
    out = dict(prefs or {})
    for k in ENCRYPTED_PREF_KEYS:
        v = out.pop(k, None)
        out[f"{k}_set"] = bool(v)
    return out


def _merge_secret_prefs(new_prefs: dict, existing_prefs: dict) -> dict:
    """
    For every ENCRYPTED_PREF_KEY: if the client sent an empty/missing value
    but we already have an encrypted value in the DB, preserve the existing
    encrypted blob. Otherwise (non-empty new value), encrypt & store the new
    one. This is what makes "leave the field blank to keep your password"
    work for the UI.
    """
    from secrets_crypto import is_encrypted
    out = dict(new_prefs or {})
    for k in ENCRYPTED_PREF_KEYS:
        new_v = out.get(k)
        # Treat None / empty string / whitespace-only as "user didn't change it"
        is_empty = not isinstance(new_v, str) or not new_v.strip()
        if is_empty:
            existing_v = (existing_prefs or {}).get(k)
            if existing_v:
                out[k] = existing_v   # preserve existing (already-encrypted)
            else:
                out.pop(k, None)      # nothing here, nothing to store
        else:
            # New non-empty value — encrypt if not already encrypted.
            if not is_encrypted(new_v):
                out[k] = encrypt(new_v)
    return out


class ProfileUpdate(BaseModel):
    name: str | None = None
    preferences: dict = {}


@router.get("/")
async def get_profile(user=Depends(get_current_user)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id, name, email, resume_url, preferences, ziprecruiter_session
            FROM users WHERE id = $1
        """, user["user_id"])
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        result = dict(row)
        prefs = _parse_prefs(result.get("preferences"))
        # SECURITY: strip all encrypted secrets (e.g. imap_pass). The UI gets
        # boolean `<key>_set` flags so it can show a "saved" indicator without
        # the cleartext value ever crossing the network or sitting in browser
        # memory. To rotate a saved secret the user types a new value and the
        # PUT merge logic encrypts + replaces.
        result["preferences"] = _strip_secret_prefs(prefs)
        # ZipRecruiter session is an encrypted blob in its own column. NEVER
        # return it — only a boolean so the UI can show "Connected".
        result["ziprecruiter_session_set"] = bool(result.pop("ziprecruiter_session", None))
        return result


@router.put("/")
async def update_profile(data: dict, background_tasks: BackgroundTasks, user=Depends(get_current_user)):
    import time as _t
    _t0 = _t.perf_counter()
    user_id = user["user_id"]

    # Reject obviously oversized payloads. The /profile/ PUT historically
    # accepted an unbounded dict; a malicious client could send tens of MB.
    name = data.get("name")
    if name is not None and (not isinstance(name, str) or len(name) > 200):
        raise HTTPException(status_code=400, detail="Invalid name")

    prefs_in = data.get("preferences") or {}
    if not isinstance(prefs_in, dict):
        raise HTTPException(status_code=400, detail="preferences must be an object")
    try:
        encoded = json.dumps(prefs_in)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="preferences must be JSON-serializable")
    print(f"  [PUT /profile/] user={user_id} prefs_size={len(encoded)} bytes name_len={len(name or '')}")
    if len(encoded) > 64 * 1024:  # 64 KB
        print(f"  [PUT /profile/] REJECTED user={user_id}: prefs payload {len(encoded)} > 64KB")
        raise HTTPException(status_code=413, detail=f"Preferences too large ({len(encoded)} bytes; max 65536)")

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # Fetch the existing encrypted prefs so we can preserve secrets
            # the user didn't change. (UI sends imap_pass='' to mean "keep
            # what's already there" since we never ship the cleartext.)
            existing_row = await conn.fetchrow(
                "SELECT preferences FROM users WHERE id = $1", user_id,
            )
            existing_prefs = _parse_prefs(existing_row["preferences"]) if existing_row else {}
            # MERGE, don't REPLACE. The Profile form sends ONLY the keys it
            # manages (basics, demographics, IMAP, etc.) — but
            # `user.preferences` also holds backend-managed flags that no
            # frontend form ever touches: `auto_apply` (set by POST
            # /auto-apply/toggle), `last_scraped_at` (set by the scraper),
            # and potentially future ones. Without merging, every Profile
            # save SILENTLY WIPED auto_apply — diagnosed live at 04:07 when
            # the hourly monitor saw auto_apply=None despite the user having
            # toggled it on.
            #
            # Apply the incoming changes ON TOP of the existing prefs so
            # form fields the user changed take precedence, but non-form
            # keys survive.
            merged_prefs = {**(existing_prefs or {}), **prefs_in}
            try:
                prefs_encrypted = _merge_secret_prefs(merged_prefs, existing_prefs)
            except Exception as e:
                print(f"  [PUT /profile/] ENCRYPT FAILED user={user_id}: {type(e).__name__}: {e}")
                raise HTTPException(status_code=500, detail=f"Encryption error: {type(e).__name__}")
            await conn.execute("""
                UPDATE users SET name = COALESCE($1, name), preferences = $2
                WHERE id = $3
            """, name, json.dumps(prefs_encrypted), user_id)
    except HTTPException:
        # Don't re-wrap an HTTPException raised inside the try (e.g. by the
        # encryption block) as a generic "DB error" — let it pass through
        # with its own status code + detail.
        raise
    except Exception as e:
        print(f"  [PUT /profile/] DB UPDATE FAILED user={user_id}: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"DB error: {type(e).__name__}")

    # Rescore all jobs in background using the updated profile
    background_tasks.add_task(score_jobs, user_id, None, True)
    elapsed_ms = round((_t.perf_counter() - _t0) * 1000, 1)
    print(f"  [PUT /profile/] OK user={user_id} in {elapsed_ms}ms (rescore queued)")
    return {"status": "updated", "rescoring": True}


@router.post("/resume")
@_rate_limit("5/minute")
async def upload_resume(
    request: Request,
    file: UploadFile = File(...),
    background_tasks: BackgroundTasks = None,
    user=Depends(get_current_user),
):
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise HTTPException(status_code=500, detail="Storage not configured")

    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    if not file.filename.lower().endswith(ALLOWED_RESUME_EXTS):
        raise HTTPException(status_code=400, detail="Only PDF, DOC, DOCX allowed")

    # SECURITY: read with a size cap so a hostile client can't OOM the server.
    content = await file.read(MAX_RESUME_BYTES + 1)
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(content) > MAX_RESUME_BYTES:
        raise HTTPException(
            status_code=413, detail=f"File too large (max {MAX_RESUME_BYTES // (1024*1024)} MB)"
        )

    # Validate by magic bytes — Content-Type and the filename ext are both
    # client-controlled. A user could upload an EXE renamed to .pdf otherwise.
    if not _sniff_extension(content):
        raise HTTPException(status_code=400, detail="File contents do not look like a resume (PDF/DOC/DOCX)")

    # Build a safe storage path that discards the original filename entirely.
    safe_name = _safe_resume_filename(file.filename)
    file_path = f"{user['user_id']}/{safe_name}"

    supabase = get_supabase()
    supabase.storage.from_("resumes").upload(
        file_path,
        content,
        {"content-type": file.content_type or "application/pdf", "x-upsert": "true"}
    )

    # Short-lived signed URL (1 hour). Previously this was 365 DAYS, which
    # meant a once-compromised account leaked the resume URL permanently.
    # The frontend can request a fresh URL via /profile/resume/download whenever
    # it needs to render or download.
    signed = supabase.storage.from_("resumes").create_signed_url(file_path, 60 * 60)
    resume_url = signed.get("signedURL") or signed.get("signedUrl") or signed.get("path", "")

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET resume_url = $1 WHERE id = $2",
            resume_url, user["user_id"],
        )

    # Rescore all jobs with the new resume
    if background_tasks:
        background_tasks.add_task(score_jobs, user["user_id"], None, True)

    return {"resume_url": resume_url, "filename": safe_name, "rescoring": True}


@router.post("/test-imap")
@_rate_limit("5/minute")
async def test_imap(request: Request, user=Depends(get_current_user)):
    """Test whether the user's saved IMAP credentials work.

    SECURITY: tight rate limit (5/min). Google flags repeated IMAP logins as
    suspicious and burns App Passwords; without this an attacker who got
    a JWT could intentionally trigger lockouts.
    """
    import imaplib
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT preferences FROM users WHERE id = $1", user["user_id"])
    prefs = _parse_prefs(row["preferences"] if row else None)

    imap_user = (prefs or {}).get("imap_user", "")
    # SECURITY: IMAP password is stored encrypted in the DB; decrypt for runtime use.
    imap_pass = decrypt((prefs or {}).get("imap_pass", ""))
    if not imap_user or not imap_pass:
        raise HTTPException(status_code=400, detail="No IMAP credentials saved — fill in Gmail and App Password first")
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(imap_user, imap_pass)
        mail.logout()
        return {"ok": True, "message": f"✓ Connected to {imap_user} successfully"}
    except imaplib.IMAP4.error as e:
        err = str(e)
        if "AUTHENTICATIONFAILED" in err or "Invalid credentials" in err:
            raise HTTPException(status_code=400, detail="Wrong password — make sure you're using a Gmail App Password, not your regular Gmail password. Go to myaccount.google.com/apppasswords to generate one.")
        raise HTTPException(status_code=400, detail=f"IMAP error: {err}")
    except Exception as e:
        # Don't leak full stack details to clients
        print(f"  ⚠ IMAP test failed for user {user['user_id']}: {e}")
        raise HTTPException(status_code=500, detail="IMAP connection failed")


class ZipRecruiterSessionIn(BaseModel):
    # Playwright storage_state captured from a real logged-in browser, plus
    # the User-Agent it was issued to (clearance cookies are UA-bound, so the
    # applier must replay this exact UA).
    user_agent: str
    storage_state: dict


@router.post("/ziprecruiter-session")
@_rate_limit("10/minute")
async def save_ziprecruiter_session(
    payload: ZipRecruiterSessionIn,
    request: Request,
    user=Depends(get_current_user),
):
    """Store a captured ZipRecruiter login session (Fernet-encrypted at rest).

    The local capture script POSTs here after the user logs in once through a
    headed browser. We store the storage_state + UA so the apply worker can
    replay the authenticated session. SECURITY: the blob can contain auth
    cookies — it is encrypted with the same key as imap_pass and is NEVER
    returned by any GET (only a boolean `ziprecruiter_session_set`).
    """
    state = payload.storage_state or {}
    # Validate it actually looks like a Playwright storage_state so we don't
    # store junk that silently fails at apply time.
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list) or not state["cookies"]:
        raise HTTPException(
            status_code=400,
            detail="storage_state doesn't look valid (no cookies). Re-run the capture script after logging in.",
        )
    ua = (payload.user_agent or "").strip()
    if not ua:
        raise HTTPException(status_code=400, detail="user_agent is required (clearance cookies are UA-bound).")

    blob = json.dumps({"ua": ua, "state": state})
    encrypted = encrypt(blob)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET ziprecruiter_session = $1 WHERE id = $2",
            encrypted, user["user_id"],
        )
    n_cookies = len(state["cookies"])
    print(f"  ✓ ZipRecruiter session saved for user {user['user_id']} ({n_cookies} cookies)")
    return {"ok": True, "cookies": n_cookies, "message": "ZipRecruiter session connected."}


@router.delete("/ziprecruiter-session")
async def clear_ziprecruiter_session(user=Depends(get_current_user)):
    """Disconnect ZipRecruiter (clear the stored session)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET ziprecruiter_session = NULL WHERE id = $1",
            user["user_id"],
        )
    return {"ok": True, "message": "ZipRecruiter disconnected."}


@router.post("/rescore")
@_rate_limit("3/minute")
async def rescore_jobs(request: Request, background_tasks: BackgroundTasks, user=Depends(get_current_user)):
    """Rescore all jobs based on updated profile preferences."""
    background_tasks.add_task(score_jobs, user["user_id"])
    return {"status": "rescoring started"}


@router.get("/resume/download")
async def get_resume_url(user=Depends(get_current_user)):
    """
    Mint a FRESH short-lived signed URL each time. Previously we returned the
    stored URL, which was a 365-day token — anyone who once compromised an
    account leaked an immortal download link.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT resume_url FROM users WHERE id = $1", user["user_id"]
        )
        if not row or not row["resume_url"]:
            raise HTTPException(status_code=404, detail="No resume uploaded")

    # Try to derive storage path from the stored URL; fall back to the URL as-is.
    stored = row["resume_url"]
    storage_path = None
    m = re.search(r"/resumes/([^?]+)", stored or "")
    if m:
        storage_path = m.group(1)

    if not storage_path or not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
        # Best effort — return the stored URL. (Older accounts may have
        # a different URL shape; we don't want to break their access.)
        return {"resume_url": stored}

    try:
        signed = get_supabase().storage.from_("resumes").create_signed_url(
            storage_path, 60 * 10  # 10 min — long enough to download, short enough to be safe
        )
        return {"resume_url": signed.get("signedURL") or signed.get("signedUrl") or stored}
    except Exception as e:
        print(f"  ⚠ Failed to mint signed URL for user {user['user_id']}: {e}")
        return {"resume_url": stored}
