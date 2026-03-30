"""
Dice.com — tech-specific job board.
Uses their internal search API (no key required).
"""
import httpx
from db import insert_jobs_batch
from matcher import is_engineering_job

DICE_QUERIES = [
    "software engineer",
    "backend engineer",
    "frontend engineer",
    "full stack engineer",
    "DevOps engineer",
    "data engineer",
    "ML engineer",
]


async def scrape_dice():
    all_jobs = []
    seen_urls = set()

    async with httpx.AsyncClient(
        timeout=20,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.dice.com/",
        },
    ) as client:
        for query in DICE_QUERIES:
            try:
                r = await client.get(
                    "https://job-search-api.dice.com/api/search",
                    params={
                        "q": query,
                        "countryCode": "US",
                        "radius": "30",
                        "radiusUnit": "mi",
                        "page": "1",
                        "pageSize": "50",
                        "language": "en",
                        "eid": "S2Q_,AQ_",
                        "filters.postedDate": "SEVEN",  # last 7 days
                    },
                )
                if r.status_code != 200:
                    continue

                data = r.json()
                for job in data.get("data", []):
                    title = job.get("title", "")
                    if not is_engineering_job(title):
                        continue
                    url = job.get("applyDataRequired", {}).get("applyUrl", "") or \
                          f"https://www.dice.com/jobs/detail/{job.get('id', '')}"
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    location = job.get("location", "") or \
                               ("Remote" if job.get("isRemote") else "")
                    all_jobs.append({
                        "title": title,
                        "company": job.get("companyPageUrl", "").split("/")[-1].replace("-", " ").title()
                                   or job.get("advertiserName", ""),
                        "location": location,
                        "url": url,
                        "source": "dice",
                        "description": (job.get("jobDescription") or "")[:5000],
                    })
            except Exception as e:
                print(f"  Dice [{query}] error: {e}")

    await insert_jobs_batch(all_jobs)
    print(f"  Dice: {len(all_jobs)} jobs")
    return len(all_jobs)
