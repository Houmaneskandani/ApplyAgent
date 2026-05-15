"""
Notification helpers — sends SMS via Twilio or email via SMTP.

MULTI-TENANT: prior to this version, `notify_application` sent every user's
apply notification to NOTIFY_EMAIL (the operator's inbox). That broke as soon
as the project had more than one user — the operator saw a flood of strangers'
applies and the candidates got nothing. The current contract is:

  - The CANDIDATE gets a user-friendly email at their own address about
    their own application (success / needs review / failed).
  - The OPERATOR (NOTIFY_EMAIL) gets an optional terse "log" email — useful
    while debugging, easy to silence by unsetting NOTIFY_EMAIL.
  - SMS (TWILIO_TO) is operator-only and only fires on FAILED / UNKNOWN
    outcomes; routine successes don't need to wake anyone up.

If neither SMTP nor Twilio is configured, everything no-ops (the `print()`
line in the dispatcher still logs the event to Railway).
"""
import asyncio
from config import (
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, TWILIO_TO,
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, NOTIFY_EMAIL,
    FRONTEND_URL,
)


def _send_sms(message: str):
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM and TWILIO_TO):
        return
    try:
        from twilio.rest import Client
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(body=message, from_=TWILIO_FROM, to=TWILIO_TO)
        print(f"  📱 SMS sent: {message[:60]}")
    except Exception as e:
        print(f"  ⚠ SMS failed: {e}")


def _send_email(subject: str, body: str, to_email: str):
    """Send `subject` + `body` to `to_email`. No-op if SMTP creds are
    missing OR `to_email` is empty (we never want to default-route to the
    operator — that was the multi-tenant bug)."""
    if not (SMTP_USER and SMTP_PASS and to_email):
        return
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = SMTP_USER
        msg["To"] = to_email
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, to_email, msg.as_string())
        print(f"  📧 Email sent to {to_email}: {subject}")
    except Exception as e:
        print(f"  ⚠ Email to {to_email} failed: {type(e).__name__}: {e}")


def _build_user_email(
    status: str, job_title: str, company: str, user_name: str,
) -> tuple[str, str]:
    """Compose a friendly subject + body for the CANDIDATE's mailbox."""
    name = (user_name or "").split(" ", 1)[0] or "there"
    dashboard = (FRONTEND_URL or "https://apply-agent-frontend.vercel.app").rstrip("/")
    if status == "applied":
        subject = f"✅ Your application to {company} was submitted"
        body = (
            f"Hi {name},\n\n"
            f"ApplyAgent just submitted your application:\n\n"
            f"  Role:    {job_title}\n"
            f"  Company: {company}\n\n"
            f"You can see this and the rest of your applications here:\n"
            f"{dashboard}/dashboard\n\n"
            f"Good luck!\n"
            f"— ApplyAgent"
        )
    elif status == "unknown":
        subject = f"⚠️ Your application to {company} needs review"
        body = (
            f"Hi {name},\n\n"
            f"ApplyAgent filled out your application to {company} for the\n"
            f"{job_title} role, but we couldn't confirm whether the\n"
            f"submission was accepted. It's in the 'Needs Review' tab of\n"
            f"your dashboard — please check and either confirm it landed\n"
            f"or hit Retry:\n\n"
            f"{dashboard}/dashboard\n\n"
            f"No credit was charged for this one.\n"
            f"— ApplyAgent"
        )
    else:  # failed
        subject = f"❌ Application to {company} failed"
        body = (
            f"Hi {name},\n\n"
            f"ApplyAgent tried to apply to {job_title} at {company} but\n"
            f"the submission failed. No credit was charged. You can retry\n"
            f"from your dashboard:\n\n"
            f"{dashboard}/dashboard\n\n"
            f"— ApplyAgent"
        )
    return subject, body


async def notify_application(
    job_title: str,
    company: str,
    status: str,
    user_name: str = "",
    user_email: str = "",
):
    """Send a notification when an application completes.

    Args:
        job_title:  the role applied to (e.g. "Senior Backend Engineer")
        company:    the company name
        status:     one of "applied" | "unknown" | "failed"
        user_name:  the candidate's full name (for the greeting)
        user_email: the candidate's email — REQUIRED for them to get a
                    notification. Without it, only the operator (if
                    NOTIFY_EMAIL is set) gets emailed.
    """
    emoji = "✅" if status == "applied" else "⚠️" if status == "unknown" else "❌"
    ops_short = f"{emoji} {status.upper()}: {job_title} @ {company}"
    print(f"  {ops_short}")

    loop = asyncio.get_event_loop()

    # 1. User-facing email — to the candidate's own mailbox.
    if user_email:
        user_subject, user_body = _build_user_email(
            status, job_title, company, user_name,
        )
        await loop.run_in_executor(
            None, _send_email, user_subject, user_body, user_email,
        )

    # 2. Operator monitoring email — only if NOTIFY_EMAIL is set AND it's
    # a different mailbox (otherwise we'd double-send to the user when the
    # operator is also the user, as in solo-dev mode).
    if NOTIFY_EMAIL and NOTIFY_EMAIL.strip().lower() != (user_email or "").strip().lower():
        ops_subject = f"[ApplyAgent] {ops_short}"
        ops_body = (
            f"User:    {user_name or '?'} <{user_email or 'unknown'}>\n"
            f"Status:  {status}\n"
            f"Role:    {job_title}\n"
            f"Company: {company}\n"
        )
        await loop.run_in_executor(
            None, _send_email, ops_subject, ops_body, NOTIFY_EMAIL,
        )

    # 3. SMS — operator-only, only for high-stakes outcomes. Successes don't
    # need to wake anyone up; failures and unknowns might want attention.
    if status in ("failed", "unknown"):
        await loop.run_in_executor(None, _send_sms, ops_short)
