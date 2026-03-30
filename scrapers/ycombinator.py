"""
Y Combinator — Work at a Startup (workatastartup.com).
High-quality YC-backed companies, strong on eng roles.
"""
import httpx
import json
import re
from bs4 import BeautifulSoup
from db import insert_jobs_batch
from matcher import is_engineering_job


def _strip_html(html: str) -> str:
    return re.sub(r'<[^>]+>', ' ', html or '')[:5000]


async def scrape_ycombinator():
    all_jobs = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json, text/html, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.workatastartup.com/",
    }

    async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as client:
        # Try internal JSON API first
        try:
            r = await client.get(
                "https://www.workatastartup.com/companies",
                params={"jobType": "fulltime", "query": "engineer", "remote": "true"},
            )
            # The page embeds next.js JSON — extract it
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")

                # Look for __NEXT_DATA__ script tag
                next_data = soup.find("script", id="__NEXT_DATA__")
                if next_data:
                    data = json.loads(next_data.string)
                    companies = (
                        data.get("props", {})
                            .get("pageProps", {})
                            .get("companies", [])
                    )
                    for company in companies:
                        company_name = company.get("name", "")
                        for job in company.get("jobs", []):
                            title = job.get("title", "")
                            if not is_engineering_job(title):
                                continue
                            url = f"https://www.workatastartup.com/jobs/{job.get('id', '')}"
                            all_jobs.append({
                                "title": title,
                                "company": company_name,
                                "location": job.get("location") or ("Remote" if job.get("remote") else ""),
                                "url": url,
                                "source": "ycombinator",
                                "description": _strip_html(job.get("description", "")),
                            })
        except Exception as e:
            print(f"  YC [companies page] error: {e}")

        # Fallback: hit the jobs search endpoint directly
        if not all_jobs:
            try:
                for query in ["software engineer", "backend", "frontend", "fullstack"]:
                    r = await client.get(
                        "https://www.workatastartup.com/jobs",
                        params={"q": query, "jobType": "fulltime", "remote": "true"},
                    )
                    if r.status_code != 200:
                        continue
                    soup = BeautifulSoup(r.text, "html.parser")
                    next_data = soup.find("script", id="__NEXT_DATA__")
                    if not next_data:
                        continue
                    data = json.loads(next_data.string)
                    jobs = (
                        data.get("props", {})
                            .get("pageProps", {})
                            .get("jobs", [])
                    )
                    for job in jobs:
                        title = job.get("title", "")
                        if not is_engineering_job(title):
                            continue
                        url = f"https://www.workatastartup.com/jobs/{job.get('id', '')}"
                        all_jobs.append({
                            "title": title,
                            "company": (job.get("company") or {}).get("name", ""),
                            "location": job.get("location") or "Remote",
                            "url": url,
                            "source": "ycombinator",
                            "description": _strip_html(job.get("description", "")),
                        })
            except Exception as e:
                print(f"  YC [jobs search] error: {e}")

    # Deduplicate by URL
    seen = set()
    unique = []
    for j in all_jobs:
        if j["url"] not in seen:
            seen.add(j["url"])
            unique.append(j)

    await insert_jobs_batch(unique)
    print(f"  Y Combinator: {len(unique)} jobs")
    return len(unique)
