import asyncio
import os
import re
import tempfile
import anthropic
from db import get_unscored_jobs, upsert_application, get_user_prefs, get_pool
from config import ANTHROPIC_API_KEY

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Title filters ──────────────────────────────────────────────────────────

# Jobs with any of these words in the title are NOT engineering — skip immediately
EXCLUDE_TITLE_WORDS = [
    "sales", "marketing", "recruiter", "recruiting", "sourcer",
    "accountant", "accounting", "finance", "financial analyst",
    "legal", "counsel", "attorney", "paralegal",
    "hr ", "human resources", "people ops",
    "graphic designer", "brand designer", "ux designer", "ui designer",
    "content writer", "copywriter", "technical writer",
    "product manager", "program manager", "project manager",
    "business development", "business analyst",
    "customer success", "customer support", "customer service",
    "account manager", "account executive",
    "office manager", "executive assistant", "administrative",
    "operations manager", "chief of staff",
]

# At least one of these must appear in the title for it to be worth scoring
ENGINEERING_TITLE_WORDS = [
    "engineer", "developer", "engineering", "software", "backend", "front end",
    "frontend", "full stack", "fullstack", "data", "ml ", "machine learning",
    "ai ", "artificial intelligence", "infrastructure", "devops", "sre",
    "platform", "mobile", "ios", "android", "cloud", "security engineer",
    "architect", "programmer", "golang", "python", "rust", "java",
    "reliability", "distributed", "systems", "api", "database", "embedded",
    "computer vision", "nlp", "deep learning", "robotics",
]


def is_engineering_job(title: str) -> bool:
    """Return True if job title looks like a software/engineering role."""
    t = title.lower()
    if any(w in t for w in EXCLUDE_TITLE_WORDS):
        return False
    return any(w in t for w in ENGINEERING_TITLE_WORDS)


# ── Resume helpers ─────────────────────────────────────────────────────────

def extract_text_from_pdf(path: str) -> str:
    try:
        import pypdf
        with open(path, "rb") as f:
            reader = pypdf.PdfReader(f)
            text = ""
            for page in reader.pages:
                text += (page.extract_text() or "") + "\n"
        return text[:3000].strip()
    except Exception:
        return ""


def load_local_resume() -> str:
    """Try to load resume.pdf from the job-bot directory."""
    path = os.path.join(os.path.dirname(__file__), "resume.pdf")
    if os.path.exists(path):
        return extract_text_from_pdf(path)
    return ""


async def download_resume_from_url(resume_url: str) -> str:
    """Download resume from Supabase Storage and extract text."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(resume_url)
            if r.status_code != 200:
                return ""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(r.content)
            tmp_path = tmp.name
        text = extract_text_from_pdf(tmp_path)
        os.unlink(tmp_path)
        return text
    except Exception as e:
        print(f"  ⚠ Could not download resume: {e}")
        return ""


# ── Profile summary builder ────────────────────────────────────────────────

def build_profile_summary(prefs: dict, user: dict = None, resume_text: str = "") -> str:
    """Build a rich profile text from all preferences + resume for AI scoring."""
    skills = prefs.get("skills", [])
    skill_lines = []
    for s in skills:
        if isinstance(s, dict):
            name = s.get("name", "")
            level = s.get("level", "intermediate")
            if name:
                skill_lines.append(f"  - {name}: {level}")
        elif isinstance(s, str) and s:
            skill_lines.append(f"  - {s}: intermediate")

    name = (user or {}).get("name", "Candidate")
    job_title = prefs.get("job_title", "Software Engineer")
    employer = prefs.get("employer", "")
    work_pref = prefs.get("work_preference", "remote")
    city = prefs.get("city", "")
    state = prefs.get("state", "")
    salary_min = prefs.get("salary_min", "")
    salary_max = prefs.get("salary_max", "")
    work_auth = prefs.get("work_auth", "authorized")
    keywords = prefs.get("keywords", "")
    startup = prefs.get("startup_experience", False)
    people_managed = prefs.get("people_managed", "0")
    clearance = prefs.get("security_clearance", "None")
    languages = prefs.get("languages", ["English"])

    summary = f"""CANDIDATE: {name}
Current/Desired Role: {job_title}
Current Employer: {employer or 'Not specified'}
Work Preference: {work_pref} (remote / hybrid / onsite)
Location: {city}, {state}
Work Authorization: {work_auth}
Salary Range: {salary_min or 'open'} – {salary_max or 'open'}

SKILLS (with proficiency level — beginner / intermediate / professional):
{chr(10).join(skill_lines) if skill_lines else '  Not specified'}

Additional keywords of interest: {keywords or 'None'}
Startup experience: {'Yes' if startup else 'No'}
People managed: {people_managed}
Security clearance: {clearance}
Languages: {', '.join(l if isinstance(l, str) else l.get('name','') for l in languages)}"""

    if resume_text:
        summary += f"\n\n--- RESUME ---\n{resume_text}\n--- END RESUME ---"

    return summary


# ── AI scoring ─────────────────────────────────────────────────────────────

async def ai_score_job(job: dict, profile_summary: str) -> int:
    """Use Claude Haiku to score job-resume fit 0-10."""
    title = job.get("title", "Unknown")
    company = job.get("company", "Unknown")
    description = (job.get("description", "") or "")[:2000]

    prompt = f"""Rate how well this job matches this candidate. Reply with ONE integer 0-10.

{profile_summary}

JOB TITLE: {title}
COMPANY: {company}
DESCRIPTION:
{description}

Scoring rules:
9-10 = Excellent — most required skills match at the right level, good seniority fit
7-8  = Good — core skills match, minor gaps
5-6  = Partial — related field but missing key skills or wrong seniority
3-4  = Weak — different tech stack or off-target seniority
0-2  = Poor — wrong domain entirely

Penalize (-2) if:
- Job requires 8+ years but candidate has ~4 years
- Job title is "Staff", "Principal", "Distinguished", or "Fellow" (too senior for 4yrs exp)
- Job is pure frontend (HTML/CSS/React only) but candidate is a backend engineer
- Job requires security clearance but candidate has none
- Job explicitly requires on-site and candidate prefers remote (or vice versa)

Boost (+1) if:
- Job uses technologies the candidate listed as "professional" level
- Job mentions "remote" and candidate prefers remote
- Job salary range is above candidate's minimum

IMPORTANT: The skill LEVEL matters.
- A candidate with "beginner" Python should NOT score high on a "Python expert required" job.
- A candidate with "professional" Go SHOULD score high on a Go backend job.

Reply with ONLY a single integer (0-10). Nothing else."""

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        match = re.search(r"\d+", raw)
        return max(0, min(int(match.group()), 10)) if match else 5
    except Exception as e:
        print(f"    ✗ AI scoring error: {e}")
        return 5


# ── Main scorer ────────────────────────────────────────────────────────────

async def score_jobs(user_id: int, resume_path: str = None, rescore: bool = False):
    jobs = await get_unscored_jobs(user_id, rescore=rescore)

    # Load full user info (prefs + resume_url)
    pool = await get_pool()
    async with pool.acquire() as conn:
        import json
        row = await conn.fetchrow(
            "SELECT name, preferences, resume_url FROM users WHERE id = $1", user_id
        )
    user = {"name": row["name"]} if row else {}
    prefs_raw = row["preferences"] if row else {}
    if isinstance(prefs_raw, str):
        prefs_raw = json.loads(prefs_raw) if prefs_raw else {}
    prefs = prefs_raw or {}
    resume_url = row["resume_url"] if row else None

    # Get resume text — prefer DB resume, fall back to local file
    resume_text = ""
    if resume_url:
        resume_text = await download_resume_from_url(resume_url)
        if resume_text:
            print(f"  ✓ Resume downloaded from storage ({len(resume_text)} chars)")
    if not resume_text:
        resume_text = load_local_resume()
        if resume_text:
            print(f"  ✓ Local resume.pdf loaded ({len(resume_text)} chars)")
        else:
            print("  ⚠ No resume found — AI will use profile info only")

    profile_summary = build_profile_summary(prefs, user, resume_text)

    print(f"  Scoring {len(jobs)} jobs for user {user_id}...")

    scored = 0
    skipped = 0

    for job in jobs:
        job_dict = dict(job)
        title = job_dict.get("title", "")

        # Pre-filter by title — no AI call needed
        if not is_engineering_job(title):
            await upsert_application(user_id, job_dict["id"], 0)
            skipped += 1
            continue

        score = await ai_score_job(job_dict, profile_summary)
        await upsert_application(user_id, job_dict["id"], score)
        scored += 1

        if score >= 7:
            print(f"  [{score}/10] ✓ {title} @ {job_dict.get('company', '')}")
        elif score <= 3:
            print(f"  [{score}/10] ✗ {title} @ {job_dict.get('company', '')}")

    print(f"\n  Done — scored: {scored}  |  skipped (not engineering): {skipped}")


if __name__ == "__main__":
    from config import USER_EMAIL, USER_NAME
    from db import get_or_create_user

    async def _run():
        uid = await get_or_create_user(USER_EMAIL, USER_NAME)
        await score_jobs(uid, rescore=True)

    asyncio.run(_run())
