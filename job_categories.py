"""
Job categories — the single source of truth for WHAT KINDS of jobs ApplyAgent
searches for, keeps (title filter), and tells the scorer the candidate wants.

Historically the whole product was hardcoded to software-engineering titles
(matcher.is_engineering_job + per-scraper query lists). This module
generalizes that so a user can opt into IT / DevOps / Data / etc. Each
category maps to:
  - queries:     search phrases fed to the query-based scrapers (ZipRecruiter,
                 JSearch). The search term IS the primary filter for those.
  - title_words: substrings that keep a scraped title (used by
                 matcher.is_engineering_job across ALL scrapers). EXCLUDE words
                 in matcher.py still always win (sales/marketing/PM/etc.).

The ACTIVE set is process-global, set once per scrape cycle by the scheduler
from the union of all users' preferences.job_categories. Defaults to
software_engineering so behavior is unchanged when nothing is configured
(keeps the existing matcher tests green).
"""
from __future__ import annotations

JOB_CATEGORIES: dict[str, dict] = {
    "software_engineering": {
        "label": "Software Engineering",
        "queries": [
            "software engineer", "backend engineer", "frontend engineer",
            "full stack engineer", "mobile engineer",
        ],
        # Mirrors the original ENGINEERING_TITLE_WORDS so the default behavior
        # (and the matcher unit tests) are unchanged.
        "title_words": [
            "engineer", "developer", "engineering", "software", "backend",
            "front end", "frontend", "full stack", "fullstack", "data", "ml ",
            "machine learning", "ai ", "artificial intelligence",
            "infrastructure", "devops", "sre", "platform", "mobile", "ios",
            "android", "cloud", "security engineer", "architect", "programmer",
            "golang", "python", "rust", "java", "reliability", "distributed",
            "systems", "api", "database", "embedded", "computer vision", "nlp",
            "deep learning", "robotics",
        ],
    },
    "it_helpdesk_sysadmin": {
        "label": "IT / Help Desk / Sysadmin",
        "queries": [
            "IT support specialist", "help desk technician",
            "systems administrator", "desktop support",
            "network administrator", "IT specialist",
        ],
        "title_words": [
            "it support", "help desk", "helpdesk", "service desk",
            "desktop support", "technical support", "support technician",
            "systems administrator", "system administrator", "sysadmin",
            "network administrator", "network admin", "network engineer",
            "it specialist", "it technician", "it analyst", "it administrator",
            "information technology", "field technician", "it manager",
        ],
    },
    "devops_cloud_sre": {
        "label": "DevOps / Cloud / SRE",
        "queries": [
            "devops engineer", "cloud engineer", "site reliability engineer",
            "platform engineer", "infrastructure engineer",
        ],
        "title_words": [
            "devops", "dev ops", "cloud engineer", "cloud architect",
            "site reliability", "sre", "platform engineer",
            "infrastructure engineer", "kubernetes", "aws engineer",
            "azure engineer", "gcp engineer", "automation engineer",
        ],
    },
    "data_analytics": {
        "label": "Data / Analytics",
        "queries": [
            "data analyst", "data engineer", "business intelligence analyst",
            "analytics engineer",
        ],
        "title_words": [
            "data analyst", "data engineer", "data scientist",
            "analytics engineer", "business intelligence", "bi analyst",
            "bi developer", "analytics", "reporting analyst", "data analytics",
        ],
    },
    "warehouse_logistics": {
        "label": "Warehouse & Logistics (local)",
        # local=True changes how scrapers SEARCH: instead of appending
        # "remote" they append the user's local area (preferences.
        # local_job_area) — these are in-person jobs near home, and a job
        # with no area to search in is skipped entirely.
        "local": True,
        "queries": [
            "warehouse associate", "warehouse worker", "forklift operator",
            "order picker", "package handler", "material handler",
            "shipping receiving clerk", "general warehouse",
        ],
        # Phrases, not bare words — a bare "warehouse" would false-match
        # "Data Warehouse Engineer" and pollute the professional list.
        "title_words": [
            "warehouse associate", "warehouse worker", "warehouse operative",
            "warehouse team", "warehouse specialist", "general warehouse",
            "forklift", "order picker", "order selector", "picker packer",
            "picker/packer", "package handler", "material handler",
            "shipping and receiving", "shipping receiving", "shipping clerk",
            "receiving clerk", "dock worker", "loader unloader",
            "freight handler", "inventory associate", "inventory clerk",
            "general labor", "general laborer", "warehouse lead",
            "fulfillment associate", "distribution center",
        ],
    },
}

DEFAULT_ACTIVE = ["software_engineering"]
_active: list[str] = list(DEFAULT_ACTIVE)

# Where to search for LOCAL (in-person) categories — e.g. "Santa Ana, CA".
# Set per scrape-cycle by the scheduler/worker from users' preferences
# (local_job_area, falling back to profile city/state). Empty = local
# categories are skipped by the query-based scrapers.
_local_area: str = ""


def set_local_area(area) -> None:
    global _local_area
    _local_area = (area or "").strip()


def local_area() -> str:
    return _local_area


def local_keys() -> set:
    """Category keys flagged local (in-person, searched near the user)."""
    return {k for k, v in JOB_CATEGORIES.items() if v.get("local")}


def set_active(keys) -> None:
    """Set the active category keys (invalid keys ignored; empty → default)."""
    global _active
    valid = [k for k in (keys or []) if k in JOB_CATEGORIES]
    _active = valid or list(DEFAULT_ACTIVE)


def active_keys() -> list[str]:
    return list(_active)


def active_queries() -> list[str]:
    """Deduped, order-preserving list of search phrases for active categories.

    NOTE: excludes LOCAL categories — their queries only make sense with an
    area attached; scrapers that support local search use active_query_specs().
    """
    out, seen = [], set()
    for k in _active:
        if JOB_CATEGORIES[k].get("local"):
            continue
        for q in JOB_CATEGORIES[k]["queries"]:
            if q not in seen:
                seen.add(q)
                out.append(q)
    return out


def active_query_specs() -> list[dict]:
    """
    Per-category search specs for scrapers that can vary search intent:
    [{"query", "category", "local"}]. Remote categories keep the historical
    "<query> remote" behavior; local categories search "<query> <area>" and
    are OMITTED entirely when no local area is configured.
    """
    out, seen = [], set()
    for k in _active:
        cat = JOB_CATEGORIES[k]
        is_local = bool(cat.get("local"))
        if is_local and not _local_area:
            continue  # nowhere to search — skip rather than search nationwide
        for q in cat["queries"]:
            if q in seen:
                continue
            seen.add(q)
            out.append({"query": q, "category": k, "local": is_local})
    return out


def active_title_words() -> set[str]:
    words: set[str] = set()
    for k in _active:
        words.update(JOB_CATEGORIES[k]["title_words"])
    return words


def labels(keys=None) -> list[str]:
    src = keys if keys is not None else _active
    return [JOB_CATEGORIES[k]["label"] for k in src if k in JOB_CATEGORIES]


async def resolve_active_from_db() -> None:
    """
    Set the active category set + local search area from ALL users' saved
    preferences (union). Called once per scrape cycle by BOTH the scheduler
    loop and the cron worker — the worker previously never resolved
    categories, so cron scrapes silently ran with the software-only default
    no matter what users selected.

    Local area precedence: first user's explicit preferences.local_job_area,
    else their profile city (+ state). Best-effort: any failure leaves the
    current (default) config in place.
    """
    import json as _json
    from db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT preferences FROM users")
    keys, area = set(), ""
    for r in rows:
        p = r["preferences"]
        if isinstance(p, str):
            try:
                p = _json.loads(p)
            except Exception:
                p = {}
        p = p or {}
        for k in p.get("job_categories", []) or []:
            keys.add(k)
        if not area:
            area = (p.get("local_job_area") or "").strip()
            if not area:
                city = (p.get("city") or "").strip()
                state = (p.get("state") or "").strip()
                area = ", ".join(x for x in (city, state) if x)
    set_active(list(keys))
    set_local_area(area)
    print(f"[Categories] active={active_keys()} local_area={_local_area or '(none)'}")
