import asyncio
import hashlib
import os
import re
import time
import anthropic
from playwright.async_api import async_playwright
from config import ANTHROPIC_API_KEY
from applier.browser_utils import stealth_session, wait_for_captcha_if_present, trusted_click

client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# ─── get_answer cache ───────────────────────────────────────────────────
#
# Why: across ~50 Greenhouse/Ashby/Lever applications the same questions
# show up over and over — gender, race, veteran status, sponsorship,
# acknowledgment-of-EEO policy, etc. Previously each application paid full
# Claude tokens to answer them again. Now we cache (question, field_type,
# profile_hash) -> answer for 24 hours; the same profile asking the same
# question gets an instant cached reply.
#
# The cache key includes a hash of profile_text so that a profile update
# transparently invalidates all stale answers.
_ANSWER_CACHE: dict[tuple[str, str, str], tuple[str, float]] = {}
_ANSWER_CACHE_TTL = 60 * 60 * 24  # 24 hours
_ANSWER_CACHE_MAX = 2000          # rough cap to avoid unbounded growth


def _profile_hash(profile_text: str) -> str:
    """Stable short hash of the profile content used as a cache partition key."""
    if not profile_text:
        return "_"
    return hashlib.sha256(profile_text.encode("utf-8")).hexdigest()[:16]


def _normalize_question(q: str) -> str:
    """Strip whitespace + lowercase so 'Are you a veteran? ' and 'are you a veteran?' share a cache slot."""
    return re.sub(r"\s+", " ", (q or "").strip().lower())


def _cache_get(question: str, field_type: str, profile_text: str) -> str | None:
    key = (_normalize_question(question), field_type, _profile_hash(profile_text))
    hit = _ANSWER_CACHE.get(key)
    if hit is None:
        return None
    answer, ts = hit
    if time.time() - ts > _ANSWER_CACHE_TTL:
        _ANSWER_CACHE.pop(key, None)
        return None
    return answer


def _cache_set(question: str, field_type: str, profile_text: str, answer: str) -> None:
    if not answer:
        return
    if len(_ANSWER_CACHE) >= _ANSWER_CACHE_MAX:
        # Evict the oldest entry (simple FIFO; not LRU but cheap and adequate)
        oldest = min(_ANSWER_CACHE.items(), key=lambda kv: kv[1][1], default=None)
        if oldest:
            _ANSWER_CACHE.pop(oldest[0], None)
    key = (_normalize_question(question), field_type, _profile_hash(profile_text))
    _ANSWER_CACHE[key] = (answer, time.time())


async def get_answer(question: str, field_type: str, profile_text: str = None) -> str:
    if not profile_text:
        raise ValueError("profile_text is required — no hardcoded profile fallback")
    profile = profile_text

    # Cache hit — return without burning a Claude token. EEOC/sponsorship/
    # consent answers are stable across applications for a given profile,
    # so this is a major cost reduction on the second+ application.
    cached = _cache_get(question, field_type, profile_text)
    if cached is not None:
        return cached

    prompt = f"""
You are an expert job application assistant filling out a form on behalf of this candidate:

{profile}

Your job is to answer ANY question intelligently — whether it's about the candidate's background, 
behavioral questions, technical questions, or general questions.

Question: "{question}"
Field type: {field_type}

Instructions:
- For factual questions (name, phone, location, salary etc): use the candidate's profile data
- For yes/no questions: reply with just "Yes" or "No" based on their profile
- For dropdown/multiple choice: reply with the single best answer
- For behavioral questions ("describe a time when...", "what's your greatest strength"):
  Write a concise, professional answer using their real experience from the profile.
  Make it sound natural and genuine, not generic.
- For motivation questions ("why do you want to work here", "what interests you"):
  Write enthusiastically based on their skills and the role
- For technical questions ("describe your experience with X"):
  Answer honestly based on their skills — if they have it, elaborate; if not, say they're familiar with similar technologies
- For demographic questions (veteran, disability, gender, race):
  Use their exact profile data — never assume or guess these.
  Pick the SIMPLEST option that matches. If profile says "male", pick "Male" or "Man", NOT "Cisgender Male".
  If profile says "decline", pick the "decline" / "prefer not to say" option.
- For questions asking if the candidate NEEDS or REQUIRES visa sponsorship, work permit sponsorship,
  immigration sponsorship, work authorization sponsorship — now or in the future:
  If work_auth is 'citizen' → answer "No" (US Citizens do NOT need sponsorship)
  If work_auth is 'authorized' → answer "No" (already authorized, no sponsorship needed)
  If work_auth is 'sponsor' → answer "Yes"
  NEVER answer "Yes" for sponsorship if work_auth is 'citizen' or 'authorized'.
- For "how did you hear about this job" / "how did you find this role" / "referral source":
  Answer: "Online job search"
- For ANY question that is an acknowledgment, consent, or compliance statement — including questions starting with
  "I acknowledge", "I agree", "I confirm", "I understand", "I certify", "[Company] adheres to",
  "[Company] is committed to", or asking you to confirm awareness of a work arrangement
  (hybrid, office, travel, schedule, background check, drug test, equal opportunity, laws/regulations):
  ALWAYS answer "Yes" — the candidate already chose to apply, so they accept the terms.
  NEVER answer "No" for acknowledgment/consent/compliance questions unless profile explicitly says otherwise.
- For conditional questions like "If you answered Yes above, please describe..." where the relevant
  prior answer was "No": reply with just "N/A". Never write meta-commentary or say you don't have info.
- For unknown questions with no profile data:
  Give a professional, reasonable answer that a strong software engineer candidate would give.
  If the resume content is included above, use it to answer education/work history questions.
- For school/degree questions: check the RESUME CONTENT section for education info.
- For employer/job title: use Current employer / Current job title from profile, or extract from resume.
- For city/state: use the city and state from the Location line in the profile.
- Keep answers concise:
  - Yes/No fields: just "Yes" or "No"
  - Short text: under 15 words
  - Textarea: 3-5 sentences max, specific and genuine

CRITICAL RULES — you will be penalized for breaking these:
1. Reply with ONLY the answer. NEVER write explanations, reasoning, or meta-commentary.
2. NEVER start your reply with "I'm ready", "I notice", "Based on", "The candidate", "Not provided", "I don't have", "I'd be happy", or any similar phrasing. These go directly into a form field.
3. If you are unsure, give your best SHORT answer (a name, a place, a word). A wrong short answer is better than an explanation.
4. The output goes directly into a form field — it must be a real answer, not a description of the answer.
5. NEVER write more than one sentence for yes/no or short-answer fields.
"""
    for attempt in range(3):
        try:
            if attempt > 0:
                await asyncio.sleep(2 ** attempt)  # 2s, 4s backoff
            message = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            answer = message.content[0].text.strip()
            _cache_set(question, field_type, profile_text, answer)
            return answer
        except Exception as e:
            err = str(e)
            if "rate_limit" in err or "429" in err:
                print(f"    ⚠ Rate limit — retrying in {2 ** (attempt+1)}s...")
                await asyncio.sleep(2 ** (attempt + 1))
            else:
                print(f"    ✗ AI error: {e}")
                return ""
    return ""

async def batch_get_answers(questions: list[dict], profile_text: str) -> dict:
    """
    Single API call to answer all form questions at once.
    questions: [{"key": str, "label": str, "type": str, "options": list|None}]
    Returns: {key: answer_str}
    """
    import json as _json
    if not questions:
        return {}

    q_lines = []
    for i, q in enumerate(questions):
        opts = f" Options: {q['options']}" if q.get("options") else ""
        q_lines.append(f'{i+1}. [{q["type"]}] {q["label"]}{opts}')

    prompt = f"""You are filling out a job application for this candidate:

{profile_text}

Answer ALL of the following form questions. Reply with a single JSON object where keys are the question numbers (as strings) and values are the answers.

QUESTIONS:
{chr(10).join(q_lines)}

RULES:
- Yes/No fields: answer "Yes" or "No" only
- Short text fields: under 15 words
- Textarea fields: 3-5 sentences, professional and genuine
- Dropdown/radio/checkbox: reply with the exact best option text
- Sponsorship/visa questions: if work_auth is citizen or authorized → "No"
- "How did you hear about us" → "Online job search"
- Demographic (gender/race/disability/veteran): use exact profile values, pick "decline" if profile says decline
- School not in dropdown list → pick "Other"
- If unsure → give best short professional answer

CRITICAL — ACKNOWLEDGMENT / COMPLIANCE / POLICY questions → ALWAYS "Yes":
These are statements the company asks you to confirm understanding of.
Examples (recognize ANY of these patterns — they're ALWAYS Yes):
- Starts with "I acknowledge", "I agree", "I confirm", "I understand",
  "I certify", "I authorize", "I consent"
- Starts with the COMPANY NAME followed by "adheres to", "is committed to",
  "complies with", "requires", "expects", "has a policy"
  (e.g. "Robinhood adheres to applicable laws...", "Stripe is committed to
  equal employment opportunity..." → ALWAYS "Yes")
- Mentions "background check", "drug test", "equal opportunity",
  "anti-discrimination", "anti-harassment", "laws and regulations",
  "company policies", "code of conduct"
- Asks if you understand a work arrangement (hybrid/office/travel/schedule)
- Asks if you authorize the company to verify/contact references

The candidate already chose to apply, so they accept these terms.
NEVER answer "No" to an acknowledgment question unless the candidate's
profile explicitly says they refuse it.

Reply ONLY with valid JSON. No explanation. No markdown. Example:
{{"1": "Yes", "2": "5 years", "3": "I am authorized to work in the US"}}"""

    for attempt in range(3):
        try:
            if attempt > 0:
                await asyncio.sleep(2 ** attempt)
            message = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip()
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = _json.loads(raw)
            return {questions[int(k)-1]["key"]: str(v) for k, v in data.items() if k.isdigit() and int(k)-1 < len(questions)}
        except Exception as e:
            err = str(e)
            if "rate_limit" in err or "429" in err:
                print(f"    ⚠ Rate limit on batch — waiting {2**(attempt+1)}s...")
                await asyncio.sleep(2 ** (attempt + 1))
            else:
                print(f"    ⚠ Batch answer failed (attempt {attempt+1}): {e}")
    return {}


async def read_email_verification_code(wait_sec: int = 90, since_dt=None, used_uids: set = None, company: str = None, imap_user: str = None, imap_pass: str = None) -> tuple[str, bytes] | tuple[None, None]:
    """
    Poll Gmail via IMAP for a Greenhouse verification code.
    Returns (code, uid) so callers can add the uid to used_uids to avoid reuse.
    Skips any email whose UID is already in used_uids.
    Only accepts emails received at or after since_dt (2-minute buffer for clock skew).
    If company is provided, only accepts emails whose subject contains the company name.
    Uses per-user imap_user/imap_pass if provided, falls back to global SMTP_USER/SMTP_PASS.
    """
    import imaplib
    import email as _email
    import email.utils as _eutils
    import re as _re
    from datetime import datetime, timezone, timedelta
    from config import SMTP_USER, SMTP_PASS

    imap_user = imap_user or SMTP_USER
    imap_pass = imap_pass or SMTP_PASS

    if not imap_user or not imap_pass:
        print("    ✗ No IMAP credentials — set Gmail + App Password in Profile → Email Verification")
        return None, None

    print(f"    📧 Using IMAP account: {imap_user}")

    if since_dt is None:
        since_dt = datetime.now(timezone.utc) - timedelta(seconds=30)
    if used_uids is None:
        used_uids = set()

    print(f"    📧 Polling {imap_user} for Greenhouse verification email (up to {wait_sec}s)...")
    elapsed = 0
    for tick in range(wait_sec // 5):
        await asyncio.sleep(5)
        elapsed += 5
        print(f"    ⏳ [{elapsed}s] Checking inbox...")
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(imap_user, imap_pass)
            # INBOX first — verification emails should always be there because
            # they're transactional. Also some accounts have "Show in IMAP"
            # disabled for the All Mail label, which makes a folder select
            # silently succeed but searches return nothing.
            # If INBOX gives no hits later, the search falls through to
            # All Mail as a wider net (Promotions/Updates/Spam).
            select_result, _ = mail.select("INBOX", readonly=False)
            if select_result != "OK":
                print(f"    ⚠ Could not select INBOX, falling back to All Mail")
                mail.select('"[Gmail]/All Mail"')

            since_str = since_dt.strftime("%d-%b-%Y")
            seen_nums = set()
            # First tick only: log how many emails are in the mailbox total
            # (sanity check for "is IMAP even seeing my mail").
            if elapsed == 5:
                try:
                    _, all_msgs = mail.search(None, "ALL")
                    print(f"    🔎 IMAP sees {len(all_msgs[0].split()) if all_msgs[0] else 0} emails in current folder")
                    if all_msgs[0]:
                        recent = all_msgs[0].split()[-3:]
                        for n in reversed(recent):
                            _, hd = mail.fetch(n, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                            print(f"    🔎 Recent: {hd[0][1].decode(errors='ignore')[:200]}")
                except Exception as _e:
                    print(f"    ⚠ IMAP sanity check failed: {_e}")

            # Aggressive search list. Real-world Greenhouse verification email
            # subject we observed: "Security code for your application to Ripple".
            # So we now match:
            #   - "security code" (the exact phrase)
            #   - "security" (broad — catches variants)
            #   - "code" (most generic)
            #   - "your application" / "application to" (lots of variants)
            # Plus company name in subject if provided.
            company_search = []
            if company:
                # Plain "ripple" string matches "Security code for your application to Ripple"
                company_search.append(f'(SUBJECT "{company}" SINCE "{since_str}")')
            for search in company_search + [
                f'(FROM "greenhouse-mail.io" SINCE "{since_str}")',
                f'(FROM "greenhouse.io" SINCE "{since_str}")',
                f'(FROM "no-reply@greenhouse.io" SINCE "{since_str}")',
                f'(SUBJECT "security code" SINCE "{since_str}")',
                f'(SUBJECT "security" SINCE "{since_str}")',
                f'(SUBJECT "verify your application" SINCE "{since_str}")',
                f'(SUBJECT "verification" SINCE "{since_str}")',
                f'(SUBJECT "verify your email" SINCE "{since_str}")',
                f'(SUBJECT "your application" SINCE "{since_str}")',
                f'(SUBJECT "code" SINCE "{since_str}")',
                f'(SUBJECT "Greenhouse" SINCE "{since_str}")',
                # No-SINCE escape hatches — IMAP date drift insurance
                f'(SUBJECT "security code")',
                f'(SUBJECT "Application")',
                # Last-resort: any unread email in the polling window
                f'(UNSEEN SINCE "{since_str}")',
            ]:
                _, msgs = mail.search(None, search)
                hit_count = len(msgs[0].split()) if msgs[0] else 0
                if hit_count > 0:
                    print(f"    🔎 {search} → {hit_count} matches")
                if not msgs[0]:
                    continue
                for num in reversed(msgs[0].split()[-10:]):
                    if num in seen_nums:
                        continue
                    seen_nums.add(num)

                    # Skip already-used emails by UID
                    if num in used_uids:
                        print(f"    ⏭ Skipping already-used email UID {num.decode()}")
                        continue

                    _, data = mail.fetch(num, "(RFC822)")
                    msg = _email.message_from_bytes(data[0][1])

                    date_str = msg.get("Date", "")
                    from_hdr = msg.get("From", "")
                    subj_hdr = msg.get("Subject", "")
                    # Cross-contamination filter — but only HARD-skip if the
                    # subject explicitly mentions a DIFFERENT company. Many
                    # Greenhouse verification emails don't include any company
                    # name in the subject (just "Verify your application").
                    # We only skip if the subject looks application-specific
                    # AND mentions a wrong company.
                    if company:
                        subj_l = subj_hdr.lower()
                        company_l = company.lower()
                        # Only skip if the email is clearly for another company
                        # (e.g. subject contains "Gusto" when we want "Ripple")
                        looks_wrong = False
                        common_words = {"verification", "verify", "application", "code", "greenhouse"}
                        words = [w for w in subj_l.split() if len(w) > 3 and w not in common_words]
                        for w in words:
                            if w not in company_l and company_l not in w and len(w) > 4:
                                # Heuristic: a long company-like word that isn't our target
                                # Don't trust this strictly — fall through and let the body
                                # regex decide. Just log it.
                                pass
                        # We deliberately DO NOT skip based on subject anymore —
                        # the body regex + date filter is enough cross-contamination
                        # protection. Comment kept for context.

                    try:
                        email_dt = _eutils.parsedate_to_datetime(date_str)
                        if email_dt.tzinfo is None:
                            email_dt = email_dt.replace(tzinfo=timezone.utc)
                        # Only accept emails from within 2 minutes before since_dt (clock skew buffer)
                        cutoff = since_dt - timedelta(minutes=2)
                        if email_dt < cutoff:
                            print(f"    ⏭ Old email {email_dt.strftime('%H:%M:%S UTC')} (cutoff {cutoff.strftime('%H:%M:%S')})")
                            continue
                        print(f"    📨 Candidate: {email_dt.strftime('%H:%M:%S UTC')} | {subj_hdr[:50]}")
                    except Exception:
                        print(f"    📨 Candidate (no date): {from_hdr[:40]}")

                    # Extract body — prefer plain text, fall back to stripped HTML
                    plain_body = ""
                    html_body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            ct = part.get_content_type()
                            payload = (part.get_payload(decode=True) or b"").decode(errors="ignore")
                            if ct == "text/plain":
                                plain_body += payload
                            elif ct == "text/html":
                                html_body += payload
                    else:
                        ct = msg.get_content_type()
                        payload = (msg.get_payload(decode=True) or b"").decode(errors="ignore")
                        if ct == "text/html":
                            html_body = payload
                        else:
                            plain_body = payload

                    if not plain_body and html_body:
                        plain_body = _re.sub(r'<[^>]+>', ' ', html_body)
                    body = plain_body or html_body

                    # Greenhouse codes: 6-12 char alphanumeric on own line after "application:"
                    m = _re.search(
                        r'application[^\n]*\r?\n\s*\r?\n\s*([A-Za-z0-9]{6,12})\s*\r?\n',
                        body
                    )
                    if not m:
                        m = _re.search(r'(?:^|\n)\s*([A-Za-z0-9]{6,12})\s*(?:\r?\n|$)', body)
                    if m:
                        code = m.group(1)
                        mail.store(num, '+FLAGS', '\\Seen')
                        mail.logout()
                        print(f"\n    ✅ VERIFICATION CODE FOUND: {code}  (from email UID {num.decode()}, {elapsed}s elapsed)")
                        print(f"    → Filling code into form and re-submitting...\n")
                        return code, num
            mail.logout()
        except Exception as e:
            err = str(e)
            if "AUTHENTICATIONFAILED" in err or "Invalid credentials" in err:
                print(f"    ✗ IMAP auth failed — wrong App Password for {imap_user}")
                print(f"    ✗ Fix: Profile → Email Verification → generate a Gmail App Password (not your regular password)")
                return None, None  # fail immediately, don't waste 90s retrying
            print(f"    ⚠ IMAP error: {e}")
    print(f"    ✗ No verification code found after {wait_sec}s")
    return None, None


async def set_react_input(el, value: str):
    """
    Set a value on a React-controlled input and trigger React's synthetic events.
    Playwright's fill() bypasses React's internal state — this forces React to
    recognize the new value by using the native HTMLInputElement setter.
    """
    await el.evaluate("""(el, v) => {
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        setter.call(el, v);
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
    }""", value)


async def get_frame(page):
    """Get the correct frame where the form lives."""
    await asyncio.sleep(2)
    for f in page.frames:
        if "job-boards.greenhouse.io" in f.url or "boards.greenhouse.io" in f.url:
            print(f"    ✓ Found form frame: {f.url[:60]}")
            return f
    print("    ℹ No iframe found, using main page")
    return page

async def apply_greenhouse(job: dict, dry_run: bool = True, user_info: dict = None, profile_text: str = None):
    if not user_info or not profile_text:
        raise ValueError("user_info and profile_text are required")
    from datetime import datetime, timezone
    session_start = datetime.now(timezone.utc)
    info = user_info
    profile = profile_text

    print(f"\n  Applying to: {job['title']} @ {job['company']}")
    print(f"  URL: {job['url']}")

    async with async_playwright() as p:
        async with stealth_session(
            p, url=job["url"], user_id=info.get("user_id"),
        ) as (_browser, _context, page):
            try:
                os.makedirs("screenshots", exist_ok=True)
                job_id = job.get('id', 'unknown')

                print(f"    → Loading: {job['url']}")
                await page.goto(job["url"], timeout=60000, wait_until="domcontentloaded")
                await asyncio.sleep(2)
                await page.screenshot(path=f"screenshots/gh_{job_id}_1_loaded.png")
                await wait_for_captcha_if_present(page)

                # Find a VISIBLE apply button. The previous selector
                # `a:has-text('Apply'), button:has-text('Apply')` was too greedy:
                # on MongoDB's careers page it matched a hidden
                # `<button id="filter-apply-handler">Apply</button>` (a
                # search-filter button) before reaching the real apply button.
                # The new strategy:
                #   1. Prefer specific phrases ("Apply for this job", "Apply now")
                #   2. Filter to :visible elements only
                #   3. Skip known filter-button IDs
                #   4. If nothing works on the wrapper page, fall back to the
                #      direct Greenhouse-hosted URL via gh_jid
                apply_clicked = False
                for selector in [
                    "a:has-text('Apply for this job'):visible",
                    "button:has-text('Apply for this job'):visible",
                    "a:has-text('Apply now'):visible",
                    "button:has-text('Apply now'):visible",
                    "a:has-text('Apply Now'):visible",
                    "button:has-text('Apply Now'):visible",
                    # Generic, but visible-only AND excluding known filter ids
                    "a:has-text('Apply'):visible:not(#filter-apply-handler)",
                    "button:has-text('Apply'):visible:not(#filter-apply-handler):not([id*='filter']):not([id*='search'])",
                ]:
                    btn = page.locator(selector)
                    try:
                        count = await btn.count()
                    except Exception:
                        continue
                    if count > 0:
                        try:
                            await btn.first.click(timeout=8000)
                            await page.wait_for_load_state("domcontentloaded", timeout=10000)
                            await asyncio.sleep(2)
                            await wait_for_captcha_if_present(page)
                            await page.screenshot(path=f"screenshots/gh_{job_id}_2_after_apply_click.png")
                            print(f"    ✓ Clicked Apply on main page (selector: {selector[:50]})")
                            apply_clicked = True
                            break
                        except Exception as e:
                            print(f"    ⚠ Apply click failed for {selector[:50]}: {type(e).__name__}")
                            continue

                # Fallback: if we couldn't find a visible apply button on the
                # company's wrapper careers page, navigate DIRECTLY to the
                # Greenhouse-hosted form via gh_jid in the URL. This is what
                # rescues MongoDB, Notion, and other companies that wrap
                # Greenhouse but don't have a clean Apply button on the wrapper.
                if not apply_clicked:
                    import re as _re_jid
                    m = _re_jid.search(r"gh_jid=(\d+)", job.get("url", ""))
                    if m:
                        # Try the standard direct URL formats; one of them
                        # almost always works.
                        gh_jid = m.group(1)
                        company_slug = job.get("company", "").lower().replace(" ", "")
                        for direct in [
                            f"https://job-boards.greenhouse.io/{company_slug}/jobs/{gh_jid}",
                            f"https://boards.greenhouse.io/{company_slug}/jobs/{gh_jid}",
                            f"https://job-boards.greenhouse.io/embed/job_app?for={company_slug}&token={gh_jid}",
                        ]:
                            try:
                                print(f"    → Wrapper Apply button not found — trying direct URL: {direct[:60]}")
                                await page.goto(direct, timeout=30000, wait_until="domcontentloaded")
                                await asyncio.sleep(2)
                                # Check that we landed on a real Greenhouse form
                                form_exists = await page.locator("input#first_name, input[name='first_name']").count()
                                if form_exists > 0:
                                    print(f"    ✓ Direct URL worked")
                                    apply_clicked = True
                                    break
                            except Exception as e:
                                print(f"    ⚠ Direct URL failed: {type(e).__name__}")
                                continue

                if not apply_clicked:
                    print("    ℹ No Apply button, form may already be visible — trying anyway")

                frame = await get_frame(page)

                await fill_by_id(frame, "first_name", info.get("first_name", ""))
                await fill_by_id(frame, "last_name", info.get("last_name", ""))
                await fill_by_id(frame, "email", info.get("email", ""))
                await fill_by_id(frame, "phone", info.get("phone", ""))
                await fill_location(frame, info.get("location", ""))

                await fill_country(frame, profile)

                resume_input = frame.locator("input#resume[type='file']")
                if await resume_input.count() > 0:
                    resume_path = info.get("resume_path", "")
                    if resume_path and os.path.exists(resume_path):
                        await resume_input.set_input_files(resume_path)
                        print("    ✓ Resume uploaded")
                        await asyncio.sleep(2)
                    else:
                        print(f"    ✗ Resume file not found: {resume_path!r}")
                else:
                    print("    ℹ No resume input found on form")

                cover_letter = frame.locator("#cover_letter_text, textarea[name='cover_letter']")
                if await cover_letter.count() > 0:
                    cl_text = await get_answer("Write a brief cover letter for this position", "textarea", profile_text=profile)
                    if cl_text:
                        await cover_letter.first.fill(cl_text)
                        print("    ✓ Cover letter filled")

                await fill_custom_questions_with_ai(frame, profile)

                await page.screenshot(path=f"screenshots/gh_{job_id}_3_form_filled.png")
                print(f"    ✓ Form filled! (screenshot: gh_{job_id}_3_form_filled.png)")

                if dry_run:
                    print(f"    ✓ DRY RUN — screenshot saved to screenshots/gh_{job_id}_3_form_filled.png")
                else:
                    result = await handle_errors_and_retry(frame, page, profile_text=profile, user_info=info, session_start=session_start, company=job.get("company", ""))
                    return result

            except Exception as e:
                import traceback
                print(f"    ✗ Error: {e}")
                traceback.print_exc()
                try:
                    await page.screenshot(path=f"screenshots/gh_error_{job.get('id', 'unknown')}.png")
                    print(f"    → Screenshot saved: screenshots/gh_error_{job.get('id', 'unknown')}.png")
                except Exception:
                    pass
                return "failed"
            finally:
                await asyncio.sleep(3)  # pause so you can see the final state
                # cleanup handled by stealth_session

    return "dry_run"

async def fill_country(frame, profile_text: str = None):
    """Fill the country field — try direct match first, AI only as fallback."""
    import re
    country_value = "United States"
    if profile_text:
        m = re.search(r"Country:\s*(.+)", profile_text)
        if m:
            country_value = m.group(1).strip() or "United States"

    el = frame.locator("#country")
    if await el.count() == 0 or not await el.is_visible():
        return

    await el.click(timeout=5000)
    await asyncio.sleep(0.8)

    options = frame.locator("div[class*='option']")
    count = await options.count()
    option_texts = []
    for i in range(count):
        text = (await options.nth(i).inner_text()).strip()
        if text:
            option_texts.append(text)

    if not option_texts:
        await el.press("Escape")
        return

    # Try exact or partial match first — no AI needed for country
    for i in range(count):
        opt = options.nth(i)
        text = (await opt.inner_text()).strip()
        if country_value.lower() in text.lower() or text.lower() in country_value.lower():
            await opt.click()
            print(f"    ✓ Country: {text}")
            return

    # Final fallback: ask AI
    await el.press("Escape")
    await fill_react_select(frame, "country", country_value, "Country", profile_text)


async def fill_location(frame, location: str):
    """Fill the candidate-location field — supports both plain text and Google autocomplete."""
    if not location:
        return
    el = frame.locator("#candidate-location")
    if await el.count() == 0:
        return
    try:
        city = location.split(",")[0].strip()
        await el.click(timeout=5000)
        await el.fill("")
        await el.type(city, delay=80)
        await asyncio.sleep(1.5)

        # Look specifically for Google Places or Greenhouse autocomplete suggestions
        suggestion_selectors = [
            ".pac-item",                   # Google Places
            "[class*='react-select__option']",
            "[class*='suggestion-item']",
            "ul.dropdown-menu li",
        ]
        clicked = False
        for sel in suggestion_selectors:
            try:
                sugg = frame.locator(sel)
                if await sugg.count() > 0:
                    await sugg.first.click(timeout=3000)
                    print(f"    ✓ Location autocomplete: {city}")
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            # Plain text input — just press Tab to commit
            await el.press("Tab")
            print(f"    ✓ Filled #candidate-location: {city}")
    except Exception as e:
        # Non-fatal — form may still accept the typed text
        print(f"    ⚠ Location field: {e}")


async def fill_by_id(frame, field_id: str, value: str):
    try:
        el = frame.locator(f"#{field_id}")
        if await el.count() == 0:
            return False
        # Click to focus, clear, then type character-by-character so React's
        # synthetic events (onChange) fire for every keystroke.
        await el.first.click()
        await el.first.fill("")
        await el.first.press_sequentially(value, delay=40)
        actual = await el.first.input_value()
        print(f"    ✓ Filled #{field_id}: {repr(actual)}")
        return True
    except Exception as e:
        print(f"    ✗ Could not fill #{field_id}: {e}")
    return False


async def _find_submit_button(frame, page):
    """Try many selector patterns to find the submit button across different Greenhouse-hosted pages."""
    candidates = [
        ("frame", "button:has-text('Submit application')"),
        ("frame", "button:has-text('Submit Application')"),
        ("frame", "button:has-text('Submit')"),
        ("frame", "button[type='submit']"),
        ("frame", "input[type='submit']"),
        ("frame", "[data-qa='submit-application-button']"),
        ("frame", "[class*='submit']:visible"),
        ("page",  "button:has-text('Submit application')"),
        ("page",  "button:has-text('Submit Application')"),
        ("page",  "button:has-text('Submit')"),
        ("page",  "button[type='submit']"),
        ("page",  "input[type='submit']"),
        ("page",  "[data-qa='submit-application-button']"),
    ]
    for scope, sel in candidates:
        try:
            loc = (frame if scope == "frame" else page).locator(sel)
            if await loc.count() > 0 and await loc.first.is_visible():
                txt = await loc.first.inner_text() if "button" in sel else sel
                print(f"    ✓ Found submit button [{scope}]: {sel!r} — '{txt.strip()[:40]}'")
                return loc.first
        except Exception:
            continue
    return None


async def _diagnose_screenshot(screenshot_path: str, error_context: str) -> str:
    """Send a failure screenshot to Claude Vision and get a diagnosis."""
    try:
        import base64
        with open(screenshot_path, "rb") as f:
            img_b64 = base64.standard_b64encode(f.read()).decode()
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": img_b64},
                    },
                    {
                        "type": "text",
                        "text": f"This is a screenshot of a job application page. Error: '{error_context}'. "
                                "Briefly describe: 1) What you see on the page, 2) Where the submit/apply button is "
                                "and what text it has, 3) Any errors or blockers visible. Be concise (2-3 sentences)."
                    }
                ],
            }]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"(vision unavailable: {e})"


async def handle_errors_and_retry(frame, page, max_retries: int = 5, profile_text: str = None, user_info: dict = None, session_start=None, company: str = None) -> str:
    """
    After submit, check for validation errors, fix them, and retry.
    """
    from datetime import datetime, timezone, timedelta
    info = user_info or {}
    if session_start is None:
        session_start = datetime.now(timezone.utc)
    submit_time: datetime | None = None  # set just before each Submit click
    used_uids: set = set()  # track email UIDs already used so we never reuse a code

    for attempt in range(max_retries):
        # Re-acquire frame on every attempt — React re-renders can make old reference stale
        try:
            frame = await get_frame(page)
        except Exception:
            pass

        # Detect email verification security code fields — read from Gmail automatically
        security = frame.locator("input[id^='security-input']")
        if await security.count() > 0:
            print("    → Email verification required — fetching code from Gmail...")
            code, uid = await read_email_verification_code(wait_sec=90, since_dt=session_start, used_uids=used_uids, company=company, imap_user=user_info.get("imap_user"), imap_pass=user_info.get("imap_pass"))
            if code:
                used_uids.add(uid)
                # Next attempt must find an email strictly newer than this one
                session_start = datetime.now(timezone.utc)
                print(f"    ✓ Verification code: {code}")
                # Use native React setter so React's controlled-input state updates
                await set_react_input(security.first, code)
                await asyncio.sleep(0.5)
                actual_val = await security.first.input_value()
                print(f"    → Code in field: {repr(actual_val)}")
                await page.screenshot(path=f"screenshots/gh_code_filled_{attempt}.png")
                # Click Submit application — it's the correct button here;
                # the issue was only that React state wasn't updating before.
                submit_btn = frame.locator("button:has-text('Submit application'), button:has-text('Submit')")
                if await submit_btn.count() > 0:
                    btn_text = await submit_btn.first.inner_text()
                    print(f"    → Clicking '{btn_text}' to submit with code...")
                    await trusted_click(submit_btn.first)
                else:
                    print("    → No submit button — pressing Enter on code field")
                    await security.first.press("Enter")
                await asyncio.sleep(5)
                await page.screenshot(path=f"screenshots/gh_after_verify_{attempt}.png")
                print("    → Checking if application went through...")
                continue
            else:
                print("    ✗ Could not retrieve verification code — failing")
                try:
                    await page.screenshot(path=f"screenshots/security_code_{attempt}.png")
                except Exception:
                    pass
                return "failed"

        # Check if we're already on the confirmation page before attempting to re-submit
        url_lower = page.url.lower()
        frame_url_lower = (frame.url or "").lower()
        if any(w in url_lower for w in ("confirmation", "thank", "success", "submitted")) or \
           any(w in frame_url_lower for w in ("confirmation", "thank", "success", "submitted")):
            print("    ✓ Already on confirmation page — application submitted successfully!")
            return "applied"

        # Re-fill unstable fields before every submit attempt.
        # CRITICAL ORDER: fill country + location FIRST (they reset last_name via React),
        # then fill last_name LAST so it's present when Submit is clicked.
        print(f"    → Re-filling unstable fields (attempt {attempt + 1})...")
        await fill_country(frame, profile_text)
        await fill_location(frame, info.get("location", ""))
        await fill_by_id(frame, "last_name", info.get("last_name", ""))
        await asyncio.sleep(1)  # wait for any deferred React re-renders to settle

        submit = await _find_submit_button(frame, page)
        if submit is None:
            # One last check: maybe page navigated to confirmation while we were re-filling
            url_lower = page.url.lower()
            frame_url_lower = (frame.url or "").lower()
            if any(w in url_lower for w in ("confirmation", "thank", "success", "submitted")) or \
               any(w in frame_url_lower for w in ("confirmation", "thank", "success", "submitted")):
                print("    ✓ Confirmation URL detected — application submitted successfully!")
                return "applied"
            screenshot_path = f"screenshots/no_submit_{attempt}.png"
            await page.screenshot(path=screenshot_path)
            # Ask Claude Vision to diagnose the screenshot
            diagnosis = await _diagnose_screenshot(screenshot_path, "Submit button not found")
            print(f"    🔍 AI diagnosis: {diagnosis}")
            print("    ✗ Submit button not found — see diagnosis above")
            return "failed"

        print(f"    → Clicking Submit (attempt {attempt + 1})...")
        submit_time = datetime.now(timezone.utc)  # capture timestamp right before submit
        await submit.first.click()
        await asyncio.sleep(3)
        try:
            await page.screenshot(path=f"screenshots/gh_submit_attempt_{attempt + 1}.png")
            print(f"    → Post-submit screenshot: gh_submit_attempt_{attempt + 1}.png")
        except Exception:
            pass

        # Check for security code after submit (Greenhouse sends it post-click)
        security = frame.locator("input[id^='security-input']")
        if await security.count() > 0:
            print("    → Email verification code appeared after submit — reading from Gmail...")
            code, uid = await read_email_verification_code(wait_sec=90, since_dt=session_start, used_uids=used_uids, company=company, imap_user=user_info.get("imap_user"), imap_pass=user_info.get("imap_pass"))
            if code:
                used_uids.add(uid)
                # Next attempt must find an email strictly newer than this one
                session_start = datetime.now(timezone.utc)
                print(f"    ✅ VERIFICATION CODE: {code} — filling in now...")
                # Use native React setter so React's controlled-input state updates
                await set_react_input(security.first, code)
                await asyncio.sleep(0.5)
                actual_val = await security.first.input_value()
                print(f"    → Code in field: {repr(actual_val)}")
                await page.screenshot(path=f"screenshots/gh_code_filled_{attempt}.png")
                # Click Submit application — it's the correct button here;
                # the issue was only that React state wasn't updating before.
                submit_btn = frame.locator("button:has-text('Submit application'), button:has-text('Submit')")
                if await submit_btn.count() > 0:
                    btn_text = await submit_btn.first.inner_text()
                    print(f"    → Clicking '{btn_text}' to submit with code...")
                    await trusted_click(submit_btn.first)
                else:
                    print("    → No submit button — pressing Enter on code field")
                    await security.first.press("Enter")
                await asyncio.sleep(5)
                await page.screenshot(path=f"screenshots/gh_after_verify_{attempt}.png")
                print("    → Checking if application went through...")
                continue
            else:
                print("    ✗ Could not retrieve verification code — failing")
                try:
                    await page.screenshot(path=f"screenshots/security_code_post_{attempt}.png")
                except Exception:
                    pass
                return "failed"

        # Broad success detection — Greenhouse/company pages vary widely
        success = frame.locator(
            "h1:has-text('Thank'), h2:has-text('Thank'), "
            "h1:has-text('Application received'), h2:has-text('Application received'), "
            "h1:has-text('Application submitted'), h2:has-text('Application submitted'), "
            "h1:has-text('Successfully'), h2:has-text('Successfully'), "
            "[class*='confirmation'], [class*='success-message'], [class*='thank-you'], "
            "[data-qa='confirmation-message']"
        )
        if await success.count() > 0:
            print("    ✓ Application submitted successfully!")
            return "applied"

        url_lower = page.url.lower()
        if any(w in url_lower for w in ("confirmation", "thank", "success", "submitted")):
            print("    ✓ Submitted — confirmation URL detected")
            return "applied"

        # Check full page text for any thank-you signal
        try:
            content = await frame.inner_text("body")
            if any(phrase in content.lower() for phrase in (
                "thank you for applying", "application has been submitted",
                "application received", "we've received your application",
                "successfully submitted", "application was submitted",
            )):
                print("    ✓ Application submitted (detected in page text)")
                return "applied"
        except Exception:
            pass

        print(f"\n    ⚠ Attempt {attempt + 1} — checking for errors...")
        errors = await find_errors(frame)

        if not errors:
            # No positive confirmation (URL change, success heading, or
            # confirmation text) AND no visible errors. This is the classic
            # silent-block case: the page froze mid-submit because of a
            # CAPTCHA or anti-bot wall that didn't surface a user-visible
            # error. Mark "unknown" instead of falsely claiming success —
            # this protects the credit balance and surfaces the case to the
            # user for manual verification.
            print("    ⚠ No success confirmation and no errors — outcome unknown")
            try:
                await page.screenshot(path=f"screenshots/gh_unknown_{attempt}.png")
            except Exception:
                pass
            return "unknown"

        print(f"    Found {len(errors)} error(s):")
        for dbg_id, dbg_msg in errors:
            print(f"      [err] id={dbg_id!r} msg={dbg_msg[:60]!r}")
        fixed = 0
        seen_fields = set()
        for field_id, error_text in errors:
            # Skip label/error suffix IDs (display elements, not inputs)
            if field_id.endswith("-label") or field_id.endswith("-error"):
                continue
            # Skip individual checkbox option IDs like question_xxx[]_yyy
            # (these are individual options within a group — handled by group logic)
            import re as _re2
            if _re2.search(r'\[\]_\d+$', field_id):
                continue
            # Skip duplicate field IDs (find_errors may return same field twice)
            if field_id in seen_fields:
                continue
            seen_fields.add(field_id)
            # Skip last_name here — we re-fill it below after all dropdowns
            if field_id == "last_name":
                continue
            print(f"      ✗ {field_id}: {error_text}")
            result = await fix_error(frame, field_id, error_text, profile_text=profile_text)
            if result:
                fixed += 1
                print(f"      ✓ Fixed {field_id}")

        # Always re-fill last_name after dropdown fixes (React resets it)
        if "last_name" in {fid for fid, _ in errors}:
            await fill_by_id(frame, "last_name", info.get("last_name", ""))
            await asyncio.sleep(1)  # let React settle before next submit
            fixed += 1
            print(f"      ✓ Re-filled last_name")

        if fixed == 0:
            print("    ✗ Could not fix any errors — taking screenshot")
            await page.screenshot(path=f"screenshots/error_{attempt}.png")
            return "failed"

        print(f"    Fixed {fixed} fields, retrying submit...")
        await asyncio.sleep(2)  # let React settle before next attempt

    return "failed"


async def find_errors(frame) -> list[tuple[str, str]]:
    """Find all validation error messages on the page."""
    errors = []

    error_els = await frame.locator(
        "[aria-invalid='true'], .field_error, [class*='error']:not([class*='error-message'])"
    ).all()

    for el in error_els:
        field_id = await el.get_attribute("id") or ""

        error_msg = ""

        error_id = await el.get_attribute("aria-errormessage")
        if error_id:
            _sel = f"[id='{error_id}']" if error_id[:1].isdigit() else f"#{error_id}"
            try:
                msg_el = frame.locator(_sel)
                if await msg_el.count() > 0:
                    error_msg = await msg_el.inner_text()
            except Exception:
                pass

        if not error_msg and field_id:
            try:
                _fsel = f"[id='{field_id}']" if field_id[:1].isdigit() else f"#{field_id}"
                parent = frame.locator(_fsel).locator("xpath=..")
                if await parent.count() > 0:
                    sibling = parent.locator("[class*='error'], .field_error")
                    if await sibling.count() > 0:
                        error_msg = await sibling.first.inner_text()
            except Exception:
                pass

        if field_id or error_msg:
            errors.append((field_id or "unknown", error_msg.strip()))

    # Only check plain text inputs (not React Select comboboxes — they're always
    # technically "empty" because the selected value lives in a separate div).
    required = await frame.locator(
        "input[required]:not([role='combobox']):not([type='hidden']), "
        "input[aria-required='true']:not([role='combobox']):not([type='hidden']), "
        "input[aria-invalid='true']:not([role='combobox']):not([type='hidden'])"
    ).all()
    for el in required:
        field_id = await el.get_attribute("id") or ""
        # Skip React Select search inputs (parent has react-select class)
        try:
            parent_class = await el.evaluate("el => el.closest('[class*=\"react-select\"]') ? 'rs' : ''")
            if parent_class == "rs":
                continue
        except Exception:
            pass
        value = await el.input_value()
        if not value and field_id:
            errors.append((field_id, "Field is required but empty"))

    return errors


async def fix_error(frame, field_id: str, error_text: str, profile_text: str = None) -> bool:
    """Try to fix a specific field error."""
    if not field_id or field_id == "unknown":
        return False

    # Always use attribute selector — [] and digits in IDs break CSS # selectors
    el = frame.locator(f"[id='{field_id}']")
    if await el.count() == 0:
        return False

    label_el = frame.locator(f"label[for='{field_id}']")
    label_text = field_id
    if await label_el.count() > 0:
        label_text = await label_el.first.inner_text()

    tag = await el.evaluate("el => el.tagName.toLowerCase()")
    role = await el.get_attribute("role") or ""
    type_ = await el.get_attribute("type") or ""

    print(f"      Fixing: {label_text[:50]} (tag={tag}, type={type_})")

    if role == "combobox":
        answer = await get_answer(label_text, "dropdown", profile_text=profile_text)
        return await fill_react_select(frame, field_id, answer, label_text, profile_text)

    elif type_ == "checkbox":
        await el.check()
        return True

    elif tag == "input":
        answer = await get_answer(label_text, "text", profile_text=profile_text)
        if answer:
            await el.fill(answer)
            return True

    elif tag == "textarea":
        answer = await get_answer(label_text, "textarea", profile_text=profile_text)
        if answer:
            await el.fill(answer)
            return True

    return False


async def fill_react_select(frame, field_id: str, value: str = None, label: str = "", profile_text: str = None):
    try:
        # Always use attribute selector — [] and special chars in IDs break CSS # selectors
        el = frame.locator(f"[id='{field_id}']")

        if await el.count() == 0 or not await el.is_visible():
            return False

        # Open the dropdown
        await el.click(timeout=5000)

        # Retry up to 3 times waiting for options to appear
        # For autocomplete fields (like city), also try typing the value to trigger suggestions
        option_texts = []
        for attempt in range(3):
            await asyncio.sleep(0.8)
            options = frame.locator("div[class*='option']")
            count = await options.count()
            if count > 0:
                for i in range(count):
                    text = (await options.nth(i).inner_text()).strip()
                    if text:
                        option_texts.append(text)
                break
            # If no options after clicking, try typing to trigger autocomplete
            if attempt == 1 and value:
                # Type first few chars of the value to get suggestions
                search_term = value.split(",")[0].strip()[:10]
                await el.type(search_term, delay=80)
                await asyncio.sleep(1.2)
            elif attempt < 2:
                await el.click(timeout=5000)

        if not option_texts:
            # Last resort: if it's a plain text input acting as combobox, just fill it
            input_type = await el.get_attribute("type") or ""
            if value and input_type == "text":
                await el.fill(value)
                print(f"    ✓ Typed location: {value}")
                return True
            await el.press("Escape")
            print(f"    - No options found for {field_id}")
            return False

        # If a batch answer was provided, try to match it directly before calling AI.
        #
        # IMPORTANT: the old logic just did `val_lower in text.lower()` which is
        # a naive substring match — that meant "male" matched "FEmale" because
        # "male" is a substring of "female". Same bug for "man" → "woman".
        # Three-tier match instead:
        #   1. Exact case-insensitive match across all options       (best)
        #   2. Word-boundary match (val appears as a whole word)     (good)
        #   3. Fall through to letting Claude pick                   (fallback)
        ai_choice = ""
        if value:
            val_lower = value.strip().lower()

            # Tier 1: exact match
            for text in option_texts:
                if text.lower() == val_lower:
                    ai_choice = text
                    print(f"    ✓ Batch match (exact): {text!r}")
                    break

            # Tier 2: word-boundary match — "male" matches "Male" inside
            # "Yes, male" but NOT inside "female" (which has 'fe' attached).
            if not ai_choice:
                import re as _re
                pattern = _re.compile(rf"\b{_re.escape(val_lower)}\b", _re.IGNORECASE)
                for text in option_texts:
                    if pattern.search(text):
                        ai_choice = text
                        print(f"    ✓ Batch match (word): {text!r}")
                        break

            # Tier 3 (super-permissive): only if val itself is long enough
            # that a substring match is unlikely to overlap. >= 6 chars
            # avoids the "male"/"female" confusion while still catching
            # "United States" matching "United States of America" etc.
            if not ai_choice and len(val_lower) >= 6:
                for text in option_texts:
                    if val_lower in text.lower() or text.lower() in val_lower:
                        ai_choice = text
                        print(f"    ✓ Batch match (substring): {text!r}")
                        break

        if not ai_choice:
            print(f"    ? AI choosing from: {option_texts}")
            # Ask Claude to pick the best option
            prompt = f"""
You are filling out a job application for this candidate:
{profile_text or ""}

Question: "{label}"
Available options: {option_texts}

Pick the BEST option from the list above that fits this candidate.

CRITICAL RULES:
1. Reply with ONLY the exact option text from the list. NOTHING ELSE.
2. Do NOT write any explanation, reasoning, or commentary.
3. Do NOT write "Based on the profile..." or any similar phrasing.
4. For gender: if the candidate is male, pick "Male" or "Man" — do NOT pick "Cisgender Male/Man" unless that is the ONLY male option.
5. For race/ethnicity/disability: if profile says decline, pick the "decline" / "prefer not to say" option.
6. For country/residence/location questions: pick the option that matches the candidate's country (from the "Country:" line in their profile — usually "United States").
7. For sponsorship/visa/work permit questions: if work_auth is 'citizen' or 'authorized', pick "No".
8. If unsure, pick the most neutral or positive option from the list.
9. For school/university/institution dropdowns: find the closest matching school name in the list. If the exact school is not listed, pick "Other" or the closest partial match. NEVER explain — just pick one option.
10. YOU MUST PICK ONE OPTION FROM THE LIST. If nothing matches, pick "Other" or the first option. NEVER return an explanation.
"""
            try:
                message = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=50,
                    messages=[{"role": "user", "content": prompt}]
                )
                ai_choice = message.content[0].text.strip()
                print(f"    → AI chose: {ai_choice}")
            except Exception as e:
                print(f"    ✗ AI error: {e}")
                # Fall back to first non-empty option
                ai_choice = option_texts[0] if option_texts else ""

        if not ai_choice:
            await el.press("Escape")
            return False

        # If AI returned a long explanation instead of a valid option, use fallback
        if len(ai_choice) > 120 or ai_choice.lower().startswith("i cannot") or "not in the" in ai_choice.lower():
            # Try to find "Other" first, else use first option
            fallback = next((t for t in option_texts if "other" in t.lower()), option_texts[0] if option_texts else None)
            print(f"    ⚠ AI gave explanation instead of option — falling back to: {fallback!r}")
            ai_choice = fallback or ""

        if not ai_choice:
            await el.press("Escape")
            return False

        # Find and click the matching option — exact match first
        for i in range(count):
            opt = options.nth(i)
            text = (await opt.inner_text()).strip()
            if text.lower() == ai_choice.lower():
                await opt.click()
                print(f"    ✓ Selected: {text}")
                await asyncio.sleep(0.3)
                return True

        # Substring match (ai_choice contained in option)
        for i in range(count):
            opt = options.nth(i)
            text = (await opt.inner_text()).strip()
            if ai_choice.lower() in text.lower():
                await opt.click()
                print(f"    ✓ Selected: {text}")
                await asyncio.sleep(0.3)
                return True

        # Fuzzy match — only if no word is a substring of a different option
        for i in range(count):
            opt = options.nth(i)
            text = (await opt.inner_text()).strip()
            words = [w for w in ai_choice.lower().split() if len(w) > 3]
            if words and all(w in text.lower() for w in words):
                await opt.click()
                print(f"    ✓ Selected (fuzzy): {text}")
                return True

        await el.press("Escape")
        print(f"    ✗ Could not match AI choice to options")
        return False

    except Exception as e:
        print(f"    ✗ Combobox failed {field_id}: {e}")
        return False


# Common country names to skip — these are dropdown option labels, not real questions
COUNTRY_NAMES = {
    "australia", "belgium", "brazil", "canada", "france", "germany",
    "india", "indonesia", "ireland", "israel", "italy", "japan",
    "malaysia", "mexico", "new zealand", "poland", "portugal", "romania",
    "singapore", "south korea", "spain", "sweden", "switzerland",
    "thailand", "the netherlands", "uae", "uk", "us", "united states",
    "united kingdom", "china", "hong kong"
}


async def fill_custom_questions_with_ai(frame, profile_text: str = None):
    """
    Collect ALL form questions, answer them in ONE batch API call, then fill all fields.
    ~85% cheaper than calling get_answer() per field.
    """
    import re as _re
    profile = profile_text or ""

    # ── Phase 1: Collect all questions ──────────────────────────────────────

    # Items to fill after batch: list of dicts with enough info to fill the field
    fill_items = []   # {"type": ..., "el": ..., "key": str, "label": str, ...}
    questions  = []   # [{key, label, type, options}] — fed to batch_get_answers

    def add_q(key, label, qtype, options=None):
        questions.append({"key": key, "label": label, "type": qtype, "options": options})

    # 1. React Select comboboxes
    comboboxes = await frame.locator("input[role='combobox']").all()
    for el in comboboxes:
        field_id = await el.get_attribute("id")
        if not field_id or field_id in ("country", "candidate-location"):
            continue
        if not await el.is_visible():
            continue

        label_el = frame.locator(f"label[for='{field_id}'], [id='{field_id}-label']")
        label_text = (await label_el.first.inner_text()) if await label_el.count() > 0 else ""
        if not label_text or label_text.strip().lower() in COUNTRY_NAMES:
            continue

        label_lower = label_text.strip().lower()
        if any(kw in label_lower for kw in ["country", "nation", "reside", "citizenship", "where do you currently live"]):
            m = _re.search(r"Country:\s*(.+)", profile)
            country_direct = (m.group(1).strip() if m else None) or "United States"
            print(f"    ? Country-of-residence: using '{country_direct}' from profile")
            await fill_react_select(frame, field_id, country_direct, label_text, profile)
            continue

        key = f"cb_{field_id}"
        add_q(key, label_text, "dropdown")
        fill_items.append({"type": "combobox", "el": el, "field_id": field_id, "label": label_text, "key": key})

    # 2. Native SELECT dropdowns (question_*)
    selects = await frame.locator("select[id^='question_']").all()
    for el in selects:
        field_id = await el.get_attribute("id")
        if not field_id:
            continue
        label_el = frame.locator(f"label[for='{field_id}']")
        label_text = (await label_el.first.inner_text()) if await label_el.count() > 0 else ""
        if not label_text:
            continue
        key = f"sel_{field_id}"
        add_q(key, label_text, "dropdown")
        fill_items.append({"type": "select", "el": el, "key": key, "label": label_text})

    # 2b. EEOC/demographic selects (job_application_*)
    eeoc_selects = await frame.locator("select[id^='job_application_']").all()
    for el in eeoc_selects:
        field_id = await el.get_attribute("id")
        if not field_id:
            continue
        label_el = frame.locator(f"label[for='{field_id}']")
        label_text = (await label_el.first.inner_text()) if await label_el.count() > 0 else \
            field_id.replace("job_application_", "").replace("_", " ").title()
        opts = [await o.inner_text() for o in await el.locator("option").all() if await o.get_attribute("value")]
        key = f"eeoc_{field_id}"
        add_q(key, label_text, "dropdown", opts)
        fill_items.append({"type": "eeoc_select", "el": el, "key": key, "label": label_text, "options_els": await el.locator("option").all()})

    # 3. Checkbox groups
    checkboxes = await frame.locator("input[type='checkbox'][id^='question_']").all()
    checkbox_groups = {}
    for cb in checkboxes:
        name = await cb.get_attribute("name")
        if name:
            checkbox_groups.setdefault(name, []).append(cb)

    for group_name, boxes in checkbox_groups.items():
        opts = []
        for cb in boxes:
            cb_id = await cb.get_attribute("id")
            lbl = frame.locator(f"label[for='{cb_id}']")
            if await lbl.count() > 0:
                opts.append((cb, await lbl.first.inner_text()))
        key = f"chk_{group_name}"
        add_q(key, group_name, "checkbox", [t for _, t in opts])
        fill_items.append({"type": "checkbox_group", "key": key, "opts": opts})

    # 4. Radio groups
    radios = await frame.locator("input[type='radio'][id^='question_']").all()
    radio_groups = {}
    for r in radios:
        name = await r.get_attribute("name")
        if name:
            radio_groups.setdefault(name, []).append(r)

    for group_name, opts in radio_groups.items():
        opt_texts = []
        for opt in opts:
            opt_id = await opt.get_attribute("id")
            lbl = frame.locator(f"label[for='{opt_id}']")
            if await lbl.count() > 0:
                opt_texts.append(await lbl.first.inner_text())
        key = f"radio_{group_name}"
        add_q(key, group_name, "radio", opt_texts)
        fill_items.append({"type": "radio_group", "key": key, "opts": opts, "opt_texts": opt_texts})

    # 5. Text inputs and textareas
    inputs   = await frame.locator("input[id^='question_']:not([type='radio']):not([type='checkbox']):not([type='hidden']):not([type='file']):not([role='combobox'])").all()
    textareas = await frame.locator("textarea[id^='question_']").all()
    for el in inputs + textareas:
        field_id = await el.get_attribute("id")
        if not field_id or field_id == "country":
            continue
        label_el = frame.locator(f"label[for='{field_id}']")
        label_text = (await label_el.first.inner_text()) if await label_el.count() > 0 else ""
        if not label_text or label_text.strip().lower() in COUNTRY_NAMES:
            continue
        tag = await el.evaluate("el => el.tagName.toLowerCase()")
        ftype = "textarea" if tag == "textarea" else "text"
        key = f"txt_{field_id}"
        add_q(key, label_text, ftype)
        fill_items.append({"type": "text", "el": el, "key": key, "label": label_text})

    if not questions:
        return

    # ── Phase 2: ONE batch API call ─────────────────────────────────────────
    print(f"    ⚡ Batch answering {len(questions)} questions in 1 API call...")
    answers = await batch_get_answers(questions, profile)
    print(f"    ✓ Got {len(answers)} answers")

    # ── Phase 3: Fill all fields using answers ───────────────────────────────
    for item in fill_items:
        key = item["key"]
        answer = answers.get(key, "")

        if item["type"] == "combobox":
            print(f"    ? Combobox: {item['label'][:60]} → {answer[:40]}")
            await fill_react_select(frame, item["field_id"], answer, item["label"], profile)

        elif item["type"] == "select":
            print(f"    ? Select: {item['label'][:60]} → {answer[:40]}")
            if answer:
                try:
                    await item["el"].select_option(label=answer)
                    print(f"    ✓ Selected: {answer}")
                except Exception:
                    try:
                        for opt in await item["el"].locator("option").all():
                            text = await opt.inner_text()
                            if answer.lower() in text.lower():
                                await item["el"].select_option(value=await opt.get_attribute("value"))
                                print(f"    ✓ Selected (fuzzy): {text}")
                                break
                    except Exception as e:
                        print(f"    ✗ Select failed: {e}")

        elif item["type"] == "eeoc_select":
            print(f"    ? EEOC: {item['label'][:60]} → {answer[:40]}")
            if answer:
                try:
                    await item["el"].select_option(label=answer)
                    print(f"    ✓ EEOC Selected: {answer}")
                except Exception:
                    for opt in item["options_els"]:
                        text = await opt.inner_text()
                        if answer.lower() in text.lower():
                            await item["el"].select_option(value=await opt.get_attribute("value"))
                            print(f"    ✓ EEOC Selected (fuzzy): {text}")
                            break

        elif item["type"] == "checkbox_group":
            print(f"    ? Checkbox: {key} → {answer[:40]}")
            # Batch may return "US", "Remote", or list-like "['US', 'Remote']"
            import re as _re_cb
            raw_ans = answer.strip()
            # Extract individual values from list-like strings: ['US', 'Remote'] → ['US', 'Remote']
            list_matches = _re_cb.findall(r"'([^']+)'|\"([^\"]+)\"", raw_ans)
            if list_matches:
                check_vals = [a or b for a, b in list_matches]
            else:
                check_vals = [v.strip() for v in raw_ans.split(",") if v.strip()]
            for cb, text in item["opts"]:
                t = text.strip().lower()
                for cv in check_vals:
                    a = cv.strip().lower()
                    if (a == t) or (len(a) > 2 and a in t) or (len(t) > 2 and t in a):
                        await cb.check()
                        print(f"    ✓ Checked: {text}")
                        break

        elif item["type"] == "radio_group":
            print(f"    ? Radio: {key} → {answer[:40]}")
            for i, opt in enumerate(item["opts"]):
                if i < len(item["opt_texts"]) and answer.lower() in item["opt_texts"][i].lower():
                    await opt.click()
                    print(f"    ✓ Radio: {item['opt_texts'][i]}")
                    break

        elif item["type"] == "text":
            print(f"    ? Text: {item['label'][:60]} → {answer[:50]}")
            if answer:
                await item["el"].fill(answer)
                print(f"    ✓ Filled")