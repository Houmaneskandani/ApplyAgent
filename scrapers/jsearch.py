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

# Direct-ATS host → source label. Mirrors classify_ats / the apply dispatcher
# so a job we re-tag here routes to the matching applier. Order = preference:
# these are the ATSes our Playwright appliers actually work on. JSearch often
# surfaces a job via an aggregator (ZipRecruiter/LinkedIn) while ALSO listing
# the employer's real ATS link in apply_options — we want the ATS link.
_ATS_HOSTS = [
    ("greenhouse.io", "greenhouse"),
    ("greenhouse",    "greenhouse"),   # boards.greenhouse.io / job-boards.greenhouse.io
    ("lever.co",      "lever"),
    ("ashbyhq.com",   "ashby"),
    ("ashby.io",      "ashby"),
    ("smartrecruiters.com", "smartrecruiters"),
    ("myworkdayjobs.com",   "workday"),
    ("workday.com",         "workday"),
]


def _ats_for_url(url: str) -> str | None:
    """Return the ATS source label if `url` is a known direct-ATS host."""
    u = (url or "").lower()
    for host, label in _ATS_HOSTS:
        if host in u:
            return label
    return None


def _pick_apply_link(job: dict) -> tuple[str, str | None]:
    """
    Choose the best apply link for a JSearch result.

    Returns (url, ats_label). Prefers a DIRECT-ATS link (Greenhouse/Lever/...)
    drawn from job_apply_link OR any apply_options[] entry, because that's
    where our appliers succeed — turning ZipRecruiter/LinkedIn into discovery
    channels that reroute to the real ATS. Falls back to the primary apply
    link, then the Google link. ats_label is None when no direct ATS matched.
    """
    candidates = []
    primary = job.get("job_apply_link") or ""
    if primary:
        candidates.append(primary)
    for opt in (job.get("apply_options") or []):
        link = (opt or {}).get("apply_link") or ""
        if link:
            candidates.append(link)
    # First candidate that is a direct ATS host wins.
    for link in candidates:
        label = _ats_for_url(link)
        if label:
            return link, label
    # No direct ATS — keep the primary apply link (or Google fallback).
    fallback = primary or job.get("job_google_link") or ""
    return fallback, None


async def scrape_jsearch():
    if not RAPIDAPI_KEY:
        print("  JSearch (LinkedIn/Indeed): RAPIDAPI_KEY not set — skipping")
        print("    Get a free key at rapidapi.com → search 'JSearch'")
        return 0

    all_jobs = []
    seen_urls = set()

    # Search the roles the user(s) actually want (set by the scheduler from
    # preferences); falls back to the default software-engineering queries.
    # Each spec carries its search intent: remote categories keep the
    # historical "<query> remote"; LOCAL categories (warehouse/temp) search
    # "<query> in <area>" near the user's home — appending "remote" there
    # would structurally hide every in-person job.
    import job_categories
    specs = job_categories.active_query_specs() or [
        {"query": q, "category": None, "local": False} for q in JSEARCH_QUERIES
    ]
    local_area = job_categories.local_area()

    async with httpx.AsyncClient(
        timeout=20,
        headers={
            "X-RapidAPI-Key": RAPIDAPI_KEY,
            "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
        },
    ) as client:
        for spec in specs:
            query = spec["query"]
            if spec["local"]:
                search_q = f"{query} in {local_area}"
            else:
                search_q = f"{query} remote"
            try:
                r = await client.get(
                    "https://jsearch.p.rapidapi.com/search",
                    params={
                        "query": search_q,
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

                    # Prefer a direct-ATS apply link (where our appliers work)
                    # over the aggregator's own link.
                    url, ats_label = _pick_apply_link(job)
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)

                    if ats_label:
                        # Re-tag to the underlying ATS so the dispatcher routes
                        # to that applier (e.g. ZipRecruiter listing → Greenhouse).
                        source = ats_label
                    else:
                        # No direct ATS — tag the original platform as the label.
                        publisher = (job.get("job_publisher") or "").lower()
                        apply_link = (job.get("job_apply_link") or "").lower()
                        if "ziprecruiter" in publisher or "ziprecruiter.com" in apply_link:
                            # ZR 1-Click applier + dashboard ATS chip grouping.
                            source = "ziprecruiter"
                        elif "linkedin" in publisher:
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
                        # Which category's query found this job. Drives the
                        # dashboard Professional/Warehouse toggle + rule-based
                        # scoring for local commodity jobs.
                        "category": spec["category"],
                    })
            except Exception as e:
                print(f"  JSearch [{query}] error: {e}")

    await insert_jobs_batch(all_jobs)
    linkedin = sum(1 for j in all_jobs if j["source"] == "linkedin")
    indeed = sum(1 for j in all_jobs if j["source"] == "indeed")
    zr = sum(1 for j in all_jobs if j["source"] == "ziprecruiter")
    rerouted = sum(1 for j in all_jobs if _ats_for_url(j["url"]))
    print(
        f"  JSearch: {len(all_jobs)} jobs (LinkedIn: {linkedin}, Indeed: {indeed}, "
        f"ZipRecruiter: {zr}, rerouted→ATS: {rerouted})"
    )
    return len(all_jobs)
