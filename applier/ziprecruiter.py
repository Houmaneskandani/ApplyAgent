"""
ZipRecruiter applier.

UNLIKE every other applier in this project, ZipRecruiter 1-Click Apply is NOT
an anonymous form-fill. It submits *through the candidate's logged-in ZR
account* using the resume/profile they uploaded to ZipRecruiter. So this
applier:

  1. Loads a previously-captured ZR session (storage_state + the UA it was
     issued to) from user_info["_ziprecruiter_session"]. apply.py decrypts it
     out of the users.ziprecruiter_session column.
  2. Replays that EXACT UA (ZR clearance cookies are UA-bound) via the
     stealth_session user_agent_override.
  3. Detects whether we're actually logged in (vs challenged / expired) and
     fails LOUDLY with an actionable note if not — never a silent success.
  4. Classifies the job: native 1-Click (data-quick-apply="one_click" /
     ".quick_apply_btn") vs "Apply on company site" external redirect.
       - native 1-click  → click, then confirm submission or route to the
                            screening-question form path.
       - external        → "unsupported" (the company ATS link should be
                            applied to via our other appliers instead).
  5. On any ambiguous outcome, returns "unknown" → Needs Review with a
     screenshot, rather than claiming "applied". (We deliberately removed the
     "no error == success" anti-pattern project-wide; ZR is no exception.)

Verified DOM anchors (from the ziprecruiter-1-click-filter extension source):
  - 1-click button:  .quick_apply_btn  with  data-quick-apply="one_click"
Everything else (success toast, screening-form field names) is treated as
best-effort with multiple fallbacks because ZR's bot wall blocks static
inspection.
"""
import asyncio
import os
from playwright.async_api import async_playwright

from applier.greenhouse import get_answer, set_job_context
from applier.browser_utils import stealth_session, trusted_click
from applier.reviewer import run_pre_submit_review

os.makedirs("screenshots", exist_ok=True)

APPLICATION_TIMEOUT = 240  # seconds — generous; ZR pages are heavy

# Text signals that we landed on a bot-verification wall rather than the job.
# ZipRecruiter fronts everything with CLOUDFLARE — its "Just a moment..." /
# "Performing security verification" interstitial blocks headless/automated
# browsers even with valid session cookies (cf_clearance is IP+UA+TLS bound).
_CHALLENGE_SIGNALS = (
    "press & hold",
    "press and hold",
    "verify you are a human",
    "are you a robot",
    "px-captcha",
    "/authn/login",  # bounced back to login = session not valid
    "checking your browser",
    # Cloudflare managed-challenge interstitial:
    "just a moment",
    "performing security verification",
    "verifies you are not a bot",
    "verify you are not a bot",
    "needs to review the security of your connection",
    "cf-chl",
    "cloudflare",
)

# Positive confirmation that the application actually went through.
_SUCCESS_SIGNALS = (
    "application submitted",
    "your application has been submitted",
    "thanks for applying",
    "thank you for applying",
    "application sent",
    "you've applied",
    "you applied",
    "we've sent your application",
)


async def apply_ziprecruiter(
    job: dict, dry_run: bool = True, user_info: dict = None, profile_text: str = None,
) -> str:
    info = user_info or {}
    set_job_context(job)  # tailor cover letters / "why here" to THIS job
    sess = info.get("_ziprecruiter_session")  # {"ua":..., "state":{...}} or None

    print(f"\n  Applying to: {job.get('title')} @ {job.get('company')} (ZipRecruiter)")
    print(f"  URL: {job.get('url')}")

    # ── 0. Must have a captured session — there's no anonymous ZR apply ──
    if not sess or not sess.get("state"):
        print("    ✗ No ZipRecruiter session on file — cannot 1-Click Apply.")
        if user_info is not None:
            user_info["_reviewer_notes"] = (
                "ZipRecruiter isn't connected. 1-Click Apply needs your logged-in "
                "ZR session. Run the one-time capture on your computer: "
                "`cd job-bot && venv/bin/python scripts/capture_ziprecruiter_session.py`, "
                "log in to ZipRecruiter, then retry this job."
            )
        return "failed"

    ua = sess.get("ua") or None
    state = sess["state"]
    job_id = job.get("id", "unknown")

    # Residential proxy for the Cloudflare wall. Prefer a ZR-specific proxy so
    # only this ATS burns metered residential bandwidth; fall back to the
    # global PROXY_URL. For clearance to validate, this should be the SAME
    # (static/sticky) IP the session was captured through.
    zr_proxy = os.getenv("ZIPRECRUITER_PROXY_URL") or os.getenv("PROXY_URL") or None
    if zr_proxy:
        print(f"    → routing through residential proxy {zr_proxy.split('@')[-1][:40]}")

    try:
        async with async_playwright() as p:
            async with stealth_session(
                p,
                url=job.get("url", ""),
                user_id=info.get("user_id"),
                persist_state=False,             # session comes from DB, not FS
                storage_state_override=state,    # replay the captured cookies
                user_agent_override=ua,          # ...with the UA they were issued to
                proxy_override=zr_proxy,         # ...through the same residential IP
            ) as (_browser, _context, page):
                try:
                    await page.goto(job["url"], timeout=60000, wait_until="domcontentloaded")
                    await asyncio.sleep(3)
                except Exception as e:
                    print(f"    ✗ Could not load job page: {type(e).__name__}: {e}")
                    return "failed"

                # ── 1. Bot wall / expired session detection ──
                cur_url = (page.url or "").lower()
                try:
                    body_txt = (await page.inner_text("body")).lower()
                except Exception:
                    body_txt = ""
                if any(sig in cur_url or sig in body_txt for sig in _CHALLENGE_SIGNALS):
                    print(f"    ✗ ZR challenged the session (url={page.url[:80]}).")
                    await _shot(page, f"zr_challenged_{job_id}")
                    if user_info is not None:
                        user_info["_reviewer_notes"] = (
                            "ZipRecruiter challenged the saved session (bot wall or "
                            "expired login). This usually means the apply ran from a "
                            "datacenter IP the session wasn't issued to. Fixes: set "
                            "PROXY_URL to a residential proxy, or re-capture the session."
                        )
                    return "unknown"

                # ── 2. Find the apply control + classify native vs external ──
                kind, btn = await _find_apply_button(page)
                if kind == "none":
                    print("    ✗ No apply button found (job may be closed or expired).")
                    await _shot(page, f"zr_noapply_{job_id}")
                    if user_info is not None:
                        user_info["_reviewer_notes"] = (
                            "No ZipRecruiter apply button found — the posting may be "
                            "closed, already applied to, or expired."
                        )
                    return "failed"

                if kind == "external":
                    print("    ℹ ZR 'Apply on company site' — not a native 1-click job.")
                    if user_info is not None:
                        user_info["_reviewer_notes"] = (
                            "This ZipRecruiter job redirects to the company's own site "
                            "('Apply on company site'), so it isn't a ZR 1-Click apply. "
                            "Apply via the company ATS link instead."
                        )
                    return "unsupported"

                # ── 3. Native 1-Click ──
                if dry_run:
                    await _shot(page, f"zr_dryrun_{job_id}")
                    print("    ✓ DRY RUN — 1-Click button found, not clicking. "
                          f"Screenshot: screenshots/zr_dryrun_{job_id}.png")
                    return "dry_run"

                print("    → Clicking 1-Click Apply...")
                clicked = await trusted_click(btn)
                if not clicked:
                    try:
                        await btn.click(timeout=5000)
                        clicked = True
                    except Exception as e:
                        print(f"    ✗ Could not click apply button: {type(e).__name__}")
                        return "failed"
                await asyncio.sleep(4)

                # ── 4. Outcome: success? screening form? ambiguous? ──
                return await _resolve_outcome(
                    page, job, info, profile_text, job_id,
                )

        # (context/browser closed by stealth_session)
    except Exception as e:
        import traceback
        print(f"    ✗ ZipRecruiter applier error: {type(e).__name__}: {e}")
        traceback.print_exc()
        return "failed"

    return "unknown"


async def _resolve_outcome(page, job, info, profile_text, job_id) -> str:
    """After the 1-click, decide: applied / form-to-fill / unknown."""
    # A screening-question modal/form may have appeared. Detect fillable
    # fields that belong to an application form (not the global search box).
    form_fields = page.locator(
        "form input:not([type=hidden]):not([type=submit]):not([type=button]):visible, "
        "form textarea:visible, form select:visible, "
        "[role=dialog] input:visible, [role=dialog] textarea:visible, [role=dialog] select:visible"
    )
    try:
        n_fields = await form_fields.count()
    except Exception:
        n_fields = 0

    # Positive confirmation text anywhere on the page?
    try:
        body_txt = (await page.inner_text("body")).lower()
    except Exception:
        body_txt = ""
    if any(sig in body_txt for sig in _SUCCESS_SIGNALS):
        print("    ✓ Confirmation text detected — application submitted!")
        await _shot(page, f"zr_applied_{job_id}")
        return "applied"

    # Did the apply button flip to an "Applied" state?
    try:
        applied_badge = page.locator(
            "text=/^\\s*applied\\s*$/i, .quick_apply_btn:has-text('Applied')"
        )
        if await applied_badge.count() > 0:
            print("    ✓ Apply button shows 'Applied' — submitted.")
            await _shot(page, f"zr_applied_{job_id}")
            return "applied"
    except Exception:
        pass

    # Screening questions to fill?
    if n_fields > 0:
        print(f"    → ZR screening form appeared ({n_fields} fields) — filling...")
        return await _fill_screening_form(page, job, info, profile_text, job_id)

    # Nothing we can positively confirm. Be honest: route to Needs Review.
    print("    ⚠ Could not confirm submission (no success signal, no form).")
    await _shot(page, f"zr_unknown_{job_id}")
    if info is not None:
        info["_reviewer_notes"] = (
            "Clicked 1-Click Apply but couldn't confirm the result. Check the "
            "screenshot in Needs Review — it may have gone through, or ZR may have "
            "shown an unexpected step."
        )
    return "unknown"


async def _fill_screening_form(page, job, info, profile_text, job_id) -> str:
    """Best-effort fill of ZR screening questions, then reviewer + submit."""
    scope = page.locator("[role=dialog]")
    if await scope.count() == 0:
        scope = page.locator("form").first

    # Text / textarea questions
    try:
        text_inputs = await scope.locator(
            "input[type=text]:visible, input:not([type]):visible, textarea:visible"
        ).all()
    except Exception:
        text_inputs = []
    for el in text_inputs:
        try:
            if (await el.input_value()).strip():
                continue  # already prefilled from the ZR profile
            label = await _label_for(page, el)
            if not label:
                continue
            ans = await get_answer(label, "textarea", profile_text=profile_text)
            if ans:
                await el.fill(ans)
        except Exception:
            continue

    # Pre-submit reviewer audit (honors the force-submit override like the others)
    if not info.get("_force_submit"):
        blocked = await run_pre_submit_review(
            page,
            user_info=info,
            profile_text=profile_text,
            company=job.get("company", ""),
            job_title=job.get("title", ""),
            screenshot_prefix="zr_reviewer_blocked",
        )
        if blocked:
            return "unknown"

    # Submit the form
    submit = page.locator(
        "[role=dialog] button:has-text('Submit'):visible, "
        "[role=dialog] button:has-text('Continue'):visible, "
        "form button:has-text('Submit application'):visible, "
        "button:has-text('Submit application'):visible, "
        "button[type=submit]:visible"
    )
    if await submit.count() == 0:
        print("    ⚠ Screening form had no Submit button we recognize.")
        await _shot(page, f"zr_form_nosubmit_{job_id}")
        if info is not None:
            info["_reviewer_notes"] = (
                "ZipRecruiter showed a screening form but we couldn't find its Submit "
                "button. Check the screenshot in Needs Review."
            )
        return "unknown"

    if not await trusted_click(submit.first):
        try:
            await submit.first.click(timeout=5000)
        except Exception:
            return "unknown"
    await asyncio.sleep(4)

    try:
        body_txt = (await page.inner_text("body")).lower()
    except Exception:
        body_txt = ""
    if any(sig in body_txt for sig in _SUCCESS_SIGNALS):
        print("    ✓ Screening form submitted — confirmation detected!")
        await _shot(page, f"zr_applied_{job_id}")
        return "applied"

    print("    ⚠ Submitted screening form but couldn't confirm success.")
    await _shot(page, f"zr_form_unknown_{job_id}")
    if info is not None:
        info["_reviewer_notes"] = (
            "Filled + submitted a ZipRecruiter screening form but couldn't confirm the "
            "result. Check the Needs Review screenshot."
        )
    return "unknown"


async def _find_apply_button(page):
    """Return (kind, locator). kind ∈ {"native","external","none"}.

    Native is the verified `.quick_apply_btn` with data-quick-apply="one_click",
    or a button/anchor literally labeled "1-Click Apply". An "Apply on company
    site" / external-redirect control is classified "external"."""
    # 1. Verified native selector first.
    native = page.locator(
        ".quick_apply_btn[data-quick-apply='one_click'], "
        "button[data-quick-apply='one_click'], "
        "button:has-text('1-Click Apply'):visible, "
        "a:has-text('1-Click Apply'):visible"
    )
    try:
        if await native.count() > 0:
            return "native", native.first
    except Exception:
        pass

    # 2. Generic quick-apply class (still ZR-native).
    quick = page.locator(".quick_apply_btn:visible")
    try:
        if await quick.count() > 0:
            return "native", quick.first
    except Exception:
        pass

    # 3. External "Apply on company site".
    external = page.locator(
        "button:has-text('Apply on company site'):visible, "
        "a:has-text('Apply on company site'):visible, "
        "button:has-text('Apply on Company Site'):visible"
    )
    try:
        if await external.count() > 0:
            return "external", external.first
    except Exception:
        pass

    # 4. Last resort: a bare "Apply" button. Treat as native (the ZR-hosted
    #    apply); the outcome resolver will still demand a positive confirmation.
    generic = page.locator(
        "button:has-text('Apply Now'):visible, button:has-text('Quick Apply'):visible, "
        "button:has-text('Apply'):visible"
    )
    try:
        if await generic.count() > 0:
            return "native", generic.first
    except Exception:
        pass

    return "none", None


async def _label_for(page, el) -> str:
    """Best-effort human label for a form field."""
    for getter in (
        lambda: el.get_attribute("aria-label"),
        lambda: el.get_attribute("placeholder"),
        lambda: el.get_attribute("name"),
    ):
        try:
            v = await getter()
            if v and v.strip():
                return v.strip()[:200]
        except Exception:
            continue
    # <label for=id>
    try:
        fid = await el.get_attribute("id")
        if fid:
            lbl = page.locator(f"label[for='{fid}']")
            if await lbl.count() > 0:
                t = (await lbl.first.inner_text()).strip()
                if t:
                    return t[:200]
    except Exception:
        pass
    return ""


async def _shot(page, name: str) -> None:
    try:
        await page.screenshot(path=f"screenshots/{name}.png")
    except Exception:
        pass
