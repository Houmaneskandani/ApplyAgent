#!/usr/bin/env python3
"""
One-time ZipRecruiter login capture.

ZipRecruiter 1-Click Apply submits through YOUR logged-in ZR account — there's
no anonymous form. This script lets you log in once through a real browser
window, captures the authenticated session (cookies + the exact User-Agent the
session was issued to), and uploads it to ApplyAgent (Fernet-encrypted at rest).
The apply worker then replays that session.

Run it on your own machine (needs a display + Playwright Chromium):

    cd job-bot
    venv/bin/python -m playwright install chromium     # first time only
    venv/bin/python scripts/capture_ziprecruiter_session.py

Optional flags:
    --api    ApplyAgent API base (default: production)
    --keep-open   leave the browser open after capture (debugging)

You'll be prompted for your ApplyAgent email + password (to authorize the
upload — NOT your ZipRecruiter password, which we never see or store).

SECURITY: a local backup is written to scripts/ziprecruiter_session_backup.json
(gitignored). It contains auth cookies — delete it once the upload succeeds.
"""
import argparse
import getpass
import json
import os
import sys

DEFAULT_API = "https://applyagent-production.up.railway.app"
LOGIN_URL = "https://www.ziprecruiter.com/authn/login"
BACKUP_PATH = os.path.join(os.path.dirname(__file__), "ziprecruiter_session_backup.json")

# A logged-in ZR session bounces AWAY from /authn/login. We use that as the
# primary "are you logged in?" heuristic, plus a cookie sanity check.
LOGIN_PATH_FRAGMENT = "/authn/login"


def _login_applyagent(api: str) -> str:
    import httpx
    print("\nApplyAgent login (to authorize the upload — this is NOT your ZipRecruiter password):")
    email = input("  ApplyAgent email: ").strip()
    password = getpass.getpass("  ApplyAgent password: ")
    try:
        r = httpx.post(f"{api}/auth/login", json={"email": email, "password": password}, timeout=30)
    except Exception as e:
        sys.exit(f"\n✗ Could not reach {api}: {e}")
    if r.status_code != 200:
        sys.exit(f"\n✗ ApplyAgent login failed (HTTP {r.status_code}): {r.text[:200]}")
    token = r.json().get("token")
    if not token:
        sys.exit("\n✗ ApplyAgent login returned no token.")
    print("  ✓ Authorized.")
    return token


def _upload(api: str, token: str, ua: str, state: dict) -> None:
    import httpx
    r = httpx.post(
        f"{api}/profile/ziprecruiter-session",
        headers={"Authorization": f"Bearer {token}"},
        json={"user_agent": ua, "storage_state": state},
        timeout=30,
    )
    if r.status_code == 200:
        body = r.json()
        print(f"\n✓ Uploaded to ApplyAgent — {body.get('cookies')} cookies stored, encrypted.")
        print("  ZipRecruiter is now connected. You can delete the local backup:")
        print(f"    rm {BACKUP_PATH}")
    else:
        print(f"\n✗ Upload failed (HTTP {r.status_code}): {r.text[:300]}")
        print(f"  Your session is saved locally at {BACKUP_PATH} — you can retry the upload.")


def main():
    ap = argparse.ArgumentParser(description="Capture a ZipRecruiter login session for ApplyAgent.")
    ap.add_argument("--api", default=DEFAULT_API, help="ApplyAgent API base URL")
    ap.add_argument("--keep-open", action="store_true", help="leave the browser open after capture")
    args = ap.parse_args()

    # Authorize the upload FIRST so we fail fast on bad ApplyAgent creds,
    # before making the user sit through a ZR login.
    token = _login_applyagent(args.api)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("✗ Playwright not installed in this venv. Run: venv/bin/pip install playwright")

    with sync_playwright() as p:
        # HEADED so you can actually log in (and solve any human-verification
        # challenge yourself). We do NOT override the UA — we capture whatever
        # this real browser reports, then the applier replays exactly that.
        #
        # CRITICAL: use your REAL installed Google Chrome (channel="chrome"),
        # not Playwright's bundled Chromium. ZipRecruiter's bot wall
        # (PerimeterX/HUMAN) fingerprints Chromium and loops "verify you are
        # human" forever even when a human solves it. Real Chrome +
        # --disable-blink-features=AutomationControlled (drops the
        # navigator.webdriver tell) sails past it. We fall back to bundled
        # Chromium only if Chrome isn't installed.
        profile_dir = os.path.join(os.path.dirname(__file__), ".zr_chrome_profile")
        launch_common = dict(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = None
        last_err = None
        for kwargs in ({"channel": "chrome", **launch_common}, dict(launch_common)):
            try:
                # Persistent context = a real on-disk Chrome profile, so you
                # only have to clear the wall once and the fingerprint stays
                # consistent across runs.
                context = p.chromium.launch_persistent_context(profile_dir, **kwargs)
                using = "Google Chrome" if kwargs.get("channel") == "chrome" else "bundled Chromium"
                print(f"  (launched {using})")
                break
            except Exception as e:
                last_err = e
        if context is None:
            sys.exit(f"✗ Could not launch a browser: {last_err}\n"
                     f"  If you don't have Chrome: venv/bin/python -m playwright install chromium")

        # Belt-and-suspenders: hide the automation tell at the JS layer too.
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded")

        print("\n" + "=" * 64)
        print("  A browser window just opened on the ZipRecruiter login page.")
        print("  1. Log in to ZipRecruiter (solve any 'press & hold' challenge).")
        print("  2. Make sure your resume is uploaded to your ZR profile.")
        print("  3. When you're fully logged in (you can see your account),")
        print("     come back here and press ENTER.")
        print("=" * 64)
        input("\n  ➜ Press ENTER once you're logged in to ZipRecruiter... ")

        # Sanity: warn if we still look logged-out.
        cur = (page.url or "").lower()
        if LOGIN_PATH_FRAGMENT in cur:
            print("\n  ⚠ The browser is still on the login page. If you're not actually")
            print("    logged in, the captured session won't work.")
            if input("    Capture anyway? [y/N]: ").strip().lower() != "y":
                context.close()
                sys.exit("  Aborted — re-run when you're logged in.")

        ua = page.evaluate("() => navigator.userAgent")
        state = context.storage_state()
        n_cookies = len(state.get("cookies", []))

        if not args.keep_open:
            context.close()

    # Local encrypted-at-rest? No — backup is plaintext on YOUR machine only.
    # It's gitignored; delete after upload. The SERVER copy is Fernet-encrypted.
    try:
        with open(BACKUP_PATH, "w") as f:
            json.dump({"user_agent": ua, "storage_state": state}, f)
    except Exception as e:
        print(f"  ⚠ Could not write local backup: {e}")

    print(f"\n  Captured {n_cookies} cookies.")
    print(f"  UA: {ua[:80]}...")
    if n_cookies == 0:
        sys.exit("\n✗ No cookies captured — you weren't logged in. Re-run and log in first.")

    _upload(args.api, token, ua, state)


if __name__ == "__main__":
    main()
