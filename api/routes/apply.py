from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException
from api.auth import get_current_user
from db import get_pool, update_application_status
from applier.greenhouse import apply_greenhouse
from applier.lever import apply_lever
import httpx
import tempfile
import json
import os

router = APIRouter()


async def download_resume(resume_url: str) -> str:
    """Download resume from Supabase Storage to a temp file, return local path."""
    async with httpx.AsyncClient() as client:
        r = await client.get(resume_url)
        if r.status_code != 200:
            raise Exception(f"Failed to download resume: {r.status_code}")

        suffix = ".pdf" if "pdf" in resume_url.lower() else ".docx"
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

    return f"""
Name: {user['name']}
Email: {user['email']}
Phone: {prefs.get('phone', 'Not provided')}
Location: {prefs.get('city', '')}, {prefs.get('state', '')} {prefs.get('zip', '')}
Address: {prefs.get('address', '')} {prefs.get('address2', '')}
Country: {prefs.get('country', 'United States')}

Work authorization: {'US Citizen - no sponsorship needed' if prefs.get('work_auth') == 'citizen' else 'Will require sponsorship' if prefs.get('work_auth') == 'sponsor' else 'Authorized, no sponsorship needed'}
Work preference: {prefs.get('work_preference', 'remote')}

Current employer: {prefs.get('employer', 'Not provided')}
Current job title: {prefs.get('job_title', 'Software Engineer')}
People managed: {prefs.get('people_managed', '0')}
Security clearance: {prefs.get('security_clearance', 'None')}
Startup experience: {'Yes' if prefs.get('startup_experience', False) else 'No'}
Start date: {prefs.get('start_date', 'ASAP')}
Verify work history: {'Yes' if prefs.get('verify_work_history', True) else 'No'}

Skills: {skill_list or 'Not provided'}

Salary expectation: {prefs.get('salary_min', 'Open to discuss')} - {prefs.get('salary_max', 'Open to discuss')}

LinkedIn: {prefs.get('linkedin', 'Not provided')}
GitHub: {prefs.get('github', 'Not provided')}
Portfolio: {prefs.get('portfolio', 'Not provided')}

Career highlight: {prefs.get('career_highlight', 'Not provided')}
Challenging situation: {prefs.get('challenging_situation', 'Not provided')}

Demographic:
- Veteran: {'Yes, I am a protected veteran' if prefs.get('is_veteran', False) else 'No, I am not a protected veteran'}
- Disability: {'Yes' if prefs.get('has_disability', False) else 'No, I do not have a disability'}
- Gender: {prefs.get('gender', 'Decline to self identify')}
- Student: {'Yes' if prefs.get('is_student', False) else 'No'}
- Vaccinated: {'Yes' if prefs.get('vaccinated', True) else 'No'}
- Willing to travel: {'Yes' if prefs.get('willing_to_travel', True) else 'No'}
- Background check: {'Yes' if prefs.get('background_check', True) else 'No'}
- Languages: {', '.join([l if isinstance(l, str) else l.get('name', '') for l in prefs.get('languages', ['English'])])}
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


async def run_application(job: dict, user_id: int, dry_run: bool):
    tmp_resume_path = None
    try:
        user = await get_user_info(user_id)
        prefs = user.get("preferences") or {}

        if not user.get("resume_url"):
            print(f"  ✗ User {user_id} has no resume uploaded")
            await update_application_status(user_id, job["id"], "failed")
            return

        print(f"  Downloading resume for user {user_id}...")
        tmp_resume_path = await download_resume(user["resume_url"])
        print(f"  ✓ Resume at {tmp_resume_path}")

        name_parts = (user["name"] or "").split(" ", 1)
        location = ", ".join(filter(None, [
            prefs.get("city", ""),
            prefs.get("state", ""),
            prefs.get("zip", ""),
        ])).strip() or prefs.get("country", "United States")

        user_info = {
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
        }

        profile_text = build_profile_text(user, prefs)

        url = job.get("url", "")
        source = job.get("source", "")

        if source == "greenhouse" or "greenhouse.io" in url or "gh_jid" in url:
            result = await apply_greenhouse(
                job, dry_run=dry_run, user_info=user_info, profile_text=profile_text
            )
        elif source == "lever" or "lever.co" in url:
            result = await apply_lever(
                job, dry_run=dry_run, user_info=user_info, profile_text=profile_text
            )
        else:
            result = "unsupported"

        await update_application_status(user_id, job["id"], result)

    except Exception as e:
        print(f"  ✗ Application error: {e}")
        await update_application_status(user_id, job["id"], "failed")
    finally:
        if tmp_resume_path and os.path.exists(tmp_resume_path):
            os.unlink(tmp_resume_path)
            print(f"  ✓ Cleaned up temp resume")


@router.post("/{job_id}")
async def apply_to_job(
    job_id: int,
    background_tasks: BackgroundTasks,
    dry_run: bool = True,
    user=Depends(get_current_user),
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        job = await conn.fetchrow("SELECT * FROM jobs WHERE id = $1", job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        user_row = await conn.fetchrow(
            "SELECT resume_url FROM users WHERE id = $1", user["user_id"]
        )
        if not user_row or not user_row["resume_url"]:
            raise HTTPException(status_code=400, detail="Please upload a resume first")

        background_tasks.add_task(
            run_application, dict(job), user["user_id"], dry_run
        )
        return {"status": "started", "job_id": job_id, "dry_run": dry_run}
