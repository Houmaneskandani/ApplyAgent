import asyncio
import os
import re
import tempfile
import anthropic
from db import get_unscored_jobs, upsert_application, get_user_prefs, get_pool
from config import ANTHROPIC_API_KEY

client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

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
    """Return True if the title matches one of the ACTIVE job categories.

    Defaults to software/engineering (so the matcher tests + historical
    behavior are unchanged), but broadens to IT / DevOps / Data / etc. when
    the scheduler has set active categories from users' preferences.
    EXCLUDE_TITLE_WORDS (sales/marketing/PM/...) always win.
    """
    t = title.lower()
    if any(w in t for w in EXCLUDE_TITLE_WORDS):
        return False
    import job_categories
    return any(w in t for w in job_categories.active_title_words())


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


async def _mint_fresh_resume_url(stored_url: str) -> str:
    """
    Phase 1 cut Supabase signed-URL lifetime from 365 days to 1 hour, so
    `users.resume_url` in the DB is often expired by the time we scrape +
    score. Parse the storage path out of the URL and mint a fresh signed
    URL. (Same logic as api/routes/apply.py — should probably be a shared
    helper, but for now the duplication is small and self-contained.)
    """
    import os as _os, re as _re
    SUPABASE_URL = _os.getenv("SUPABASE_URL")
    SUPABASE_SERVICE_KEY = _os.getenv("SUPABASE_SERVICE_KEY")
    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
        return stored_url
    m = _re.search(r"/resumes/([^?]+)", stored_url or "")
    if not m:
        return stored_url
    storage_path = m.group(1)
    try:
        from supabase import create_client
        sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        signed = sb.storage.from_("resumes").create_signed_url(storage_path, 60 * 5)
        return signed.get("signedURL") or signed.get("signedUrl") or stored_url
    except Exception as e:
        print(f"  ⚠ Matcher could not mint fresh resume URL: {e} — using stored")
        return stored_url


async def download_resume_from_url(resume_url: str) -> str:
    """Download resume from Supabase Storage and extract text."""
    try:
        import httpx
        # Mint a fresh signed URL — the stored one is likely expired
        # (Phase 1 dropped lifetime from 365 days to 1 hour).
        fresh_url = await _mint_fresh_resume_url(resume_url)
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(fresh_url)
            if r.status_code != 200:
                print(f"  ⚠ Resume URL returned {r.status_code} — scoring without resume content")
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

async def ai_score_job(job: dict, profile_summary: str, years_experience: str = "",
                       open_to_lower_level: bool = False) -> int:
    """Use Claude Haiku to score job-resume fit 0-10.

    open_to_lower_level: when True, the candidate is deliberately willing to
    take roles BELOW their experience level (e.g. a senior engineer also
    seeking entry IT / help desk / junior DevOps). We then stop penalizing
    'over-qualification' / seniority mismatch — otherwise those roles score
    below the auto-apply threshold and never get applied to.
    """
    title = job.get("title", "Unknown")
    company = job.get("company", "Unknown")
    description = (job.get("description", "") or "")[:2000]

    # Build seniority guidance from the candidate's actual years of experience
    # instead of hardcoding "4 years" — that was leaking the operator's profile
    # into every other user's scoring.
    try:
        yrs = int(str(years_experience).strip() or "0")
    except (TypeError, ValueError):
        yrs = 0
    if yrs >= 12:
        seniority_note = (
            f"- The candidate has ~{yrs} years experience. Staff/Principal/Director roles are appropriate."
        )
    elif yrs >= 7:
        seniority_note = (
            f"- The candidate has ~{yrs} years experience. Senior/Lead roles fit; Staff/Principal stretch."
        )
    elif yrs >= 3:
        seniority_note = (
            f"- The candidate has ~{yrs} years experience. Mid/Senior fits; Staff/Principal/Distinguished too senior (-2)."
        )
    elif yrs >= 1:
        seniority_note = (
            f"- The candidate has ~{yrs} years experience. Mid/Junior fits; Senior+ likely too senior (-2)."
        )
    else:
        seniority_note = "- Seniority unknown — judge fit from the rest of the profile/resume only."

    # When the candidate has opted into lower-level roles, override the
    # seniority guidance entirely — over-qualification is NOT a negative here.
    if open_to_lower_level:
        seniority_note = (
            "- The candidate is ACTIVELY OPEN to roles BELOW their experience level "
            "(e.g. entry/junior IT support, help desk, desktop support, junior DevOps, "
            "data/analytics). Do NOT penalize 'overqualification' or seniority mismatch — "
            "they WANT these roles. An experienced person applying to a lower-level role "
            "they can clearly do is a STRONG fit. Judge on: (a) can they do the job, and "
            "(b) is it in one of their target areas — NOT on whether the level matches."
        )

    prompt = f"""Rate how well this job matches this candidate. Reply with ONE integer 0-10.

{profile_summary}

JOB TITLE: {title}
COMPANY: {company}
DESCRIPTION:
{description}

Scoring rules:
9-10 = Excellent — most required skills match at the right level, good seniority fit
7-8  = Good — core skills match, minor gaps
5-6  = Partial — related field but missing key skills{"" if open_to_lower_level else " or wrong seniority"}
3-4  = Weak — different tech stack{"" if open_to_lower_level else " or off-target seniority"}
0-2  = Poor — wrong domain entirely
{("OVERRIDE — the candidate opted into lower-level roles: do NOT use 'wrong/off-target seniority' as a downgrade. A role they can clearly do in a target area scores 8-9 even if it's below their level." ) if open_to_lower_level else ""}

Seniority guidance:
{seniority_note}

Penalize (-2) if:
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

    for attempt in range(4):
        try:
            message = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=5,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip()
            # \b\d+\b matches whole numbers — prevents "15" from being parsed as "1"
            # (the previous \d+ would grab the first single digit).
            match = re.search(r"\b\d+\b", raw)
            if not match:
                print(f"    ⚠ Could not parse score from response: {raw!r}")
                # Treat parse failure as an error so the caller can count it.
                return 5, True
            return max(0, min(int(match.group()), 10)), False
        except Exception as e:
            msg = str(e)
            if "rate_limit" in msg or "429" in msg or "529" in msg:
                wait = 60 * (attempt + 1)
                print(f"    ⏳ Rate limited — waiting {wait}s (attempt {attempt+1}/4)...")
                await asyncio.sleep(wait)
            else:
                print(f"    ✗ AI scoring error: {e}")
                # Non-retryable. Return the safety-net 5 BUT mark it as
                # errored so the caller's tally doesn't mistake it for a
                # real score. This is the exact bug that hid 47 hours of
                # silent 401s after the worker's ANTHROPIC_API_KEY went
                # bad — the "Done — scored: 100" message looked healthy.
                return 5, True
    print(f"    ✗ Gave up scoring after 4 attempts (rate limit)")
    return 5, True


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

    # Per-user resume only. NEVER fall back to a committed local resume.pdf —
    # that would score one user's jobs against another user's resume, which is
    # a real multi-tenant correctness bug.
    resume_text = ""
    if resume_url:
        resume_text = await download_resume_from_url(resume_url)
        if resume_text:
            print(f"  ✓ Resume downloaded from storage ({len(resume_text)} chars)")
    if not resume_text:
        print(f"  ⚠ User {user_id} has no resume — AI will use profile info only")

    profile_summary = build_profile_summary(prefs, user, resume_text)

    # If the user opted into job categories beyond what their résumé
    # emphasizes (e.g. a software engineer also open to IT / DevOps roles),
    # tell the scorer those areas are DESIRED — otherwise it rates them low on
    # résumé-match alone and Auto Apply never queues them.
    _cat_keys = (prefs or {}).get("job_categories") or []
    if _cat_keys:
        try:
            import job_categories as _jc
            _tlabels = _jc.labels(_cat_keys)
            if _tlabels:
                profile_summary += (
                    "\n\nTARGET ROLE CATEGORIES (the candidate is ACTIVELY seeking "
                    "these — score a job in ANY of these areas as a strong fit even "
                    "if the résumé emphasizes a different area): "
                    + ", ".join(_tlabels) + "."
                )
        except Exception:
            pass

    print(f"  Scoring {len(jobs)} jobs for user {user_id}...")

    # Concurrency cap — Claude Haiku tier allows roughly 50 RPM with 50k TPM.
    # 5 concurrent in-flight requests is conservative; tune via env if needed.
    # The internal retry logic in ai_score_job handles transient 429s.
    SCORE_CONCURRENCY = int(os.getenv("MATCHER_CONCURRENCY", "5"))
    yrs_exp = prefs.get("years_experience", "")
    # When the user opts into lower-level roles, the scorer stops penalizing
    # over-qualification so entry/junior IT/DevOps/Data jobs can clear the
    # auto-apply threshold. Auto-on if they've chosen any non-SWE category.
    open_low = bool(prefs.get("open_to_lower_level"))
    if not open_low:
        _cats = prefs.get("job_categories") or []
        open_low = any(c != "software_engineering" for c in _cats)
    sem = asyncio.Semaphore(SCORE_CONCURRENCY)

    counters = {"scored": 0, "errored": 0, "skipped": 0}

    # Local commodity categories (warehouse/temp) are scored by RULE, not by
    # Claude — the fit question is "right title, right place", which a
    # substring check answers for free. AI-scoring "Warehouse Associate"
    # against a software résumé costs money AND gets the wrong answer.
    import job_categories as _jc
    _local_cat_keys = _jc.local_keys()
    _user_local_cats = set(prefs.get("job_categories") or []) & _local_cat_keys
    # Tokens >2 chars only: "Irvine, CA" must not produce a bare "ca" token,
    # which would substring-match Chicago/Casablanca/... and mis-score them 7.
    _local_area_tokens = [
        t.strip().lower()
        for t in ((prefs.get("local_job_area") or prefs.get("city") or "").split(","))
        if len(t.strip()) > 2
    ]

    def _rule_score_local(job_dict) -> int:
        """Fixed scores for local commodity jobs: 7 = in the user's area
        (auto-apply eligible), 5 = right title but area unconfirmed (visible,
        below the >=6 auto-apply gate)."""
        loc = (job_dict.get("location") or "").lower()
        if _local_area_tokens and any(t in loc for t in _local_area_tokens):
            return 7
        return 5

    async def score_one(job):
        job_dict = dict(job)
        title = job_dict.get("title", "")
        # Local commodity job (tagged by the scraper that found it): rule-based
        # score, zero Claude tokens. Only when this user actually opted into
        # the category — otherwise it's 0 like any other non-target job.
        if job_dict.get("category") in _local_cat_keys:
            if job_dict["category"] in _user_local_cats:
                await upsert_application(user_id, job_dict["id"], _rule_score_local(job_dict))
                counters["scored"] += 1
            else:
                await upsert_application(user_id, job_dict["id"], 0)
                counters["skipped"] += 1
            return
        # Pre-filter by title — no AI call needed
        if not is_engineering_job(title):
            await upsert_application(user_id, job_dict["id"], 0)
            counters["skipped"] += 1
            return
        async with sem:
            score, errored = await ai_score_job(
                job_dict, profile_summary, years_experience=yrs_exp,
                open_to_lower_level=open_low,
            )
        if errored:
            # API error / parse failure / rate-limit-give-up. Do NOT write the
            # safety-net 5 — get_unscored_jobs only re-picks rows with score
            # IS NULL, so a written 5 would brand the job permanently (one
            # below the apply threshold) with no retry path. Leaving it
            # unscored lets the next scrape cycle try again.
            counters["errored"] += 1
            return
        await upsert_application(user_id, job_dict["id"], score)
        counters["scored"] += 1
        if score >= 7:
            print(f"  [{score}/10] ✓ {title} @ {job_dict.get('company', '')}")
        elif score <= 3:
            print(f"  [{score}/10] ✗ {title} @ {job_dict.get('company', '')}")

    # Run all scoring tasks concurrently. asyncio.gather preserves order on
    # return but we don't care — each task writes to the DB independently.
    # return_exceptions=True so one failed score doesn't drop the rest.
    results = await asyncio.gather(*(score_one(j) for j in jobs), return_exceptions=True)
    errors = [r for r in results if isinstance(r, Exception)]
    if errors:
        print(f"  ⚠ {len(errors)} scoring task exceptions (logged above)")

    # Honest summary: distinguish real AI scores from default-on-error
    # writes. If `errored` is high (esp. == total), the worker's
    # ANTHROPIC_API_KEY is probably broken — that exact scenario hid for
    # 47 hours because the old "Done — scored: N" looked healthy.
    err_warning = ""
    if counters["errored"] > 0:
        total_scored_attempts = counters["scored"] + counters["errored"]
        err_pct = round(100 * counters["errored"] / max(total_scored_attempts, 1), 1)
        err_warning = f"  ⚠ {counters['errored']} ({err_pct}%) errored — check Anthropic API key + logs"
    print(
        f"\n  Done — scored OK: {counters['scored']}  |  "
        f"errored (defaulted to 5): {counters['errored']}  |  "
        f"skipped (not engineering): {counters['skipped']}"
    )
    if err_warning:
        print(err_warning)


if __name__ == "__main__":
    from config import USER_EMAIL, USER_NAME
    from db import get_or_create_user

    async def _run():
        uid = await get_or_create_user(USER_EMAIL, USER_NAME)
        await score_jobs(uid, rescore=True)

    asyncio.run(_run())
