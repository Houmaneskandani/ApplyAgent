import httpx
import asyncio
import re
from db import insert_jobs_batch

GREENHOUSE_COMPANIES = [
    "airbnb", "stripe", "notion", "figma", "linear",
    "shopify", "dropbox", "square", "robinhood", "brex",
    "gusto", "rippling", "lattice", "airtable", "asana",
    "benchling", "chime", "doordash", "instacart", "lyft"
]


def clean_html(html: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:2000]


async def fetch_description(client, company, job_id):
    try:
        url = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs/{job_id}"
        r = await client.get(url, timeout=10)
        if r.status_code == 200:
            return clean_html(r.json().get("content", ""))
    except:
        pass
    return ""


async def scrape_greenhouse():
    all_jobs = []
    async with httpx.AsyncClient(timeout=15) as client:
        for company in GREENHOUSE_COMPANIES:
            try:
                r = await client.get(
                    f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs"
                )
                if r.status_code != 200:
                    continue

                jobs = r.json().get("jobs", [])

                # Fetch all descriptions concurrently (max 10 at a time)
                semaphore = asyncio.Semaphore(10)

                async def fetch_with_limit(job):
                    async with semaphore:
                        job_id = job.get("id")
                        desc = await fetch_description(client, company, job_id)
                        return {
                            "title": job.get("title"),
                            "company": company.capitalize(),
                            "location": job.get("location", {}).get("name", ""),
                            "url": job.get("absolute_url"),
                            "source": "greenhouse",
                            "description": desc
                        }

                batch = await asyncio.gather(*[fetch_with_limit(j) for j in jobs])
                all_jobs.extend(batch)
                print(f"  {company}: {len(batch)} jobs with descriptions")

            except Exception as e:
                print(f"  Error scraping {company}: {e}")

    await insert_jobs_batch(all_jobs)
    return len(all_jobs)
