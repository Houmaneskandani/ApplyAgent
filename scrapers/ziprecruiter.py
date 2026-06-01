"""
ZipRecruiter search scraper (Playwright + stealth).

ZipRecruiter has an aggressive bot wall (every static fetch 403s), so we drive
a stealth browser session and anchor on the JSON-LD `JobPosting` blocks that ZR
embeds for SEO — far more stable than scraping card markup. Falls back to
`.job_content` cards if JSON-LD isn't present.

This is BEST-EFFORT: if ZR challenges the scrape, we log and return what we got
(0 is fine). The JSearch scraper also surfaces ZipRecruiter jobs via RapidAPI,
so this direct scraper is additive coverage, not the only path.
"""
import asyncio
import json
import re

from db import insert_jobs_batch
from matcher import is_engineering_job

ZR_QUERIES = [
    "software engineer",
    "backend engineer",
    "frontend engineer",
    "full stack engineer",
    "devops engineer",
    "data engineer",
    "machine learning engineer",
]

_SEARCH_URL = "https://www.ziprecruiter.com/jobs-search?search={kw}&location={loc}"

_CHALLENGE_SIGNALS = ("press & hold", "press and hold", "verify you are a human",
                      "are you a robot", "checking your browser")


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s or "")[:5000]


def _flatten_jsonld(obj, out):
    """Collect every JobPosting node out of arbitrarily-nested JSON-LD."""
    if isinstance(obj, list):
        for x in obj:
            _flatten_jsonld(x, out)
    elif isinstance(obj, dict):
        t = obj.get("@type")
        if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
            out.append(obj)
        # ItemList / @graph wrappers
        for key in ("itemListElement", "@graph", "item"):
            if key in obj:
                _flatten_jsonld(obj[key], out)


def _job_from_posting(p: dict) -> dict | None:
    title = (p.get("title") or "").strip()
    if not title or not is_engineering_job(title):
        return None
    org = p.get("hiringOrganization") or {}
    company = (org.get("name") if isinstance(org, dict) else "") or ""
    url = p.get("url") or p.get("@id") or ""
    if not url or "ziprecruiter" not in url.lower():
        return None
    # Location
    loc = ""
    jl = p.get("jobLocation")
    if isinstance(jl, list) and jl:
        jl = jl[0]
    if isinstance(jl, dict):
        addr = jl.get("address") or {}
        if isinstance(addr, dict):
            loc = ", ".join(filter(None, [addr.get("addressLocality"), addr.get("addressRegion")]))
    if not loc and (p.get("jobLocationType") == "TELECOMMUTE"):
        loc = "Remote"
    return {
        "title": title,
        "company": company.strip(),
        "location": loc or "",
        "url": url,
        "source": "ziprecruiter",
        "description": _strip_html(p.get("description", "")),
    }


async def scrape_ziprecruiter():
    all_jobs = []
    seen = set()

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("  ZipRecruiter: playwright not installed")
        return 0

    # Reuse the project's stealth session (random fingerprint + proxy + stealth
    # patches). No user_id — this is an anonymous scrape.
    from applier.browser_utils import stealth_session
    import job_categories
    # Search the roles the user(s) actually want (set by the scheduler from
    # preferences); falls back to software-engineering queries.
    queries = job_categories.active_queries() or ZR_QUERIES

    async with async_playwright() as p:
        for query in queries:
            url = _SEARCH_URL.format(kw=query.replace(" ", "+"), loc="Remote+(USA)")
            try:
                async with stealth_session(
                    p, url=url, user_id=None, persist_state=False,
                ) as (_b, _ctx, page):
                    await page.goto(url, timeout=45000, wait_until="domcontentloaded")
                    await asyncio.sleep(3)

                    try:
                        body = (await page.inner_text("body")).lower()
                    except Exception:
                        body = ""
                    if any(sig in body for sig in _CHALLENGE_SIGNALS):
                        print(f"  ZipRecruiter [{query}]: bot challenge — skipping")
                        continue

                    # JSON-LD JobPosting blocks (primary)
                    postings = []
                    try:
                        blocks = await page.locator(
                            "script[type='application/ld+json']"
                        ).all_text_contents()
                    except Exception:
                        blocks = []
                    for raw in blocks:
                        try:
                            _flatten_jsonld(json.loads(raw), postings)
                        except Exception:
                            continue

                    for p_ in postings:
                        j = _job_from_posting(p_)
                        if j and j["url"] not in seen:
                            seen.add(j["url"])
                            all_jobs.append(j)

                    # Fallback: card anchors if JSON-LD yielded nothing
                    if not postings:
                        try:
                            cards = await page.locator("[data-jobs-list] .job_content, .job_content").all()
                        except Exception:
                            cards = []
                        for c in cards:
                            try:
                                a = c.locator("a.title, .title a, a").first
                                href = await a.get_attribute("href")
                                title = (await a.inner_text()).strip()
                                if not href or "ziprecruiter" not in href.lower():
                                    continue
                                if not is_engineering_job(title) or href in seen:
                                    continue
                                seen.add(href)
                                all_jobs.append({
                                    "title": title, "company": "", "location": "",
                                    "url": href, "source": "ziprecruiter", "description": "",
                                })
                            except Exception:
                                continue
            except Exception as e:
                print(f"  ZipRecruiter [{query}] error: {type(e).__name__}: {e}")
                continue

    await insert_jobs_batch(all_jobs)
    print(f"  ZipRecruiter: {len(all_jobs)} jobs")
    return len(all_jobs)
