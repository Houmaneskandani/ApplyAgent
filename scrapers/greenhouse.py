import httpx
import asyncio
import re
from db import insert_jobs_batch
from matcher import is_engineering_job

GREENHOUSE_COMPANIES = [
    "airbnb", "stripe", "notion", "figma", "linear",
    "shopify", "dropbox", "square", "robinhood", "brex",
    "gusto", "rippling", "lattice", "airtable", "asana",
    "benchling", "chime", "doordash", "instacart", "lyft"
]


def clean_html(html: str) -> str:
    import html as html_module
    # Decode HTML entities first (handles double-encoded content like &lt;h2&gt; → <h2>)
    html = html_module.unescape(html)
    # Remove script/style tags and their content, keep all other HTML for rendering
    html = re.sub(r'<(script|style)[^>]*>.*?</(script|style)>', '', html, flags=re.DOTALL)
    return html[:5000]


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
                eng_batch = [j for j in batch if is_engineering_job(j.get("title", ""))]
                all_jobs.extend(eng_batch)
                print(f"  {company}: {len(eng_batch)}/{len(batch)} engineering jobs")

            except Exception as e:
                print(f"  Error scraping {company}: {e}")

    await insert_jobs_batch(all_jobs)
    return len(all_jobs)
