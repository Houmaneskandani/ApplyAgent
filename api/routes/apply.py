from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException, Request
from api.auth import get_current_user, _rate_limit
from db import get_pool, update_application_status, get_user_credits, deduct_credits
from applier.greenhouse import apply_greenhouse
from applier.lever import apply_lever
from applier.ashby import apply_ashby
from applier.smartrecruiters import apply_smartrecruiters
from applier.workday import apply_workday
from applier.generic import apply_generic
import httpx
import tempfile
import json
import os


def extract_resume_text(pdf_path: str) -> str:
    """Extract plain text from a PDF resume to give Claude full context."""
    try:
        import pypdf
        with open(pdf_path, "rb") as f:
            reader = pypdf.PdfReader(f)
            text = ""
            for page in reader.pages:
                text += (page.extract_text() or "") + "\n"
        return text[:4000].strip()
    except ImportError:
        return ""
    except Exception:
        return ""

router = APIRouter()


async def _mint_fresh_resume_url(stored_url: str) -> str:
    """
    The resume_url we persist in users.resume_url is a signed Supabase URL,
    but Phase 1 cut signed-URL lifetime from 365 days to 1 hour for security.
    Old URLs in the DB are now expired (they 400 / 403 on download). Plus,
    Supabase invalidates signed tokens when the project is paused/resumed
    (which happened here when the user's trial expired). So before
    downloading, derive the storage path from the URL and mint a fresh
    short-lived signed URL.
    """
    import os, re
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
        return stored_url  # nothing we can do; fall back to the stored value
    m = re.search(r"/resumes/([^?]+)", stored_url or "")
    if not m:
        return stored_url
    storage_path = m.group(1)
    try:
        from supabase import create_client
        sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        signed = sb.storage.from_("resumes").create_signed_url(
            storage_path, 60 * 5  # 5 min — long enough to download, no longer
        )
        return signed.get("signedURL") or signed.get("signedUrl") or stored_url
    except Exception as e:
        print(f"  ⚠ Could not mint fresh signed URL: {e} — using stored URL")
        return stored_url


async def download_resume(resume_url: str) -> str:
    """Download resume from Supabase Storage to a temp file, return local path."""
    fresh_url = await _mint_fresh_resume_url(resume_url)
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(fresh_url)
        if r.status_code != 200:
            raise Exception(f"Failed to download resume: {r.status_code}")

        # Try Content-Type first, fall back to URL hint
        ct = (r.headers.get("content-type") or "").lower()
        if "pdf" in ct:
            suffix = ".pdf"
        elif "wordprocessing" in ct or "msword" in ct:
            suffix = ".docx"
        else:
            suffix = ".pdf" if "pdf" in fresh_url.lower() else ".docx"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(r.content)
        tmp.close()
        return tmp.name


def build_profile_text(user: dict, prefs: dict) -> str:
    skills = prefs.get("skills", [])
    skill_list = ", ".join(
        f"{s['name']} ({s.get('level', 'intermediate')})" if isinstance(s, dict) else str(s)
        for s in (skills or [])
    )
    langs = ', '.join([l if isinstance(l, str) else l.get('name', '') for l in prefs.get('languages', ['English'])])

    work_auth = prefs.get('work_auth', 'citizen')
    if work_auth == 'citizen':
        auth_text = 'US Citizen — no sponsorship needed'
    elif work_auth == 'sponsor':
        auth_text = 'Will require work visa sponsorship'
    else:
        auth_text = 'Authorized to work in the US — no sponsorship needed'

    work_pref = prefs.get('work_preference', 'remote')
    if isinstance(work_pref, list):
        work_pref = ', '.join(work_pref)

    salary_min = prefs.get('salary_min', '')
    salary_max = prefs.get('salary_max', '')
    salary_text = f"{salary_min} - {salary_max}".strip(' -') or 'Open to discuss / competitive market rate'

    return f"""
=== CANDIDATE PROFILE ===

Name: {user['name']}
Preferred name: {prefs.get('preferred_name', '') or 'Same as legal name'}
Email: {user['email']}
Phone: {prefs.get('phone', 'Not provided')}
Location: {prefs.get('city', '')}, {prefs.get('state', '')} {prefs.get('zip', '')}
Address: {prefs.get('address', '')} {prefs.get('address2', '')}
Country: {prefs.get('country', 'United States')}

--- EDUCATION ---
Highest degree: {prefs.get('degree', "Bachelor's")}
Field of study / Major: {prefs.get('major', 'Not provided')}
School / University: {prefs.get('school', 'Not provided')}
Graduation year: {prefs.get('graduation_year', 'Not provided')}

--- WORK AUTHORIZATION & PREFERENCES ---
Work authorization: {auth_text}
Work location preference: {work_pref}
Willing to relocate: {'Yes' if prefs.get('willing_to_relocate') else 'No'}{f" — target cities: {prefs.get('relocation_cities')}" if prefs.get('willing_to_relocate') and prefs.get('relocation_cities') else ''}
Employment type: {prefs.get('employment_type', 'Full-time')}

--- CURRENT EMPLOYMENT ---
Currently employed: {'Yes' if prefs.get('currently_employed', True) else 'No'}
Current employer: {prefs.get('employer', 'Not provided')}
Current job title: {prefs.get('job_title', 'Software Engineer')}
Total years of professional experience: {prefs.get('years_experience', 'Not provided')}
Notice period: {prefs.get('notice_period', '2 weeks')}
Available to start: {prefs.get('start_date', 'ASAP')}
Previously applied to this company: {'Yes' if prefs.get('previously_applied', False) else 'No'}

--- SKILLS & BACKGROUND ---
Skills: {skill_list or 'Not provided'}
People managed: {prefs.get('people_managed', '0')}
Security clearance: {prefs.get('security_clearance', 'None')}
Startup experience: {'Yes' if prefs.get('startup_experience', False) else 'No'}
Verify work history: {'Yes' if prefs.get('verify_work_history', True) else 'No'}

--- COMPENSATION ---
Salary expectation: {salary_text}

--- LINKS ---
LinkedIn: {prefs.get('linkedin', 'Not provided')}
GitHub: {prefs.get('github', 'Not provided')}
Portfolio: {prefs.get('portfolio', 'Not provided')}

--- BEHAVIORAL / ESSAY ANSWERS ---
Professional summary: {prefs.get('professional_summary', 'Not provided')}
Work motivation: {prefs.get('work_motivation', 'Not provided')}
Career highlight / proudest project: {prefs.get('career_highlight', 'Not provided')}
Greatest achievement: {prefs.get('greatest_achievement', 'Not provided')}
Challenging situation overcome: {prefs.get('challenging_situation', 'Not provided')}
Reason for leaving current role: {prefs.get('reason_for_leaving', 'Not provided')}

--- DEMOGRAPHIC (EEO) ---
Veteran: {'Yes, I am a protected veteran' if prefs.get('is_veteran', False) else 'No, I am not a protected veteran'}
Disability: {'Yes, I have a disability' if prefs.get('has_disability', False) else 'No, I do not have a disability'}
Gender: {prefs.get('gender', 'Decline to self identify')}
Pronouns: {prefs.get('pronouns', 'Not specified')}
Sexual orientation: {prefs.get('sexual_orientation', 'Decline to self identify')}
Race / Ethnicity: {prefs.get('race_ethnicity', 'Decline to self identify')}
Student: {'Yes' if prefs.get('is_student', False) else 'No'}
Languages: {langs}
Vaccinated: {'Yes' if prefs.get('vaccinated', True) else 'No'}
Willing to travel: {'Yes' if prefs.get('willing_to_travel', True) else 'No'}

--- LEGAL / COMPLIANCE ---
Background check: {'Yes, I consent' if prefs.get('background_check', True) else 'No'}
Driver\'s license: {'Yes' if prefs.get('drivers_license', True) else 'No'}
Drug test: {'Yes, I consent' if prefs.get('drug_test', True) else 'No'}
Criminal history: {'Yes' if prefs.get('criminal_history', False) else 'No criminal convictions'}
"""


async def get_user_info(user_id: int) -> dict:
    """Load user profile + preferences from database."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id, name, email, resume_url, preferences
            FROM users WHERE id = $1
        """, user_id)
        if not row:
            raise Exception("User not found")
        user = dict(row)

        # Parse preferences if it's a string
        prefs = user.get("preferences") or {}
        if isinstance(prefs, str):
            try:
                prefs = json.loads(prefs)
            except Exception:
                prefs = {}
        user["preferences"] = prefs
        return user


async def _set_step(user_id: int, job_id: int, step: str):
    """Update the notes field with the current progress step."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE applications SET notes = $1 WHERE user_id = $2 AND job_id = $3",
                step, user_id, job_id,
            )
    except Exception as e:
        # SECURITY/RELIABILITY: never block the main flow on a status-write
        # hiccup — but don't silently swallow either. A bare `except: pass`
        # here hid intermittent DB issues for months. The print() lands in
        # Railway logs + Sentry (via the LoggingIntegration once we migrate
        # off print). At minimum the user-facing progress UI stops moving
        # and we want to know why.
        print(f"  ⚠ _set_step failed user={user_id} job={job_id} step={step!r}: "
              f"{type(e).__name__}: {e}")


async def run_application(job: dict, user_id: int, dry_run: bool):
    tmp_resume_path = None
    job_id = job["id"]
    try:
        await _set_step(user_id, job_id, "Loading profile...")
        user = await get_user_info(user_id)
        prefs = user.get("preferences") or {}

        # Validate required profile fields
        missing = []
        if not (user.get("name") or "").strip():
            missing.append("Full name")
        if not (prefs.get("phone") or "").strip():
            missing.append("Phone number")
        if not user.get("resume_url"):
            missing.append("Resume upload")
        if missing:
            print(f"  ✗ Profile incomplete — missing: {', '.join(missing)}")
            await update_application_status(user_id, job_id, f"failed: missing {', '.join(missing)}")
            return

        await _set_step(user_id, job_id, "Downloading resume...")
        print(f"  Downloading resume for user {user_id}...")
        tmp_resume_path = await download_resume(user["resume_url"])
        print(f"  ✓ Resume at {tmp_resume_path}")

        name_parts = (user["name"] or "").split(" ", 1)
        location = ", ".join(filter(None, [
            prefs.get("city", ""),
            prefs.get("state", ""),
            prefs.get("zip", ""),
        ])).strip() or prefs.get("country", "United States")

        # SECURITY: imap_pass is encrypted at rest. Decrypt only inside this
        # short-lived in-memory dict that the applier consumes.
        from secrets_crypto import decrypt as _decrypt
        user_info = {
            "user_id": user_id,  # needed for per-user storage_state persistence
            "first_name": name_parts[0],
            "last_name": name_parts[1] if len(name_parts) > 1 else "",
            "email": user["email"],
            "phone": prefs.get("phone", ""),
            "resume_path": tmp_resume_path,
            "location": location,
            "linkedin": prefs.get("linkedin", ""),
            "github": prefs.get("github", ""),
            "website": prefs.get("portfolio", ""),
            "salary": f"{prefs.get('salary_min', '')}-{prefs.get('salary_max', '')}".strip("-") or "Open to discuss",
            "imap_user": prefs.get("imap_user", ""),
            "imap_pass": _decrypt(prefs.get("imap_pass", "")),
        }

        # Read-and-clear the force_submit flag atomically. If the user
        # clicked "Submit anyway" on a reviewer-blocked apply, this run
        # bypasses the reviewer; the flag resets so a future normal retry
        # gets the audit again. Doing it in one UPDATE prevents the flag
        # from leaking across retries on a transient queue restart.
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                fs_row = await conn.fetchrow("""
                    UPDATE applications SET force_submit = FALSE
                     WHERE user_id = $1 AND job_id = $2 AND force_submit = TRUE
                    RETURNING force_submit
                """, user_id, job_id)
            if fs_row is not None:
                user_info["_force_submit"] = True
                print(f"  🔓 Force-submit flag consumed for user={user_id} job={job_id} "
                      f"— reviewer will be bypassed this run.")
        except Exception as _e:
            # Defensive: never let force-submit infra break the apply. If
            # the read-and-clear fails, fall through with reviewer enabled.
            print(f"  ⚠ force_submit read-and-clear failed: {type(_e).__name__}: {_e}")

        await _set_step(user_id, job_id, "Reading resume...")
        profile_text = build_profile_text(user, prefs)
        resume_text = extract_resume_text(tmp_resume_path)
        if resume_text:
            profile_text += f"\n\n--- RESUME CONTENT ---\n{resume_text}\n--- END RESUME ---"
            print(f"  ✓ Resume text extracted ({len(resume_text)} chars)")
        else:
            print("  ⚠ Could not extract resume text (install pypdf: pip install pypdf)")

        url = job.get("url", "")
        source = job.get("source", "")

        await _set_step(user_id, job_id, "Opening job page...")
        if source == "greenhouse" or "greenhouse.io" in url or "gh_jid" in url:
            await _set_step(user_id, job_id, "Filling Greenhouse form...")
            result = await apply_greenhouse(
                job, dry_run=dry_run, user_info=user_info, profile_text=profile_text
            )
        elif source == "lever" or "lever.co" in url:
            await _set_step(user_id, job_id, "Filling Lever form...")
            result = await apply_lever(
                job, dry_run=dry_run, user_info=user_info, profile_text=profile_text
            )
        elif source == "ashby" or "ashby.io" in url or "ashbyhq.com" in url:
            await _set_step(user_id, job_id, "Filling Ashby form...")
            result = await apply_ashby(
                job, dry_run=dry_run, user_info=user_info, profile_text=profile_text
            )
        elif source == "smartrecruiters" or "smartrecruiters.com" in url:
            await _set_step(user_id, job_id, "Filling SmartRecruiters form...")
            result = await apply_smartrecruiters(
                job, dry_run=dry_run, user_info=user_info, profile_text=profile_text
            )
        elif source == "workday" or "myworkdayjobs.com" in url or "workday.com" in url:
            await _set_step(user_id, job_id, "Filling Workday form...")
            result = await apply_workday(
                job, dry_run=dry_run, user_info=user_info, profile_text=profile_text
            )
        else:
            await _set_step(user_id, job_id, "Trying generic form filler...")
            result = await apply_generic(
                job, dry_run=dry_run, user_info=user_info, profile_text=profile_text
            )

        # The reviewer-agent in greenhouse.py (and eventually the other
        # appliers) can stash a detailed "why we blocked" note on user_info
        # so we can surface it in the Needs Review tab. Prefer that over
        # the generic "Done: result" message.
        reviewer_notes = user_info.get("_reviewer_notes") if user_info else None
        await _set_step(user_id, job_id, reviewer_notes or f"Done: {result}")

        # For dry runs: reset status back to 'new' so the job stays in Job Matches
        # For live runs: update to the real result (applied / failed / unsupported)
        if dry_run:
            await update_application_status(user_id, job_id, "new")
        else:
            await update_application_status(user_id, job_id, result)

        # Deduct 0.4 credits on successful real application
        if result == "applied" and not dry_run:
            ok = await deduct_credits(user_id, 0.4)
            if ok:
                print(f"  ✓ Deducted 0.4 credits from user {user_id}")
            else:
                print(f"  ⚠ Could not deduct credits from user {user_id} (low balance?)")

        try:
            from notifications import notify_application
            # Pass the CANDIDATE's email so the apply-completion email lands
            # in THEIR inbox, not the operator's. (NOTIFY_EMAIL is still
            # CC'd for ops monitoring if it's set + different from user.)
            await notify_application(
                job.get("title", ""),
                job.get("company", ""),
                result,
                user_name=user.get("name", ""),
                user_email=user.get("email", ""),
            )
        except Exception:
            pass

    except Exception as e:
        import traceback
        print(f"  ✗ Application error: {e}")
        traceback.print_exc()
        await _set_step(user_id, job_id, f"Error: {e}")
        # Dry run errors reset to 'new' so the job stays visible in Job Matches
        await update_application_status(user_id, job_id, "new" if dry_run else "failed")
    finally:
        if tmp_resume_path and os.path.exists(tmp_resume_path):
            os.unlink(tmp_resume_path)
            print(f"  ✓ Cleaned up temp resume")


@router.post("/{job_id}/confirm")
@_rate_limit("20/minute")
async def confirm_unknown_apply(job_id: int, request: Request, user=Depends(get_current_user)):
    """
    User manually confirmed that an `unknown` apply landed successfully.
    Promote the row to `applied` and charge the credit (the bot did the
    actual work — Claude tokens, browser session — so this is fair).
    """
    user_id = user["user_id"]
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT status FROM applications WHERE user_id = $1 AND job_id = $2",
            user_id, job_id,
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Application not found")
        if existing["status"] == "applied":
            return {"status": "applied", "already": True}
        if existing["status"] != "unknown":
            raise HTTPException(
                status_code=400,
                detail=f"Can only confirm applications in 'unknown' state (current: {existing['status']})",
            )
        # Atomic credit deduction. If the user has 0 credits, allow the
        # confirmation but don't go negative — the bot already did the work,
        # we're not going to refuse a "yes it worked" confirmation. Best-effort.
        deducted = await deduct_credits(user_id, 0.4)
        await conn.execute(
            """
            UPDATE applications
               SET status = 'applied', applied_at = COALESCE(applied_at, NOW()),
                   notes = CASE WHEN $3 THEN 'Manually confirmed — credit charged'
                                ELSE 'Manually confirmed — credit waived (low balance)' END
             WHERE user_id = $1 AND job_id = $2
            """,
            user_id, job_id, deducted,
        )
    return {"status": "applied", "credit_deducted": deducted}


@router.post("/{job_id}/force-submit")
@_rate_limit("10/minute")
async def force_submit_apply(job_id: int, request: Request, user=Depends(get_current_user)):
    """
    User overrides a reviewer-blocked apply by clicking "Submit anyway".
    Re-queues the application with `force_submit = TRUE`, which run_application
    reads-and-clears atomically and passes to the reviewer as `_force_submit`
    in user_info; the reviewer then short-circuits with no block.

    Only valid when the current row is in `unknown` (reviewer blocked) or
    `failed` (e.g., transient submit error). We don't allow force-submit
    from `new` / `queued` / `applying` — those should go through normal
    /apply with full reviewer auditing.
    """
    user_id = user["user_id"]
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT status FROM applications WHERE user_id = $1 AND job_id = $2",
            user_id, job_id,
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Application not found")
        if existing["status"] not in ("unknown", "failed"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Force-submit only allowed for 'unknown' or 'failed' applications "
                    f"(current: {existing['status']})"
                ),
            )
        # Compute the queue position inside the same transaction as the
        # status flip so concurrent /force-submit + /apply requests can't
        # both observe the same COUNT.
        async with conn.transaction():
            cnt_row = await conn.fetchrow("""
                SELECT COUNT(*) AS cnt FROM applications
                WHERE user_id = $1 AND status IN ('queued', 'applying')
            """, user_id)
            position = (cnt_row["cnt"] or 0) + 1
            await conn.execute("""
                UPDATE applications
                   SET status = 'queued',
                       notes = 'Force-submit queued (reviewer bypassed)',
                       force_submit = TRUE,
                       dry_run = FALSE,
                       queue_position = $3
                 WHERE user_id = $1 AND job_id = $2
            """, user_id, job_id, position)
    # Kick the per-user queue worker. asyncio.create_task is fire-and-forget
    # which is fine — the worker is responsible for its own lifecycle and
    # idempotency (per-user asyncio.Lock + DB-level FOR UPDATE SKIP LOCKED).
    import asyncio as _asyncio
    from api.routes.queue import process_user_queue
    _asyncio.create_task(process_user_queue(user_id))
    return {"status": "queued", "queue_position": position}


@router.post("/{job_id}/retry")
@_rate_limit("20/minute")
async def retry_application(job_id: int, request: Request, user=Depends(get_current_user)):
    """
    Reset an `unknown` or `failed` row back to `new` so the user can re-queue it.
    Does NOT charge a credit (the new apply will charge if it succeeds).
    Also clears `force_submit` so a subsequent normal retry uses the reviewer.
    """
    user_id = user["user_id"]
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT status FROM applications WHERE user_id = $1 AND job_id = $2",
            user_id, job_id,
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Application not found")
        if existing["status"] in ("applied", "applying", "queued"):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot retry — application is {existing['status']}",
            )
        await conn.execute(
            "UPDATE applications SET status = 'new', notes = NULL, force_submit = FALSE "
            "WHERE user_id = $1 AND job_id = $2",
            user_id, job_id,
        )
    return {"status": "reset"}


@router.post("/{job_id}")
@_rate_limit("10/minute")
async def apply_to_job(
    job_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    dry_run: bool = True,
    user=Depends(get_current_user),
):
    user_id = user["user_id"]
    pool = await get_pool()
    async with pool.acquire() as conn:
        job = await conn.fetchrow("SELECT * FROM jobs WHERE id = $1", job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        user_row = await conn.fetchrow(
            "SELECT resume_url, credits FROM users WHERE id = $1", user_id
        )
        if not user_row or not user_row["resume_url"]:
            raise HTTPException(status_code=400, detail="Please upload a resume first")

        # Check credits (only for live mode — dry runs are free)
        if not dry_run:
            credits = float(user_row["credits"] or 0)
            if credits < 0.4:
                raise HTTPException(
                    status_code=402,
                    detail=f"Not enough credits ({credits:.1f} remaining). Please purchase more credits."
                )

        # Prevent re-queuing a job that's already being applied or was applied.
        # Previously this only blocked "applying" — meaning a user could apply
        # twice to the same role and be billed twice. Now we also block:
        #  - "applied"  : already submitted successfully
        #  - "queued"   : waiting in the queue (double-queue would skip the lock)
        # We deliberately ALLOW retry on "failed" and "unknown" so users can
        # retry a bot-blocked submission.
        existing = await conn.fetchrow(
            "SELECT status FROM applications WHERE user_id = $1 AND job_id = $2",
            user_id, job_id
        )
        if existing:
            status = existing["status"]
            if status == "applying":
                raise HTTPException(status_code=409, detail="This job is currently being applied")
            if status == "applied" and not dry_run:
                raise HTTPException(status_code=409, detail="You already applied to this job")
            if status == "queued":
                raise HTTPException(status_code=409, detail="This job is already in your queue")

    from db import add_to_queue
    position = await add_to_queue(user_id, job_id, dry_run)

    from api.routes.queue import process_user_queue
    background_tasks.add_task(process_user_queue, user_id)

    return {"status": "queued", "job_id": job_id, "position": position, "dry_run": dry_run}
