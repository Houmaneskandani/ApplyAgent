"""
Hacker News "Ask HN: Who is hiring?" — monthly thread of direct-from-founder
job postings. High signal for SWE roles, zero cost (public Algolia API),
and many comments link straight to a Greenhouse/Lever posting our appliers
can handle.

Format convention (loosely followed): first line is
  "Company | Role | Location | REMOTE/ONSITE | salary..."
We parse best-effort; the title gate (is_engineering_job) filters the rest.
"""
import re

import httpx

from db import insert_jobs_batch
from matcher import is_engineering_job

ALGOLIA = "https://hn.algolia.com/api/v1"

# Direct-ATS links inside a comment become the job URL — those are the ones
# the bot can actually apply to. Otherwise the comment permalink is the URL
# (manual apply: email the founder / follow their instructions).
ATS_LINK = re.compile(
    r"https?://[^\s\"'<>]*(?:greenhouse\.io|lever\.co|ashbyhq\.com|"
    r"smartrecruiters\.com|myworkdayjobs\.com)[^\s\"'<>]*",
    re.I,
)


def _strip_html(html: str) -> str:
    text = re.sub(r"<p>", "\n", html or "")
    text = re.sub(r"<[^>]+>", " ", text)
    # Algolia returns HTML entities
    for ent, ch in [("&#x27;", "'"), ("&quot;", '"'), ("&amp;", "&"),
                    ("&gt;", ">"), ("&lt;", "<"), ("&#x2F;", "/")]:
        text = text.replace(ent, ch)
    return text.strip()


def _parse_comment(comment_html: str) -> dict | None:
    """Best-effort parse of a Who-is-hiring comment into job fields."""
    text = _strip_html(comment_html)
    if len(text) < 40:
        return None
    first_line = text.split("\n", 1)[0][:200]
    parts = [p.strip() for p in first_line.split("|") if p.strip()]
    company = parts[0][:100] if parts else ""
    title = parts[1][:150] if len(parts) > 1 else ""
    location = ""
    for p in parts[2:5]:
        pl = p.lower()
        if "remote" in pl or "onsite" in pl or "hybrid" in pl or "," in p:
            location = p[:100]
            break
    if not title:
        return None
    return {
        "company": company,
        "title": title,
        "location": location,
        "description": text[:5000],
    }


async def scrape_hackernews() -> int:
    """Scrape the latest 'Ask HN: Who is hiring?' thread."""
    all_jobs, seen = [], set()
    async with httpx.AsyncClient(timeout=25) as client:
        # 1. Find the newest official thread (posted by 'whoishiring').
        r = await client.get(f"{ALGOLIA}/search_by_date", params={
            "query": "Ask HN: Who is hiring?",
            "tags": "story,author_whoishiring",
            "hitsPerPage": 1,
        })
        hits = r.json().get("hits") or []
        if not hits:
            print("  HackerNews: no Who-is-hiring thread found")
            return 0
        story_id = hits[0]["objectID"]
        story_title = hits[0].get("title", "")

        # 2. Pull its top-level comments (paginated).
        for page in range(4):  # 4 x 100 comments covers most months
            r = await client.get(f"{ALGOLIA}/search_by_date", params={
                "tags": f"comment,story_{story_id}",
                "hitsPerPage": 100,
                "page": page,
            })
            comments = r.json().get("hits") or []
            if not comments:
                break
            for c in comments:
                # Only top-level comments are postings; replies are discussion.
                if str(c.get("parent_id")) != str(story_id):
                    continue
                parsed = _parse_comment(c.get("comment_text") or "")
                if not parsed:
                    continue
                if not is_engineering_job(parsed["title"]):
                    continue
                # Prefer a direct-ATS link (bot-appliable); else HN permalink.
                m = ATS_LINK.search(parsed["description"])
                url = m.group(0) if m else f"https://news.ycombinator.com/item?id={c['objectID']}"
                if url in seen:
                    continue
                seen.add(url)
                all_jobs.append({
                    **parsed,
                    "url": url,
                    "source": "hackernews",
                })

    await insert_jobs_batch(all_jobs)
    with_ats = sum(1 for j in all_jobs if "news.ycombinator.com" not in j["url"])
    print(f"  HackerNews [{story_title[:40]}]: {len(all_jobs)} jobs ({with_ats} with direct ATS links)")
    return len(all_jobs)
