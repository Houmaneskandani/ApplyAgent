"""
JSearch via RapidAPI — aggregates LinkedIn Easy Apply + Indeed + Glassdoor.
Requires RAPIDAPI_KEY in .env (free tier: 500 req/month at rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch)
"""
import httpx
from db import insert_jobs_batch
from matcher import is_engineering_job
from config import RAPIDAPI_KEY

JSEARCH_QUERIES = [
    "software engineer",
    "backend engineer",
    "frontend engineer",
    "full stack engineer",
    "devops engineer",
    "machine learning engineer",
    "data engineer",
]


async def scrape_jsearch():
    if not RAPIDAPI_KEY:
        print("  JSearch (LinkedIn/Indeed): RAPIDAPI_KEY not set — skipping")
        print("    Get a free key at rapidapi.com → search 'JSearch'")
        return 0

    all_jobs = []
    seen_urls = set()

    async with httpx.AsyncClient(
        timeout=20,
        headers={
            "X-RapidAPI-Key": RAPIDAPI_KEY,
            "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
        },
    ) as client:
        for query in JSEARCH_QUERIES:
            try:
                r = await client.get(
                    "https://jsearch.p.rapidapi.com/search",
                    params={
                        "query": f"{query} remote",
                        "page": "1",
                        "num_pages": "2",
                        "date_posted": "week",
                    },
                )
                if r.status_code != 200:
                    print(f"  JSearch [{query}] HTTP {r.status_code}")
                    continue

                for job in r.json().get("data", []):
                    title = job.get("job_title", "")
                    if not is_engineering_job(title):
                        continue

                    url = job.get("job_apply_link", "") or job.get("job_google_link", "")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)

                    # Tag the original platform as source label
                    publisher = (job.get("job_publisher") or "").lower()
                    if "linkedin" in publisher:
                        source = "linkedin"
                    elif "indeed" in publisher:
                        source = "indeed"
                    elif "glassdoor" in publisher:
                        source = "glassdoor"
                    else:
                        source = "jsearch"

                    city = job.get("job_city", "") or ""
                    state = job.get("job_state", "") or ""
                    location = ", ".join(filter(None, [city, state])) or \
                               ("Remote" if job.get("job_is_remote") else "")

                    all_jobs.append({
                        "title": title,
                        "company": job.get("employer_name", ""),
                        "location": location,
                        "url": url,
                        "source": source,
                        "description": (job.get("job_description") or "")[:5000],
                    })
            except Exception as e:
                print(f"  JSearch [{query}] error: {e}")

    await insert_jobs_batch(all_jobs)
    linkedin = sum(1 for j in all_jobs if j["source"] == "linkedin")
    indeed = sum(1 for j in all_jobs if j["source"] == "indeed")
    print(f"  JSearch: {len(all_jobs)} jobs (LinkedIn: {linkedin}, Indeed: {indeed})")
    return len(all_jobs)
