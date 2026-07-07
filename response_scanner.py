"""
Response Inbox — scans the user's Gmail (same IMAP App Password used for
verification codes) for replies to job applications: interview invites,
assessments, recruiter replies, rejections.

Why: at 10 applications/day the user quickly has hundreds in flight; ONE
missed interview email wastes weeks of applying. This closes the loop.

Cost design: pure rule-based classification (sender domains + keyword
lists) — zero Claude tokens. Runs in the always-on auto_apply_loop every
cycle and on demand via POST /responses/scan.
"""
import asyncio
import email
import email.utils
import imaplib
import json
import re
from datetime import datetime, timedelta, timezone

from db import get_pool
from secrets_crypto import decrypt

# Senders that are almost certainly about a job application.
ATS_SENDER_DOMAINS = [
    "greenhouse.io", "greenhouse-mail.io", "lever.co", "hire.lever.co",
    "ashbyhq.com", "smartrecruiters.com", "myworkday.com", "workday.com",
    "hackerrank.com", "codesignal.com", "codility.com", "karat.com",
    "calendly.com", "goodtime.io", "modernloop.io", "brighthire.ai",
    "icims.com", "jobvite.com", "bamboohr.com", "breezy.hr",
]

INTERVIEW_WORDS = [
    "interview", "phone screen", "schedule a call", "schedule time",
    "meet the team", "availability", "next steps in our process",
    "would love to chat", "set up a time", "hiring manager would like",
]
ASSESSMENT_WORDS = [
    "assessment", "coding challenge", "take-home", "take home",
    "hackerrank", "codesignal", "codility", "online test", "technical screen",
]
REJECTION_WORDS = [
    "unfortunately", "not moving forward", "other candidates",
    "decided to pursue", "not selected", "won't be moving", "will not be moving",
    "position has been filled", "no longer under consideration",
]
# Bulk-mail noise we never want in the inbox even if it matches a company name.
NOISE_WORDS = [
    "job alert", "jobs for you", "recommended jobs", "new jobs matching",
    "newsletter", "digest", "unsubscribe to stop receiving job alerts",
    # Machine mail from the application flow itself — the applier consumes
    # these; they're not a human response.
    "security code for your application", "verification code",
    "confirm your email",
]

# For mail matched ONLY by company name (not an ATS sender), require it to
# actually be about hiring — otherwise being a CUSTOMER of a company you
# applied to (e.g. Robinhood brokerage statements) floods the inbox.
HIRING_SIGNAL = re.compile(
    r"\b(appl(?:y|ying|ication|ied)|interview|recruit|talent|career|hiring|"
    r"candidate|position|role|resume|assessment)\b", re.I)


def _classify(subject: str, body: str, sender: str) -> str:
    text = f"{subject} {body}".lower()
    if any(w in text for w in NOISE_WORDS):
        return "noise"
    if any(w in text for w in REJECTION_WORDS):
        return "rejection"
    if any(w in text for w in ASSESSMENT_WORDS):
        return "assessment"
    if any(w in text for w in INTERVIEW_WORDS):
        return "interview"
    return "reply"


def _decode(value) -> str:
    if value is None:
        return ""
    try:
        from email.header import decode_header
        parts = decode_header(value)
        out = ""
        for chunk, enc in parts:
            if isinstance(chunk, bytes):
                out += chunk.decode(enc or "utf-8", errors="replace")
            else:
                out += chunk
        return out
    except Exception:
        return str(value)


def _text_snippet(msg) -> str:
    """First ~400 chars of the text/plain part (or stripped HTML)."""
    try:
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace")
                    break
            if not body:
                for part in msg.walk():
                    if part.get_content_type() == "text/html":
                        html = part.get_payload(decode=True).decode(
                            part.get_content_charset() or "utf-8", errors="replace")
                        body = re.sub(r"<[^>]+>", " ", html)
                        break
        else:
            body = msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", errors="replace")
        return re.sub(r"\s+", " ", body).strip()[:400]
    except Exception:
        return ""


def _fetch_messages(imap_user: str, imap_pass: str, since: datetime) -> list[dict]:
    """Blocking IMAP fetch — run in an executor. Returns raw message dicts."""
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        mail.login(imap_user, imap_pass)
        mail.select("INBOX", readonly=True)  # never mutate the user's inbox
        since_str = since.strftime("%d-%b-%Y")
        _, data = mail.search(None, f'(SINCE "{since_str}")')
        uids = data[0].split()
        # Newest first, capped — a first scan of a busy inbox could be huge.
        uids = uids[-300:]
        out = []
        for uid in reversed(uids):
            try:
                _, msg_data = mail.fetch(uid, "(BODY.PEEK[])")
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                out.append({
                    "message_id": (msg.get("Message-ID") or f"uid-{uid.decode()}").strip(),
                    "sender": _decode(msg.get("From")),
                    "subject": _decode(msg.get("Subject")),
                    "date": msg.get("Date"),
                    "snippet": _text_snippet(msg),
                })
            except Exception:
                continue
        return out
    finally:
        try:
            mail.logout()
        except Exception:
            pass


async def scan_user_responses(user_id: int) -> int:
    """Scan one user's inbox; insert new matches. Returns new-response count."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT email, preferences FROM users WHERE id = $1", user_id)
        if not row:
            return 0
        prefs = row["preferences"]
        if isinstance(prefs, str):
            try:
                prefs = json.loads(prefs)
            except Exception:
                prefs = {}
        prefs = prefs or {}
        imap_user = prefs.get("imap_user") or ""
        imap_pass = decrypt(prefs.get("imap_pass") or "")
        if not imap_user or not imap_pass:
            return 0

        # Companies the user actually applied to — the match universe.
        comp_rows = await conn.fetch("""
            SELECT DISTINCT j.company, a.job_id
              FROM applications a JOIN jobs j ON j.id = a.job_id
             WHERE a.user_id = $1 AND a.status IN ('applied', 'unknown')
               AND COALESCE(j.company, '') <> ''""", user_id)
    companies = {}
    for r in comp_rows:
        name = (r["company"] or "").strip()
        if len(name) > 3:
            companies[name.lower()] = r["job_id"]

    last_scan = prefs.get("responses_last_scan_at")
    if last_scan:
        try:
            since = datetime.fromisoformat(last_scan) - timedelta(days=1)  # overlap
        except Exception:
            since = datetime.now(timezone.utc) - timedelta(days=14)
    else:
        since = datetime.now(timezone.utc) - timedelta(days=14)

    loop = asyncio.get_event_loop()
    try:
        messages = await loop.run_in_executor(
            None, _fetch_messages, imap_user, imap_pass, since)
    except Exception as e:
        print(f"[Responses] IMAP fetch failed for user {user_id}: {type(e).__name__}: {e}")
        return 0

    new_count = 0
    alerts = []
    async with pool.acquire() as conn:
        for m in messages:
            sender_l = m["sender"].lower()
            hay = f"{sender_l} {m['subject'].lower()} {m['snippet'].lower()}"
            from_ats = any(d in sender_l for d in ATS_SENDER_DOMAINS)
            matched_company, job_id = None, None
            for cname, jid in companies.items():
                if cname in hay:
                    matched_company, job_id = cname, jid
                    break
            if not from_ats and not matched_company:
                continue  # unrelated mail
            if not from_ats and not HIRING_SIGNAL.search(hay):
                continue  # customer/transactional mail from an applied company
            kind = _classify(m["subject"], m["snippet"], m["sender"])
            if kind == "noise":
                continue
            try:
                received = email.utils.parsedate_to_datetime(m["date"])
                if received.tzinfo is None:
                    received = received.replace(tzinfo=timezone.utc)
            except Exception:
                received = datetime.now(timezone.utc)
            inserted = await conn.fetchval("""
                INSERT INTO responses
                       (user_id, job_id, message_id, sender, subject, snippet, kind, received_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (user_id, message_id) DO NOTHING
                RETURNING id""",
                user_id, job_id, m["message_id"], m["sender"][:300],
                m["subject"][:500], m["snippet"], kind,
                received.replace(tzinfo=None))
            if inserted:
                new_count += 1
                if kind in ("interview", "assessment"):
                    alerts.append(m)

        # Stamp the scan time (read-modify-write keeps other prefs intact).
        prefs["responses_last_scan_at"] = datetime.now(timezone.utc).isoformat()
        await conn.execute(
            "UPDATE users SET preferences = $1 WHERE id = $2",
            json.dumps(prefs), user_id)

    # Email alert for the responses that matter most — never for rejections.
    if alerts:
        try:
            from notifications import _send_email
            lines = "\n".join(f"  • {a['subject']}  (from {a['sender']})" for a in alerts[:5])
            _send_email(
                f"🎉 {len(alerts)} interview/assessment repl{'y' if len(alerts) == 1 else 'ies'} — check ApplyAgent",
                f"Your applications are getting traction:\n\n{lines}\n\n"
                f"Open the Responses tab to see everything: "
                f"https://apply-agent-frontend.vercel.app/dashboard",
                row["email"],
            )
        except Exception as e:
            print(f"[Responses] alert email failed: {type(e).__name__}: {e}")

    if new_count:
        print(f"[Responses] user {user_id}: {new_count} new response(s)")
    return new_count


async def scan_all_users() -> int:
    """Scan every user that has IMAP creds. Called from the auto-apply loop."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id FROM users")
    total = 0
    for r in rows:
        try:
            total += await scan_user_responses(r["id"])
        except Exception as e:
            print(f"[Responses] scan failed for user {r['id']}: {type(e).__name__}: {e}")
    return total
