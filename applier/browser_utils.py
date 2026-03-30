import asyncio
import re
import os


async def new_stealth_page(browser):
    """Create a Playwright page with stealth mode applied to avoid bot detection."""
    page = await browser.new_page()
    try:
        from playwright_stealth import Stealth
        stealth = Stealth()
        await stealth.apply_stealth_async(page)
    except ImportError:
        print("    ⚠ playwright-stealth not installed, run: pip install playwright-stealth")
    except Exception as e:
        # Older API fallback
        try:
            from playwright_stealth import stealth_async
            await stealth_async(page)
        except Exception:
            print(f"    ⚠ playwright-stealth apply failed: {e}")
    return page


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
