import httpx
import asyncio
import re
from db import insert_jobs_batch
from matcher import is_engineering_job

GREENHOUSE_COMPANIES = [
    # Original
    "airbnb", "stripe", "figma", "linear",
    "shopify", "dropbox", "square", "robinhood", "brex",
    "gusto", "lattice", "airtable", "asana",
    "chime", "instacart", "lyft",
    # AI / ML
    "anthropic", "scaleai",
    # Crypto / Fintech
    "coinbase", "ripple", "gemini", "carta", "mercury",
    # Infrastructure / DevOps
    "cloudflare", "datadog", "mongodb", "elastic",
    "twilio", "fivetran", "fastly", "newrelic", "pagerduty", "sumologic",
    "gitlab", "vercel", "netlify", "planetscale",
    # Consumer / Social
    "reddit", "discord", "duolingo", "roblox", "pinterest",
    "squarespace", "webflow", "toast",
    # Enterprise / SaaS
    "okta", "intercom", "faire",
    # Autonomous / Robotics
    "waymo", "nuro", "spacex",
    # Other
    "jetbrains", "databricks", "gusto", "ginkgobioworks",
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
    semaphore = asyncio.Semaphore(10)  # max 10 concurrent description fetches globally

    async with httpx.AsyncClient(timeout=15) as client:

        async def scrape_company(company):
            try:
                r = await client.get(
                    f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs"
                )
                if r.status_code != 200:
                    return []
                jobs = r.json().get("jobs", [])

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
                eng = [j for j in batch if is_engineering_job(j.get("title", ""))]
                print(f"  {company}: {len(eng)}/{len(jobs)} engineering jobs")
                return eng
            except Exception as e:
                print(f"  Error scraping {company}: {e}")
                return []

        # Scrape all companies in parallel
        results = await asyncio.gather(*[scrape_company(c) for c in GREENHOUSE_COMPANIES])
        for eng_batch in results:
            all_jobs.extend(eng_batch)

    await insert_jobs_batch(all_jobs)
    return len(all_jobs)
