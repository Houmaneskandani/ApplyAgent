"""
Wellfound (AngelList) — startup jobs, direct founder contact.
Uses Playwright since the site is React-rendered.
"""
import asyncio
import json
import re
from db import insert_jobs_batch
from matcher import is_engineering_job

WELLFOUND_ROLES = [
    "software-engineer",
    "backend-engineer",
    "frontend-engineer",
    "full-stack-engineer",
    "devops-engineer",
    "data-engineer",
    "machine-learning-engineer",
]


def _strip_html(html: str) -> str:
    return re.sub(r'<[^>]+>', ' ', html or '')[:5000]


async def scrape_wellfound():
    all_jobs = []
    seen_urls = set()

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("  Wellfound: playwright not installed")
        return 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )

        for role in WELLFOUND_ROLES:
            try:
                page = await context.new_page()

                # Intercept XHR responses to capture job data
                job_data_from_network = []

                async def handle_response(response):
                    if "wellfound.com" in response.url and response.status == 200:
                        ct = response.headers.get("content-type", "")
                        if "json" in ct:
                            try:
                                body = await response.json()
                                if isinstance(body, dict) and "jobs" in body:
                                    job_data_from_network.extend(body["jobs"])
                            except Exception:
                                pass

                page.on("response", handle_response)

                url = f"https://wellfound.com/role/r/{role}"
                await page.goto(url, timeout=30000, wait_until="domcontentloaded")
                await asyncio.sleep(3)  # let XHR load

                # Try to extract from network responses first
                if job_data_from_network:
                    for job in job_data_from_network:
                        title = job.get("title") or job.get("jobType", "")
                        if not title or not is_engineering_job(title):
                            continue
                        job_url = job.get("url") or job.get("applyUrl") or ""
                        if not job_url or job_url in seen_urls:
                            continue
                        seen_urls.add(job_url)
                        all_jobs.append({
                            "title": title,
                            "company": (job.get("startup") or {}).get("name", ""),
                            "location": job.get("locationNames") or "Remote",
                            "url": job_url,
                            "source": "wellfound",
                            "description": _strip_html(job.get("description", "")),
                        })
                else:
                    # Fallback: parse __NEXT_DATA__ from HTML
                    content = await page.content()
                    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', content, re.DOTALL)
                    if m:
                        data = json.loads(m.group(1))
                        jobs = (
                            data.get("props", {})
                                .get("pageProps", {})
                                .get("jobs", [])
                        )
                        for job in jobs:
                            title = (job.get("jobType") or {}).get("name", "") or job.get("title", "")
                            if not is_engineering_job(title):
                                continue
                            slug = job.get("slug", "")
                            job_url = f"https://wellfound.com/jobs/{slug}" if slug else ""
                            if not job_url or job_url in seen_urls:
                                continue
                            seen_urls.add(job_url)
                            all_jobs.append({
                                "title": title,
                                "company": (job.get("startup") or {}).get("name", ""),
                                "location": ", ".join(job.get("locationNames") or ["Remote"]),
                                "url": job_url,
                                "source": "wellfound",
                                "description": _strip_html(job.get("description", "")),
                            })

                await page.close()
            except Exception as e:
                print(f"  Wellfound [{role}] error: {e}")
                try:
                    await page.close()
                except Exception:
                    pass

        await browser.close()

    await insert_jobs_batch(all_jobs)
    print(f"  Wellfound: {len(all_jobs)} jobs")
    return len(all_jobs)
