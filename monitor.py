"""
monitor.py — tail the server logs, detect application failures,
analyse the screenshot with Claude Vision, and keep investigating
until the root cause is identified or fixed.

Usage:
    # Pipe the server output into the monitor:
    uvicorn api.main:app 2>&1 | python monitor.py

    # Or run the server normally and redirect logs:
    python monitor.py --logfile server.log
"""
import sys
import os
import re
import time
import argparse
import base64
import anthropic
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
SCREENSHOTS_DIR = Path("screenshots")
MAX_INVESTIGATE_ROUNDS = 3   # how many Vision rounds per failure before giving up

try:
    from config import ANTHROPIC_API_KEY
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
except Exception:
    client = anthropic.Anthropic()          # falls back to ANTHROPIC_API_KEY env var


# ── Patterns we watch for ─────────────────────────────────────────────────────
FAILURE_RE   = re.compile(r"❌ FAILED.*?@\s*(.+)")
ERROR_RE     = re.compile(r"✗\s+(.+)")
JOB_START_RE = re.compile(r"Applying to:\s*(.+?)\s*@\s*(.+)")
SCREENSHOT_RE = re.compile(r"screenshot[s]?[:/\\]+\s*([\w\-\.\/]+\.png)", re.IGNORECASE)


def find_latest_screenshot(job_id_hint: str = "") -> Path | None:
    """Return the most-recently-modified PNG in screenshots/."""
    if not SCREENSHOTS_DIR.exists():
        return None
    pngs = sorted(SCREENSHOTS_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    if job_id_hint:
        for p in pngs:
            if job_id_hint.lower() in p.name.lower():
                return p
    return pngs[0] if pngs else None


def encode_image(path: Path) -> str:
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode()


def ask_claude_vision(screenshot: Path, context: str, round_num: int) -> str:
    """Send screenshot + context to Claude and get a diagnosis."""
    try:
        img_b64 = encode_image(screenshot)
        prompt = (
            f"Round {round_num} investigation.\n\n"
            f"Context from the application log:\n{context}\n\n"
            "You are debugging an automated job application bot. "
            "Look at this screenshot and diagnose what went wrong. "
            "Answer these questions:\n"
            "1. What stage of the form is visible?\n"
            "2. Is there a submit/apply button? What does it say exactly?\n"
            "3. Are there any validation errors, CAPTCHAs, or blockers?\n"
            "4. What CSS selector would reliably click the submit button on this page?\n"
            "5. What single action should the bot take next to proceed?\n\n"
            "Be specific and actionable. If you see a button, quote its exact text."
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                    {"type": "text", "text": prompt},
                ],
            }]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"Vision error: {e}"


def ask_claude_text(log_context: str) -> str:
    """Analyse the log text alone (no screenshot) to suggest a fix."""
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": (
                    "You are debugging an automated Playwright job-application bot.\n\n"
                    f"Here are the recent log lines for a FAILED application:\n\n{log_context}\n\n"
                    "Based on the log:\n"
                    "1. What is the most likely root cause?\n"
                    "2. What specific code change would fix it?\n"
                    "3. Rate your confidence (low/medium/high).\n\n"
                    "Be concise and specific."
                ),
            }]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"Text analysis error: {e}"


def investigate(job_title: str, company: str, log_lines: list[str]):
    """Run up to MAX_INVESTIGATE_ROUNDS of diagnosis for a failed application."""
    context = "\n".join(log_lines[-40:])   # last 40 lines most relevant
    print("\n" + "═" * 70)
    print(f"🔍  INVESTIGATING FAILURE: {job_title} @ {company}")
    print("═" * 70)

    # Round 1: text-only analysis
    print("\n[Round 1 / text analysis]")
    diagnosis = ask_claude_text(context)
    print(diagnosis)

    # Round 2+: Vision analysis of screenshots
    screenshot = find_latest_screenshot(job_id_hint=company.replace(" ", "_").lower()[:10])
    for round_num in range(2, MAX_INVESTIGATE_ROUNDS + 1):
        if not screenshot:
            print(f"\n[Round {round_num}] No screenshot found — skipping vision analysis")
            break
        print(f"\n[Round {round_num} / vision — {screenshot.name}]")
        vision_diagnosis = ask_claude_vision(screenshot, context, round_num)
        print(vision_diagnosis)

        # If diagnosis says it's resolved or gives a clear selector, stop
        lower = vision_diagnosis.lower()
        if any(w in lower for w in ("confirmation", "thank you", "application received", "submitted successfully")):
            print("\n✅  Vision confirms application was actually successful!")
            break
        if "captcha" in lower or "recaptcha" in lower:
            print("\n⚠️  CAPTCHA detected — manual intervention needed")
            break

    print("\n" + "═" * 70 + "\n")


def tail_stdin():
    """Read lines from stdin (piped server output)."""
    for line in sys.stdin:
        yield line.rstrip()


def tail_file(path: str):
    """Tail a log file, yielding new lines as they appear."""
    with open(path, "r") as f:
        f.seek(0, 2)  # seek to end
        while True:
            line = f.readline()
            if line:
                yield line.rstrip()
            else:
                time.sleep(0.2)


def run(line_iter):
    current_job_title = ""
    current_company = ""
    log_buffer: list[str] = []

    print("👁️  monitor.py watching for application failures...\n")

    for raw_line in line_iter:
        print(raw_line)          # pass-through so normal logs still appear
        log_buffer.append(raw_line)
        if len(log_buffer) > 200:
            log_buffer = log_buffer[-200:]

        # Track which job we're currently processing
        m = JOB_START_RE.search(raw_line)
        if m:
            current_job_title = m.group(1).strip()
            current_company   = m.group(2).strip()
            log_buffer = [raw_line]   # fresh buffer per job

        # Detect failure
        m = FAILURE_RE.search(raw_line)
        if m:
            company = m.group(1).strip() or current_company
            investigate(current_job_title, company, list(log_buffer))
            log_buffer = []
            continue

        # Also trigger on explicit error lines even without the ❌ marker
        m = ERROR_RE.search(raw_line)
        if m and any(kw in raw_line for kw in ("Submit button not found", "No form found", "Error:", "✗ Application error")):
            investigate(current_job_title, current_company, list(log_buffer))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monitor job-bot logs and auto-diagnose failures")
    parser.add_argument("--logfile", help="Tail a log file instead of reading from stdin")
    args = parser.parse_args()

    if args.logfile:
        run(tail_file(args.logfile))
    else:
        run(tail_stdin())
