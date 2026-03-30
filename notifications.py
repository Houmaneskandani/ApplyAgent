"""
Notification helpers — sends SMS via Twilio or email via SMTP.
If neither is configured, just prints to console (always works).
"""
import asyncio
from config import (
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, TWILIO_TO,
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, NOTIFY_EMAIL,
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


def _send_email(subject: str, body: str):
    if not (SMTP_USER and SMTP_PASS and NOTIFY_EMAIL):
        return
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = SMTP_USER
        msg["To"] = NOTIFY_EMAIL
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, NOTIFY_EMAIL, msg.as_string())
        print(f"  📧 Email sent: {subject}")
    except Exception as e:
        print(f"  ⚠ Email failed: {e}")


async def notify_application(job_title: str, company: str, status: str, user_name: str = ""):
    """Send notification when an application completes."""
    emoji = "✅" if status == "applied" else "❌" if status == "failed" else "ℹ️"
    short = f"{emoji} {status.upper()}: {job_title} @ {company}"
    detail = f"ApplyAgent — {user_name or 'User'} application {status}\n\nJob: {job_title}\nCompany: {company}"

    # Always log
    print(f"  {short}")

    # Send in background thread so we don't block the async loop
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _send_sms, short)
    await loop.run_in_executor(None, _send_email, f"ApplyAgent: {short}", detail)
