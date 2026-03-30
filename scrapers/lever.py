import httpx
import html as html_module
from db import insert_jobs_batch
from matcher import is_engineering_job

LEVER_COMPANIES = [
    "netflix", "uber", "reddit", "scale-ai", "openai",
    "anthropic", "databricks", "plaid", "coinbase", "canva",
    "discord", "figma", "intercom", "carta", "checkr",
    "flexport", "gotinder", "headspace", "hippo", "hopin",
    "ironclad", "joinef", "lattice", "loom", "mercury",
    "mixpanel", "momentive", "netlify", "noom", "outreach"
]

async def scrape_lever():
    all_jobs = []
    async with httpx.AsyncClient(timeout=15) as client:
        for company in LEVER_COMPANIES:
            try:
                url = f"https://api.lever.co/v0/postings/{company}?mode=json"
                r = await client.get(url)
                if r.status_code != 200:
                    continue
                jobs = r.json()
                batch = [
                    {
                        "title": job.get("text"),
                        "company": company.capitalize(),
                        "location": (job.get("categories") or {}).get("location", ""),
                        "url": job.get("hostedUrl"),
                        "source": "lever",
                        "description": html_module.unescape(job.get("description") or job.get("descriptionPlain", ""))[:5000]
                    }
                    for job in jobs
                    if is_engineering_job(job.get("text", ""))
                ]
                all_jobs.extend(batch)
                print(f"  {company}: {len(batch)} engineering jobs")
            except Exception as e:
                print(f"  Error scraping {company}: {e}")

    await insert_jobs_batch(all_jobs)
    return len(all_jobs)
