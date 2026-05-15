import asyncio
import json
import os
import random
import re
from contextlib import asynccontextmanager
from urllib.parse import urlparse


# ─── Anti-detection: launch flags ────────────────────────────────────
#
# Why these flags:
#  - --headless=new   : modern headless mode that ships with real Chrome.
#                       Eliminates the WebGL "Google SwiftShader" renderer
#                       and most of the "I'm running a HeadlessChrome build"
#                       fingerprint that gave us away.
#  - --disable-blink-features=AutomationControlled : drops the headless-
#    chrome banner and the "Automation: true" hint that's set even when
#    navigator.webdriver is patched.
#  - The other flags are unrelated to detection; they keep Chromium happy
#    in Docker (no-sandbox, /dev/shm too small for default chrome cache).
#
# To opt-out of headless entirely (run headed under Xvfb for the strongest
# anti-detection profile), set HEADED_BROWSER=1 in the environment.
_BASE_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
]


def _headless_mode() -> str | bool:
    """
    Return the value to pass to `chromium.launch(headless=...)`.

    Playwright accepts either a bool or the string "new" (which selects the
    modern Chrome headless implementation). Default is "new". Setting
    HEADED_BROWSER=1 disables headless (requires a display server in
    Docker — see Dockerfile for Xvfb instructions).
    """
    if os.getenv("HEADED_BROWSER") == "1":
        return False
    # Playwright passes the string through to --headless=new under the hood.
    return "new"


# ─── Per-session fingerprint randomization ────────────────────────────
#
# Every BrowserContext we create picks one entry from each pool. This kills
# the "every application has the same canvas hash + WebGL vendor + screen
# dimensions" signal that ATS bot-scorers cluster on.
_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]

# Real laptop/desktop viewport sizes. We deliberately don't include exotic
# resolutions — most ATS forms render fine in these.
_VIEWPORTS = [
    (1440, 900),   # MacBook Pro 14"
    (1512, 982),   # MacBook Pro 14" effective
    (1366, 768),   # common Windows laptop
    (1536, 864),   # common Windows laptop
    (1920, 1080),  # desktop FHD
    (1600, 900),   # widescreen laptop
]

_LOCALES = ["en-US", "en-GB", "en-CA"]

_TIMEZONES = [
    "America/Los_Angeles", "America/New_York", "America/Chicago",
    "America/Denver", "America/Toronto", "Europe/London",
]


# ─── Storage state persistence ────────────────────────────────────────
#
# Why this matters: every ATS-fronting CDN (Cloudflare, Akamai, PerimeterX)
# applies a first-visit challenge. If you reuse a session whose cookies
# remember "this client passed our challenge an hour ago", the next visit
# is much less likely to trip the bot wall. We persist the BrowserContext
# storage_state per (user_id, ATS-domain) and reload it on the next apply.
#
# Storage backend is local FS by default (works on a single Railway
# container). The path is configurable via STORAGE_STATE_DIR. For multi-
# host scale-out, swap _read_storage_state / _write_storage_state to read
# from Supabase Storage.

_STORAGE_DIR = os.getenv("STORAGE_STATE_DIR", "/tmp/applyagent_storage_state")


def _safe_filename(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "_", s)[:120]


def _storage_state_path(user_id: int | str, url: str) -> str:
    domain = urlparse(url).netloc.lower() or "unknown"
    user = _safe_filename(str(user_id or "anon"))
    os.makedirs(os.path.join(_STORAGE_DIR, user), exist_ok=True)
    return os.path.join(_STORAGE_DIR, user, _safe_filename(domain) + ".json")


def _read_storage_state(user_id: int | str | None, url: str) -> dict | None:
    if not user_id:
        return None
    path = _storage_state_path(user_id, url)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None


async def _write_storage_state(context, user_id: int | str | None, url: str) -> None:
    if not user_id:
        return
    path = _storage_state_path(user_id, url)
    try:
        await context.storage_state(path=path)
    except Exception as e:
        print(f"    ⚠ Could not persist storage state: {e}")


# ─── Per-domain throttling ────────────────────────────────────────────
#
# We never want to hit the same ATS host 20 times in 5 minutes from one IP —
# that's textbook bot behavior. A process-wide Semaphore caps in-flight
# applications per netloc to MAX_CONCURRENT_PER_DOMAIN, and after each
# application we jitter-sleep before releasing the slot.
MAX_CONCURRENT_PER_DOMAIN = int(os.getenv("MAX_CONCURRENT_PER_DOMAIN", "2"))
DOMAIN_JITTER_MIN = float(os.getenv("DOMAIN_JITTER_MIN_SEC", "15"))
DOMAIN_JITTER_MAX = float(os.getenv("DOMAIN_JITTER_MAX_SEC", "60"))

_domain_locks: dict[str, asyncio.Semaphore] = {}


def _get_domain_semaphore(netloc: str) -> asyncio.Semaphore:
    """Lazy-create a process-wide Semaphore for a given ATS host."""
    sem = _domain_locks.get(netloc)
    if sem is None:
        sem = asyncio.Semaphore(MAX_CONCURRENT_PER_DOMAIN)
        _domain_locks[netloc] = sem
    return sem


@asynccontextmanager
async def throttle_for_url(url: str):
    """
    Async context manager that:
      1. Awaits a per-domain Semaphore slot before yielding.
      2. After the body runs, sleeps a random 15-60s before releasing,
         so two same-domain applies don't immediately follow each other.

    Usage:
        async with throttle_for_url(job_url):
            await apply_to_job(...)
    """
    domain = urlparse(url).netloc.lower() or "unknown"
    sem = _get_domain_semaphore(domain)
    await sem.acquire()
    try:
        yield
    finally:
        # Spawn the cooldown sleep, then release. This way the function
        # returns immediately to the caller, but the slot is held for an
        # extra jitter interval before another applier can grab it.
        async def _cooldown():
            try:
                await asyncio.sleep(random.uniform(DOMAIN_JITTER_MIN, DOMAIN_JITTER_MAX))
            finally:
                sem.release()
        asyncio.create_task(_cooldown())


# ─── Browser launch & context creation ────────────────────────────────


def _random_fingerprint() -> dict:
    """Pick a fresh per-session fingerprint."""
    vw, vh = random.choice(_VIEWPORTS)
    return {
        "user_agent": random.choice(_USER_AGENTS),
        "viewport": {"width": vw, "height": vh},
        "screen": {"width": vw, "height": vh},
        "locale": random.choice(_LOCALES),
        "timezone_id": random.choice(_TIMEZONES),
    }


def _proxy_config() -> dict | None:
    """
    Read proxy config from env. Returns None if not configured.

    Supports a single proxy or a comma-separated list (one is picked at
    random per launch). Format: scheme://user:pass@host:port

      PROXY_URL=http://user:pass@proxy.example.com:8000
      PROXY_URLS=http://u:p@h1:8000,http://u:p@h2:8000

    For residential rotating providers (Bright Data / IPRoyal / Smartproxy),
    the gateway URL is typically all you need — the provider handles rotation.
    """
    pool = os.getenv("PROXY_URLS", "").strip()
    if pool:
        choices = [p.strip() for p in pool.split(",") if p.strip()]
        if choices:
            url = random.choice(choices)
        else:
            url = ""
    else:
        url = os.getenv("PROXY_URL", "").strip()
    if not url:
        return None
    parsed = urlparse(url)
    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    cfg = {"server": server}
    if parsed.username:
        cfg["username"] = parsed.username
    if parsed.password:
        cfg["password"] = parsed.password
    return cfg


async def _apply_stealth(page) -> None:
    """Apply playwright-stealth to a page. Tolerant of API drift."""
    try:
        from playwright_stealth import Stealth
        stealth = Stealth()
        await stealth.apply_stealth_async(page)
        return
    except ImportError:
        print("    ⚠ playwright-stealth not installed (pip install playwright-stealth)")
        return
    except Exception as e:
        try:
            from playwright_stealth import stealth_async  # older API
            await stealth_async(page)
        except Exception:
            print(f"    ⚠ playwright-stealth apply failed: {e}")


@asynccontextmanager
async def stealth_session(
    playwright,
    *,
    url: str = "",
    user_id: int | str | None = None,
    persist_state: bool = True,
):
    """
    The new high-level browser entry point. Yields (browser, context, page)
    with all anti-detection measures applied:

      - Modern --headless=new (or headed if HEADED_BROWSER=1)
      - Per-session randomized UA / viewport / locale / timezone
      - Optional proxy from PROXY_URL / PROXY_URLS env
      - Persistent storage_state per (user_id, ATS-domain)
      - playwright-stealth patches on the page
      - Realistic navigator overrides (languages, hardwareConcurrency)

    Usage:
        async with stealth_session(p, url=job["url"], user_id=user["id"]) as (browser, context, page):
            await page.goto(job["url"])
            ...

    Backwards compat: the old `new_stealth_page(browser)` still works for
    code paths that haven't been migrated yet.
    """
    fp = _random_fingerprint()
    proxy = _proxy_config()

    launch_kwargs = {
        "headless": _headless_mode(),
        "args": list(_BASE_LAUNCH_ARGS),
    }
    if proxy:
        launch_kwargs["proxy"] = proxy

    browser = await playwright.chromium.launch(**launch_kwargs)

    context_kwargs = {
        "user_agent": fp["user_agent"],
        "viewport": fp["viewport"],
        "screen": fp["screen"],
        "locale": fp["locale"],
        "timezone_id": fp["timezone_id"],
        # Real browsers report multiple languages; the first is the primary.
        "extra_http_headers": {
            "Accept-Language": f"{fp['locale']},en;q=0.9",
        },
    }

    state = _read_storage_state(user_id, url) if (persist_state and url) else None
    if state:
        context_kwargs["storage_state"] = state

    context = await browser.new_context(**context_kwargs)

    # Realistic navigator overrides. playwright-stealth handles most of these,
    # but adding init_script gives us belt-and-suspenders coverage if stealth
    # is broken or removed.
    await context.add_init_script("""
        // navigator.webdriver should be undefined, not false (false reveals automation).
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        // Real browsers advertise multiple languages.
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        // 4 is a common, plausible value across consumer hardware.
        try {
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
        } catch (_) {}
        // Mock chrome.runtime presence (real Chrome has it; HeadlessChrome historically didn't).
        if (!window.chrome) window.chrome = { runtime: {} };
    """)

    page = await context.new_page()
    await _apply_stealth(page)

    try:
        yield browser, context, page
    finally:
        # Persist storage state before tearing down.
        if persist_state and url:
            try:
                await _write_storage_state(context, user_id, url)
            except Exception:
                pass
        try:
            await context.close()
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass


async def new_stealth_page(browser):
    """
    Legacy entry point — kept for backwards compatibility with appliers
    that haven't been migrated to `stealth_session` yet.

    NEW CODE should use stealth_session(playwright, url=..., user_id=...).
    """
    page = await browser.new_page()
    await _apply_stealth(page)
    return page


# ─── Trusted-click helper ─────────────────────────────────────────────
#
# Many ATSes (Lever's hCaptcha integration in particular) reject mouse
# events whose isTrusted === false — and that's exactly what
# `el.evaluate("el => el.click()")` produces. Real users generate trusted
# pointer events.
#
# This helper prefers the Playwright `.click()` path (which IS a trusted
# user-gesture path) and only falls back to dispatching the synthetic event
# when something is fundamentally wrong (covered element, etc.).
async def trusted_click(locator, *, timeout: int = 7000) -> bool:
    """
    Click a Playwright Locator with a trusted user-gesture if possible.

    Returns True if either trusted or fallback click succeeded.

    Use this instead of `await el.evaluate("e => e.click()")` anywhere
    the click feeds into form submission or CAPTCHA validation.
    """
    try:
        await locator.click(timeout=timeout)
        return True
    except Exception as e_native:
        # Force-click bypasses pointer-event interception checks but is still
        # a trusted event (Playwright synthesizes it from the browser side).
        try:
            await locator.click(timeout=timeout, force=True)
            return True
        except Exception:
            pass
        # Last-resort fallback: the old JS click. This will fail on CAPTCHA-
        # gated submits, but it's better than nothing for fields where
        # isTrusted doesn't matter (e.g., toggling a custom dropdown).
        try:
            await locator.evaluate("el => el.click()")
            return True
        except Exception:
            print(f"    ⚠ click failed (native + force + js): {e_native}")
            return False


_UUID_RE = re.compile(
    r'[A-F0-9]{8}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{12}',
    re.IGNORECASE
)

# Known static public keys for common ATS platforms
_KNOWN_KEYS = {
    "lever": "B7D8911C-5CC8-A9A3-35B0-554ACEE604DA",
}


async def _extract_arkose_public_key(page, source: str = "") -> str:
    """Extract the Arkose Labs / FunCaptcha public key from the page."""

    # 1. Check iframe src attributes
    iframes = await page.locator(
        "iframe[src*='arkoselabs'], iframe[src*='funcaptcha'], iframe[src*='arkoselabs.com']"
    ).all()
    for iframe in iframes:
        src = await iframe.get_attribute("src") or ""
        m = re.search(r'pkey=([a-fA-F0-9-]+)', src)
        if m:
            return m.group(1)
        m = _UUID_RE.search(src)
        if m:
            return m.group(0)

    # 2. Check script src attributes (Arkose loads via <script src=".../{KEY}/api.js">)
    scripts = await page.locator("script[src*='arkoselabs'], script[src*='funcaptcha'], script[src*='client-api']").all()
    for script in scripts:
        src = await script.get_attribute("src") or ""
        m = _UUID_RE.search(src)
        if m:
            return m.group(0)

    # 3. Search full page HTML with multiple patterns
    try:
        content = await page.content()
        for pattern in [
            r'"public_key"\s*:\s*"([A-F0-9-]{36})"',
            r"'public_key'\s*:\s*'([A-F0-9-]{36})'",
            r'pkey["\'\s:=]+([A-F0-9-]{36})',
            r'websitePublicKey["\'\s:=]+([A-F0-9-]{36})',
            r'FunCaptcha[^"\']*["\']([A-F0-9-]{36})["\']',
            r'arkoselabs\.com/v2/([A-F0-9-]{36})',
            r'client-api\.arkoselabs\.com/[^/]+/([A-F0-9-]{36})',
        ]:
            m = re.search(pattern, content, re.IGNORECASE)
            if m:
                print(f"    ✓ Found CAPTCHA key via pattern: {pattern[:40]}")
                return m.group(1)
    except Exception:
        pass

    # 4. JS deep-search: scan all inline script text and window properties
    try:
        result = await page.evaluate("""
            () => {
                const UUID = /[A-F0-9]{8}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{12}/i;
                // Search inline scripts
                for (const s of document.scripts) {
                    if (!s.src) {
                        const m = s.text.match(UUID);
                        if (m) return m[0];
                    } else if (s.src.includes('arkoselabs') || s.src.includes('funcaptcha')) {
                        const m = s.src.match(UUID);
                        if (m) return m[0];
                    }
                }
                // Check window.fc_config or similar globals
                for (const key of ['fc_config','ArkoseConfig','_fcConfig']) {
                    try {
                        const cfg = window[key];
                        if (cfg && cfg.public_key) return cfg.public_key;
                    } catch(e) {}
                }
                return null;
            }
        """)
        if result:
            print(f"    ✓ Found CAPTCHA key via JS evaluation")
            return result
    except Exception:
        pass

    # 5. Known static fallback for well-known ATS platforms
    if source and source.lower() in _KNOWN_KEYS:
        key = _KNOWN_KEYS[source.lower()]
        print(f"    ✓ Using known {source} CAPTCHA key as fallback")
        return key

    # 6. Last resort: any UUID on the page that appears near CAPTCHA-related words
    try:
        content = await page.content()
        # Find all UUIDs near captcha/arkose/funcaptcha context
        for match in _UUID_RE.finditer(content):
            start = max(0, match.start() - 100)
            ctx = content[start:match.end() + 100].lower()
            if any(w in ctx for w in ['arkose', 'funcaptcha', 'captcha', 'enforcement']):
                print(f"    ✓ Found CAPTCHA key via context search")
                return match.group(0)
    except Exception:
        pass

    print("    ✗ Could not extract CAPTCHA public key from page")
    return ""


async def _inject_arkose_token(page, token: str) -> bool:
    """Inject the solved Arkose Labs token into the page."""
    try:
        result = await page.evaluate(f"""
            (() => {{
                const t = {repr(token)};

                // Method 1: FunCaptcha callback function
                if (typeof FunCaptchaCallback === 'function') {{
                    FunCaptchaCallback(t);
                    return true;
                }}

                // Method 2: enforcement object (Lever's integration)
                if (typeof enforcement !== 'undefined') {{
                    if (typeof enforcement.passed === 'function') enforcement.passed(t);
                    else if (typeof enforcement.setToken === 'function') enforcement.setToken(t);
                    return true;
                }}

                // Method 3: hidden input fields
                const inputs = document.querySelectorAll(
                    'input[name*="captcha"], input[name*="arkose"], ' +
                    'input[name*="fc-token"], input[id*="captcha"]'
                );
                let found = false;
                for (const inp of inputs) {{
                    inp.value = t;
                    inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                    inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                    found = true;
                }}
                return found;
            }})()
        """)
        return bool(result)
    except Exception as e:
        print(f"    ⚠ Token injection error: {e}")
        return False


async def _detect_captcha_type(page) -> tuple[str, str]:
    """
    Detect CAPTCHA type. Returns (type, sitekey).
    Supported: hcaptcha, recaptcha, turnstile, funcaptcha.
    """
    import re as _re
    content = await page.content()

    # ── hCaptcha ──────────────────────────────────────────────
    if "hcaptcha.com" in content or "h-captcha" in content:
        # data-sitekey on widget div
        for sel in [".h-captcha[data-sitekey]", "#h-captcha[data-sitekey]",
                    "[data-hcaptcha-sitekey]"]:
            el = page.locator(sel)
            if await el.count() > 0:
                key = await el.first.get_attribute("data-sitekey") or \
                      await el.first.get_attribute("data-hcaptcha-sitekey") or ""
                if key:
                    return ("hcaptcha", key)
        # sitekey in page source
        m = _re.search(r'(?:data-sitekey|sitekey)[=:]["\'\s]*([a-f0-9-]{36})', content, _re.I)
        if m:
            return ("hcaptcha", m.group(1))
        return ("hcaptcha", "")

    # ── Cloudflare Turnstile ──────────────────────────────────
    if "challenges.cloudflare.com" in content or "turnstile" in content.lower():
        m = _re.search(r'(?:data-sitekey|sitekey)[=:]["\'\s]*(0x[A-Fa-f0-9]+)', content)
        key = m.group(1) if m else ""
        return ("turnstile", key)

    # ── Google reCAPTCHA ──────────────────────────────────────
    if "recaptcha" in content.lower() or "google.com/recaptcha" in content:
        m = _re.search(r'(?:data-sitekey|sitekey)[=:]["\'\s]*(6[A-Za-z0-9_-]{39})', content)
        key = m.group(1) if m else ""
        # Detect v3 vs v2 by checking for grecaptcha.execute
        captcha_type = "recaptchav3" if "grecaptcha.execute" in content else "recaptchav2"
        return (captcha_type, key)

    # ── Arkose / FunCaptcha ───────────────────────────────────
    if "arkoselabs" in content or "funcaptcha" in content:
        key = await _extract_arkose_public_key(page)
        return ("funcaptcha", key)

    return ("", "")


async def _inject_hcaptcha_token(page, token: str) -> bool:
    """Inject solved hCaptcha token into the page."""
    try:
        result = await page.evaluate(f"""
            (() => {{
                const t = {repr(token)};
                // Set the hidden response textarea
                const ta = document.querySelector(
                    'textarea[name="h-captcha-response"], [name="g-recaptcha-response"]'
                );
                if (ta) {{
                    ta.value = t;
                    ta.dispatchEvent(new Event('input', {{bubbles: true}}));
                }}
                // Fire hcaptcha callback if available
                if (window.hcaptcha) {{
                    try {{ window.hcaptcha.execute(); }} catch(e) {{}}
                }}
                // Try global callback patterns
                for (const key of Object.keys(window)) {{
                    if (key.startsWith('hcaptcha') || key.includes('Captcha')) {{
                        try {{
                            if (typeof window[key] === 'function') window[key](t);
                        }} catch(e) {{}}
                    }}
                }}
                return !!ta;
            }})()
        """)
        return True
    except Exception as e:
        print(f"    ⚠ hCaptcha injection error: {e}")
        return False


async def _solve_with_capsolver(page, api_key: str, source: str = "") -> tuple[str, str]:
    """
    Detect CAPTCHA type, call Capsolver, return (captcha_type, token).
    """
    import httpx

    captcha_type, site_key = await _detect_captcha_type(page)
    if not captcha_type:
        print("    ✗ Could not detect CAPTCHA type")
        return ("", "")

    url = page.url
    print(f"    → {captcha_type} detected (key: {site_key[:8]}...) — sending to Capsolver")

    if captcha_type == "hcaptcha":
        task = {"type": "HCaptchaTaskProxyless", "websiteURL": url, "websiteKey": site_key}
    elif captcha_type == "recaptchav2":
        task = {"type": "ReCaptchaV2TaskProxyless", "websiteURL": url, "websiteKey": site_key}
    elif captcha_type == "recaptchav3":
        task = {"type": "ReCaptchaV3TaskProxyless", "websiteURL": url,
                "websiteKey": site_key, "pageAction": "submit", "minScore": 0.5}
    elif captcha_type == "turnstile":
        task = {"type": "AntiTurnstileTaskProxyless", "websiteURL": url, "websiteKey": site_key}
    else:
        # FunCaptcha / Arkose
        if not site_key:
            site_key = await _extract_arkose_public_key(page, source=source)
        if not site_key:
            print("    ✗ Could not extract Arkose public key")
            return ("", "")
        task = {"type": "FunCaptchaTaskProxyless", "websiteURL": url, "websitePublicKey": site_key}

    payload = {"clientKey": api_key, "task": task}

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post("https://api.capsolver.com/createTask", json=payload)
            data = r.json()
            task_id = data.get("taskId")
            if not task_id:
                print(f"    ✗ Capsolver error: {data.get('errorDescription', 'unknown')}")
                return ("", "")

            print(f"    → Capsolver task {task_id} — waiting...")
            for _ in range(24):
                await asyncio.sleep(5)
                r = await client.post(
                    "https://api.capsolver.com/getTaskResult",
                    json={"clientKey": api_key, "taskId": task_id}
                )
                result = r.json()
                status = result.get("status")
                if status == "ready":
                    token = (
                        result.get("solution", {}).get("gRecaptchaResponse")
                        or result.get("solution", {}).get("token", "")
                    )
                    print(f"    ✓ Capsolver solved!")
                    return (captcha_type, token)
                elif status == "failed":
                    print(f"    ✗ Capsolver failed: {result.get('errorDescription', '')}")
                    return ("", "")

            print("    ✗ Capsolver timeout")
            return ("", "")
    except Exception as e:
        print(f"    ✗ Capsolver request error: {e}")
        return ("", "")


async def handle_captcha(page, source: str = ""):
    """
    Detect CAPTCHA and solve it automatically via Capsolver if configured,
    otherwise fall back to manual pause.
    """
    # Check for any known CAPTCHA
    captcha = page.locator(
        "iframe[src*='arkoselabs'], iframe[src*='funcaptcha'], "
        "iframe[src*='hcaptcha.com'], "
        "iframe[title*='verification'], iframe[title*='CAPTCHA'], "
        "iframe[title*='hCaptcha'], "
        ".h-captcha, #h-captcha, [id*='arkose'], [class*='captcha']"
    )
    if await captcha.count() == 0:
        return

    from config import CAPSOLVER_API_KEY

    if CAPSOLVER_API_KEY:
        print("\n  🔒 CAPTCHA detected — solving automatically via Capsolver...")
        captcha_type, token = await _solve_with_capsolver(page, CAPSOLVER_API_KEY, source=source)
        if token:
            if captcha_type == "hcaptcha":
                await _inject_hcaptcha_token(page, token)
            elif captcha_type in ("recaptchav2", "recaptchav3", "turnstile"):
                # These all use the same g-recaptcha-response textarea pattern
                await _inject_hcaptcha_token(page, token)
            else:
                await _inject_arkose_token(page, token)
            print("    ✓ CAPTCHA token injected!")
            await asyncio.sleep(2)
            return
        else:
            print("    ⚠ Capsolver did not return a token — trying manual fallback")
    else:
        print("\n  ⚠ CAPSOLVER_API_KEY not set — using manual mode")

    # Manual fallback — in headless mode we can't solve manually, so wait briefly then continue
    print("    ⚠ Continuing without CAPTCHA solve (headless mode — form may still work)")
    await asyncio.sleep(2)


wait_for_captcha_if_present = handle_captcha  # backward-compatible alias
