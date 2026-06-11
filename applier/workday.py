"""
Workday ATS applier.
Handles: *.myworkdayjobs.com, *.workday.com/*/jobs
Workday is a multi-step wizard with heavy JavaScript rendering.
This applier handles the most common flow: Personal Info → Resume → Questions → Submit.
"""
import asyncio
import os
from playwright.async_api import async_playwright
from applier.greenhouse import get_answer
from applier.browser_utils import stealth_session, wait_for_captcha_if_present, trusted_click

os.makedirs("screenshots", exist_ok=True)

# Max steps to navigate through (safety limit)
MAX_STEPS = 8


async def apply_workday(job: dict, dry_run: bool = True, user_info: dict = None, profile_text: str = None) -> str:
    if not user_info or not profile_text:
        raise ValueError("user_info and profile_text are required")

    info = user_info
    print(f"\n  Applying to: {job['title']} @ {job['company']}")
    print(f"  URL: {job['url']}")

    async with async_playwright() as p:
        async with stealth_session(
            p, url=job["url"], user_id=info.get("user_id"),
        ) as (_browser, _context, page):
            try:
                print(f"    → Loading: {job['url']}")
                await page.goto(job["url"], timeout=60000, wait_until="domcontentloaded")
                await asyncio.sleep(3)
                await wait_for_captcha_if_present(page, source="workday")

                # Click Apply button
                apply_btn = page.locator(
                    "a:has-text('Apply'), button:has-text('Apply'), "
                    "[data-automation-id='applyButton'], "
                    "[aria-label*='Apply']"
                )
                if await apply_btn.count() > 0:
                    await apply_btn.first.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=20000)
                    await asyncio.sleep(3)
                    print("    ✓ Clicked Apply")
                else:
                    print("    ℹ No Apply button — may be directly on form")

                await wait_for_captcha_if_present(page, source="workday")

                # Workday sometimes asks to create account or sign in — try "Apply Manually" / guest
                for guest_sel in [
                    "a:has-text('Apply Manually')",
                    "button:has-text('Apply Manually')",
                    "a:has-text('Apply without an account')",
                    "a:has-text('Continue as Guest')",
                    "button:has-text('Continue as guest')",
                ]:
                    try:
                        el = page.locator(guest_sel)
                        if await el.count() > 0 and await el.first.is_visible():
                            await el.first.click()
                            await asyncio.sleep(2)
                            print(f"    ✓ Bypassed login: {guest_sel}")
                            break
                    except Exception:
                        pass

                # Walk through the multi-step wizard
                result = await _walk_workday_wizard(page, info, profile_text, dry_run, job)
                return result

            except Exception as e:
                import traceback
                print(f"    ✗ Error: {e}")
                traceback.print_exc()
                try:
                    await page.screenshot(path=f"screenshots/workday_error_{job.get('id', 'unknown')}.png")
                except Exception:
                    pass
                return "failed"
            finally:
                await asyncio.sleep(2)
                # cleanup handled by stealth_session


async def _walk_workday_wizard(page, info: dict, profile_text: str, dry_run: bool, job: dict) -> str:
    """Navigate through Workday's multi-step application wizard."""

    for step in range(MAX_STEPS):
        await asyncio.sleep(2)
        print(f"    → Step {step + 1}...")

        # Only trust RELIABLE post-submit confirmation signals. Loose matches
        # like "complete" in the URL or [class*='success'] (a common CSS
        # utility) fire on PRE-submit wizard pages — which falsely reported
        # "applied" (charging a credit + telling the user they applied when
        # they hadn't). Workday's real confirmation is data-automation-id=
        # 'confirmationPage' or an explicit "Thank you for applying" heading.
        url_lower = page.url.lower()
        confirmed_url = any(w in url_lower for w in ("/thankyou", "/confirmation", "submitted"))
        success_el = page.locator(
            "[data-automation-id='confirmationPage'], "
            "h1:has-text('Thank you for applying'), h2:has-text('Thank you for applying'), "
            "text=/your application (has been|was) submitted/i"
        )
        if confirmed_url or await success_el.count() > 0:
            print("    ✓ Workday confirmation detected")
            return "applied"

        # Fill whatever fields are visible on this step
        await _fill_workday_step(page, info, profile_text, step)

        # Dry run: screenshot and stop after first step
        if dry_run and step == 0:
            await page.screenshot(path=f"screenshots/workday_dry_{job.get('id', 'unknown')}.png", full_page=True)
            print(f"    ✓ DRY RUN — screenshot saved")
            return "dry_run"

        # Try to click Next / Continue / Submit. We pass user_info /
        # profile_text / job through so the helper can run the pre-submit
        # reviewer agent — but ONLY on the final "Submit" click (the
        # helper inspects the button text to decide).
        advanced = await _click_next_or_submit(
            page, step, user_info=info, profile_text=profile_text, job=job,
        )
        if not advanced:
            # Distinguish reviewer-block from genuine "no button found".
            # The helper sets `_reviewer_blocked_workday` on user_info
            # when its False return is actually a reviewer veto, not a
            # missing button. Map that to "unknown" so apply.py routes
            # it to Needs Review (with the existing reviewer notes the
            # helper already stashed via run_pre_submit_review).
            if info.get("_reviewer_blocked_workday"):
                print("    ✗ Reviewer blocked the final Workday submit — routing to Needs Review")
                return "unknown"
            print("    ✗ Could not advance — no Next/Submit button found")
            try:
                await page.screenshot(path=f"screenshots/workday_stuck_{step}.png")
            except Exception:
                pass
            return "failed"

        await asyncio.sleep(3)

        # Re-check for success after clicking submit
        url_lower = page.url.lower()
        if any(w in url_lower for w in ("thank", "confirm", "success", "submitted", "complete")):
            print("    ✓ Submitted successfully!")
            return "applied"

    print("    ✗ Exceeded max steps without success")
    return "failed"


async def _fill_workday_step(page, info: dict, profile_text: str, step: int):
    """Fill visible fields on current Workday wizard step."""

    # --- Personal info fields (Workday uses data-automation-id attributes) ---
    wd_fields = [
        ("[data-automation-id='legalNameSection_firstName'], input[id*='firstName']", info.get("first_name", "")),
        ("[data-automation-id='legalNameSection_lastName'],  input[id*='lastName']",  info.get("last_name", "")),
        ("[data-automation-id='email'], input[type='email']",                          info.get("email", "")),
        ("[data-automation-id='phone-number'], input[type='tel']",                     info.get("phone", "")),
        ("[data-automation-id='addressSection_addressLine1']",                         info.get("address", "")),
        ("[data-automation-id='addressSection_city']",                                 info.get("city", "")),
        ("[data-automation-id='addressSection_postalCode']",                           info.get("zip", "")),
    ]
    for selector, value in wd_fields:
        if not value:
            continue
        for sel in [s.strip() for s in selector.split(",")]:
            try:
                el = page.locator(sel)
                if await el.count() > 0 and await el.first.is_visible():
                    current_val = await el.first.input_value()
                    if current_val:
                        break
                    # Workday inputs need JS-based interaction to trigger React updates
                    await el.first.click()
                    await el.first.fill(value)
                    await el.first.press("Tab")
                    print(f"    ✓ Filled {sel.split('[')[0]}: '{value}'")
                    break
            except Exception:
                continue

    # --- Resume upload (step 0 or whenever file input is visible) ---
    resume_path = info.get("resume_path", "")
    if resume_path and os.path.exists(resume_path):
        file_inputs = await page.locator("input[type='file']:not([aria-hidden='true'])").all()
        for fi in file_inputs:
            try:
                if await fi.is_visible() or True:  # file inputs are often hidden
                    await fi.set_input_files(resume_path)
                    print("    ✓ Resume uploaded")
                    await asyncio.sleep(3)  # Workday parses the resume
                    break
            except Exception:
                pass

    # --- Workday country / state dropdowns ---
    # Country
    country_sel = page.locator(
        "[data-automation-id='addressSection_countryRegion'] button, "
        "[aria-label*='Country'] button, "
        "button[aria-haspopup][aria-label*='ountry']"
    )
    if await country_sel.count() > 0 and await country_sel.first.is_visible():
        try:
            btn_text = await country_sel.first.inner_text()
            if not btn_text.strip() or "select" in btn_text.lower():
                await country_sel.first.click()
                await asyncio.sleep(1)
                us_option = page.locator("li:has-text('United States'), [role='option']:has-text('United States')")
                if await us_option.count() > 0:
                    await us_option.first.click()
                    print("    ✓ Country: United States")
                    await asyncio.sleep(1)
        except Exception:
            pass

    # --- Generic text / textarea / select for custom questions ---
    await _fill_workday_generic_questions(page, info, profile_text)


async def _fill_workday_generic_questions(page, info: dict, profile_text: str):
    """Fill any remaining visible questions on this Workday step."""

    # Textareas (custom questions)
    textareas = await page.locator("textarea:visible").all()
    for el in textareas:
        try:
            val = await el.input_value()
            if val:
                continue
            label = await _get_wd_label(page, el)
            if not label:
                continue
            print(f"    ? Textarea: {label[:60]}")
            answer = await get_answer(label, "textarea", profile_text=profile_text)
            if answer:
                await el.click()
                await el.fill(answer)
                print(f"    ✓ {answer[:60]}")
        except Exception as e:
            print(f"    ✗ Textarea error: {e}")

    # Visible text inputs not already filled
    inputs = await page.locator("input[type='text']:visible").all()
    for el in inputs:
        try:
            val = await el.input_value()
            if val:
                continue
            el_id = (await el.get_attribute("id") or "") + (await el.get_attribute("data-automation-id") or "")
            if any(s in el_id.lower() for s in ("first", "last", "email", "phone", "address", "city", "postal", "zip")):
                continue
            label = await _get_wd_label(page, el)
            if not label:
                continue
            print(f"    ? Input: {label[:60]}")
            answer = await get_answer(label, "text", profile_text=profile_text)
            if answer:
                await el.click()
                await el.fill(answer)
                await el.press("Tab")
                print(f"    ✓ {answer[:60]}")
        except Exception as e:
            print(f"    ✗ Input error: {e}")

    # Workday custom dropdowns (button-based, not <select>)
    wd_dropdowns = await page.locator(
        "button[aria-haspopup='listbox']:visible, "
        "[data-automation-id*='dropdown']:visible button:visible"
    ).all()
    for btn in wd_dropdowns:
        try:
            btn_text = (await btn.inner_text()).strip()
            if btn_text and "select" not in btn_text.lower():
                continue  # already has a value
            label = await _get_wd_label(page, btn)
            if not label:
                continue
            await btn.click()
            await asyncio.sleep(1)
            options = await page.locator("[role='option']:visible, li[role='option']:visible").all()
            if not options:
                await btn.press("Escape")
                continue
            option_texts = [await o.inner_text() for o in options]
            print(f"    ? Dropdown: {label[:60]}")
            answer = await get_answer(f"{label}. Options: {option_texts}", "dropdown", profile_text=profile_text)
            clicked = False
            for o, txt in zip(options, option_texts):
                if answer.lower() in txt.lower() or txt.lower() in answer.lower():
                    await o.click()
                    print(f"    ✓ Selected: {txt}")
                    clicked = True
                    break
            if not clicked:
                await options[0].click()
                print(f"    ✓ Selected (first): {option_texts[0]}")
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"    ✗ Dropdown error: {e}")

    # Workday Yes/No radio questions
    radio_groups: dict[str, list] = {}
    for el in await page.locator("input[type='radio']:visible").all():
        name = await el.get_attribute("name") or await el.get_attribute("data-automation-id") or ""
        radio_groups.setdefault(name, []).append(el)

    for group_name, radios in radio_groups.items():
        try:
            options = []
            for r in radios:
                r_id = await r.get_attribute("id")
                lbl = ""
                if r_id:
                    lbl_el = page.locator(f"label[for='{r_id}']")
                    if await lbl_el.count() > 0:
                        lbl = (await lbl_el.first.inner_text()).strip()
                options.append((r, lbl or await r.get_attribute("value") or ""))

            question = await _get_wd_group_label(page, radios[0])
            if not question:
                continue

            option_labels = [o[1] for o in options]
            print(f"    ? Radio: {question[:60]} → {option_labels}")
            answer = await get_answer(f"{question}. Options: {option_labels}", "radio", profile_text=profile_text)
            if not answer:
                continue
            for r, lbl in options:
                if answer.lower() in lbl.lower() or lbl.lower() in answer.lower():
                    await trusted_click(r)
                    print(f"    ✓ Radio: {lbl}")
                    break
        except Exception as e:
            print(f"    ✗ Radio error: {e}")


async def _click_next_or_submit(
    page,
    step: int,
    user_info: dict | None = None,
    profile_text: str | None = None,
    job: dict | None = None,
) -> bool:
    """Click the Next or Submit button to advance the Workday wizard.

    Runs the pre-submit reviewer agent ONLY when the matched button is
    actually a "Submit" (not a "Next" / "Continue" / "Save and Continue").
    Workday calls this helper on every step transition, so a naive
    integration would burn ~$0.01 + 10s on every Next click.

    Detection: we read the visible button's text after locating it and
    compare to a known set of final-submit labels. If the user_info or
    profile_text aren't supplied (legacy callers), the reviewer is
    skipped entirely.
    """
    # Try Submit first (so on the last step we click it before falling
    # through to Next selectors that might also match).
    candidates = [
        "button:has-text('Submit'):visible",
        "button:has-text('Save and Continue'):visible",
        "button:has-text('Next'):visible",
        "button:has-text('Continue'):visible",
        "[data-automation-id='bottom-navigation-next-button']:visible",
        "[data-automation-id='bottom-navigation-save-button']:visible",
        "button[aria-label*='Next']:visible",
    ]
    # Labels we consider a "final submit" (case-insensitive, stripped).
    FINAL_SUBMIT_LABELS = {"submit", "submit application", "submit my application"}

    for sel in candidates:
        try:
            btn = page.locator(sel)
            if await btn.count() == 0:
                continue
            # Read the actual button text to decide if this is the FINAL
            # submit vs. an intermediate Next. The selector "has-text('Submit')"
            # would also match "Save and Submit Later", so don't trust the
            # selector — trust the rendered text.
            try:
                btn_text = (await btn.first.inner_text() or "").strip().lower()
            except Exception:
                btn_text = ""
            is_final_submit = btn_text in FINAL_SUBMIT_LABELS

            if is_final_submit and user_info is not None and profile_text is not None:
                from applier.reviewer import run_pre_submit_review
                blocked = await run_pre_submit_review(
                    page,
                    user_info=user_info,
                    profile_text=profile_text,
                    company=(job or {}).get("company", ""),
                    job_title=(job or {}).get("title", ""),
                    screenshot_prefix="workday_reviewer_blocked",
                )
                if blocked:
                    # Signal to the caller we got blocked, NOT that we failed
                    # to advance — _walk_workday_wizard treats the False
                    # return as "failed", which is wrong. Stash a sentinel
                    # in user_info so the caller can distinguish.
                    user_info["_reviewer_blocked_workday"] = True
                    return False

            print(f"    → Clicking: {sel.split(':')[0]}  "
                  f"({'FINAL SUBMIT' if is_final_submit else 'next'})")
            await btn.first.click()
            return True
        except Exception:
            continue
    return False


async def _get_wd_label(page, el) -> str:
    # 1. aria-labelledby
    labelledby = await el.get_attribute("aria-labelledby")
    if labelledby:
        for lid in labelledby.split():
            ref = page.locator(f"#{lid}")
            if await ref.count() > 0:
                text = (await ref.first.inner_text()).strip()
                if text:
                    return text
    # 2. aria-label
    aria = await el.get_attribute("aria-label")
    if aria:
        return aria.strip()
    # 3. label[for=id]
    field_id = await el.get_attribute("id")
    if field_id:
        lbl = page.locator(f"label[for='{field_id}']")
        if await lbl.count() > 0:
            return (await lbl.first.inner_text()).strip()
    # 4. data-automation-id
    auto_id = await el.get_attribute("data-automation-id")
    if auto_id:
        # Convert camelCase to words: "workAuthorizationCountry" → "work authorization country"
        import re
        words = re.sub(r'([A-Z])', r' \1', auto_id).strip().lower()
        if len(words) > 3:
            return words
    # 5. Walk up
    for levels in ["xpath=..", "xpath=../..", "xpath=../../.."]:
        try:
            parent = el.locator(levels)
            if await parent.count() == 0:
                continue
            for sel in ["label", "legend", "[class*='label']", "[data-automation-id*='label']"]:
                lbl = parent.locator(sel)
                if await lbl.count() > 0:
                    text = (await lbl.first.inner_text()).strip()
                    if len(text) > 3:
                        return text
        except Exception:
            continue
    return ""


async def _get_wd_group_label(page, first_el) -> str:
    for levels in ["xpath=..", "xpath=../..", "xpath=../../..", "xpath=../../../.."]:
        try:
            parent = first_el.locator(levels)
            if await parent.count() == 0:
                continue
            for sel in ["legend", "label", "[class*='question']", "[class*='label']", "p", "span"]:
                q = parent.locator(sel)
                if await q.count() > 0:
                    text = (await q.first.inner_text()).strip()
                    if len(text) > 5 and text not in ("Yes", "No"):
                        return text
        except Exception:
            continue
    return ""
