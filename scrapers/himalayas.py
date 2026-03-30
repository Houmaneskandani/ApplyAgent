"""
Himalayas + Remote.co — free public JSON API, no key needed.
Covers fully remote engineering roles globally.
"""
import httpx
import re
from db import insert_jobs_batch
from matcher import is_engineering_job


def _strip_html(html: str) -> str:
    return re.sub(r'<[^>]+>', ' ', html or '')[:5000]


async def scrape_himalayas():
    all_jobs = []
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "Mozilla/5.0"}) as client:
        # Himalayas public API — returns up to 200 jobs per request
        for category in ["Engineering", "Software Development", "DevOps / Sysadmin"]:
            try:
                r = await client.get(
                    "https://himalayas.app/jobs/api",
                    params={"limit": 100, "categories[]": category},
                )
                if r.status_code != 200:
                    continue
                for job in r.json().get("jobs", []):
                    title = job.get("title", "")
                    if not is_engineering_job(title):
                        continue
                    url = job.get("applicationLink") or job.get("url", "")
                    if not url:
                        continue
                    all_jobs.append({
                        "title": title,
                        "company": (job.get("company") or {}).get("name", ""),
                        "location": ", ".join(job.get("locations") or ["Remote"]),
                        "url": url,
                        "source": "himalayas",
                        "description": _strip_html(job.get("description", ""))[:5000],
                    })
            except Exception as e:
                print(f"  Himalayas [{category}] error: {e}")

    await insert_jobs_batch(all_jobs)
    print(f"  Himalayas: {len(all_jobs)} jobs")
    return len(all_jobs)
