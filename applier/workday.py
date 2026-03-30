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
from applier.browser_utils import new_stealth_page, wait_for_captcha_if_present

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
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = await new_stealth_page(browser)

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
            await browser.close()


async def _walk_workday_wizard(page, info: dict, profile_text: str, dry_run: bool, job: dict) -> str:
    """Navigate through Workday's multi-step application wizard."""

    for step in range(MAX_STEPS):
        await asyncio.sleep(2)
        print(f"    → Step {step + 1}...")

        # Check if we landed on a confirmation / success page
        url_lower = page.url.lower()
        if any(w in url_lower for w in ("thank", "confirm", "success", "submitted", "complete")):
            print("    ✓ Confirmation URL detected")
            return "applied"

        success_el = page.locator(
            "[data-automation-id='confirmationPage'], "
            "h1:has-text('Thank'), h2:has-text('Thank'), "
            "[class*='confirmation'], [class*='success']"
        )
        if await success_el.count() > 0:
            print("    ✓ Confirmation element found")
            return "applied"

        # Fill whatever fields are visible on this step
        await _fill_workday_step(page, info, profile_text, step)

        # Dry run: screenshot and stop after first step
        if dry_run and step == 0:
            await page.screenshot(path=f"screenshots/workday_dry_{job.get('id', 'unknown')}.png", full_page=True)
            print(f"    ✓ DRY RUN — screenshot saved")
            return "dry_run"

        # Try to click Next / Continue / Submit
        advanced = await _click_next_or_submit(page, step)
        if not advanced:
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
                    await r.evaluate("el => el.click()")
                    print(f"    ✓ Radio: {lbl}")
                    break
        except Exception as e:
            print(f"    ✗ Radio error: {e}")


async def _click_next_or_submit(page, step: int) -> bool:
    """Click the Next or Submit button to advance the Workday wizard."""
    # Prefer "Submit" on later steps
    candidates = [
        "button:has-text('Submit'):visible",
        "button:has-text('Save and Continue'):visible",
        "button:has-text('Next'):visible",
        "button:has-text('Continue'):visible",
        "[data-automation-id='bottom-navigation-next-button']:visible",
        "[data-automation-id='bottom-navigation-save-button']:visible",
        "button[aria-label*='Next']:visible",
    ]
    for sel in candidates:
        try:
            btn = page.locator(sel)
            if await btn.count() > 0:
                print(f"    → Clicking: {sel.split(':')[0]}")
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
