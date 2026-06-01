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
}

DEFAULT_ACTIVE = ["software_engineering"]
_active: list[str] = list(DEFAULT_ACTIVE)


def set_active(keys) -> None:
    """Set the active category keys (invalid keys ignored; empty → default)."""
    global _active
    valid = [k for k in (keys or []) if k in JOB_CATEGORIES]
    _active = valid or list(DEFAULT_ACTIVE)


def active_keys() -> list[str]:
    return list(_active)


def active_queries() -> list[str]:
    """Deduped, order-preserving list of search phrases for active categories."""
    out, seen = [], set()
    for k in _active:
        for q in JOB_CATEGORIES[k]["queries"]:
            if q not in seen:
                seen.add(q)
                out.append(q)
    return out


def active_title_words() -> set[str]:
    words: set[str] = set()
    for k in _active:
        words.update(JOB_CATEGORIES[k]["title_words"])
    return words


def labels(keys=None) -> list[str]:
    src = keys if keys is not None else _active
    return [JOB_CATEGORIES[k]["label"] for k in src if k in JOB_CATEGORIES]
