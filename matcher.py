import re
from db import get_unscored_jobs, upsert_application, get_user_prefs

MUST_HAVE = ["engineer", "developer", "backend", "python", "software"]
NICE_TO_HAVE = ["senior", "lead", "remote", "api", "infrastructure"]
EXCLUDE = ["manager", "director", "sales", "marketing", "recruiter"]


def keyword_score(job: dict, prefs: dict = None) -> int:
    text = f"{job['title']} {job.get('description', '')}".lower()

    # Exclusions
    if any(word in text for word in EXCLUDE):
        return 0

    # User's exclude list
    if prefs:
        exclude_companies = prefs.get("exclude_companies", "")
        if exclude_companies and job.get("company", "").lower() in exclude_companies.lower():
            return 0

    score = 0

    # Base keyword matching
    score += sum(3 for word in MUST_HAVE if word in text)
    score += sum(1 for word in NICE_TO_HAVE if word in text)

    if prefs:
        # Match user's skills
        skills = prefs.get("skills", [])
        for skill in skills:
            skill_name = skill["name"] if isinstance(skill, dict) else skill
            skill_level = skill.get("level", "intermediate") if isinstance(skill, dict) else "intermediate"
            if skill_name.lower() in text:
                weight = {"beginner": 1, "intermediate": 2, "professional": 3}.get(skill_level, 2)
                score += weight

        # Match keywords from preferences
        keywords = prefs.get("keywords", "")
        if keywords:
            for kw in str(keywords).split(","):
                if kw.strip().lower() in text:
                    score += 2

        # Work preference matching
        work_pref = prefs.get("work_preference", "both")
        if work_pref == "remote":
            if "remote" in text:
                score += 3
            elif "on-site" in text or "onsite" in text or "in office" in text:
                score -= 3  # penalize on-site jobs
        elif work_pref == "onsite":
            if "remote" in text and "on-site" not in text:
                score -= 2  # penalize fully remote if they want onsite
        elif work_pref == "hybrid":
            if "hybrid" in text:
                score += 2
            elif "remote" in text:
                score += 1

        # Location match
        location = prefs.get("city", "") or prefs.get("location", "")
        if location and str(location).lower() in str(job.get("location", "")).lower():
            score += 2

        # Salary filter
        salary_min = prefs.get("salary_min", "")
        if salary_min:
            try:
                min_val = int(str(salary_min).replace("$", "").replace(",", "").replace("k", "000"))
                job_desc = job.get("description", "")
                salaries = re.findall(r"\$(\d+)(?:,(\d+))?k?", job_desc)
                if salaries:
                    job_max = max(int(s[0]) * (1000 if "k" in job_desc else 1) for s in salaries)
                    if job_max < min_val:
                        return 0
            except Exception:
                pass

    return max(0, min(score, 10))


async def score_jobs(user_id: int):
    jobs = await get_unscored_jobs(user_id)
    prefs = await get_user_prefs(user_id)
    print(f"  Scoring {len(jobs)} jobs with profile preferences...")

    if prefs.get("skills"):
        skill_names = [s["name"] if isinstance(s, dict) else s for s in prefs["skills"]]
        print(f"  Using skills: {', '.join(skill_names[:5])}{'...' if len(skill_names) > 5 else ''}")

    for job in jobs:
        score = keyword_score(dict(job), prefs)
        await upsert_application(user_id, job["id"], score)
        if score >= 7:
            print(f"  [{score}/10] {job['title']} @ {job['company']}")


if __name__ == "__main__":
    import asyncio
    from config import USER_EMAIL, USER_NAME
    from db import get_or_create_user

    async def _run():
        uid = await get_or_create_user(USER_EMAIL, USER_NAME)
        await score_jobs(uid)

    asyncio.run(_run())
