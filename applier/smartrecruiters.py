"""
SmartRecruiters ATS applier.
Handles: jobs.smartrecruiters.com
"""
import asyncio
import os
from playwright.async_api import async_playwright
from applier.greenhouse import get_answer
from applier.browser_utils import new_stealth_page, wait_for_captcha_if_present

os.makedirs("screenshots", exist_ok=True)


async def apply_smartrecruiters(job: dict, dry_run: bool = True, user_info: dict = None, profile_text: str = None) -> str:
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
            await asyncio.sleep(2)
            await wait_for_captcha_if_present(page, source="smartrecruiters")

            # Click the Apply button
            apply_btn = page.locator(
                "button:has-text('Apply now'), button:has-text('Apply Now'), "
                "a:has-text('Apply now'), a:has-text('Apply Now'), "
                "button:has-text('Apply'), a:has-text('Apply'), "
                "[data-hook='apply-button']"
            )
            if await apply_btn.count() > 0:
                await apply_btn.first.click()
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                await asyncio.sleep(2)
                await wait_for_captcha_if_present(page, source="smartrecruiters")
                print("    ✓ Clicked Apply")
            else:
                print("    ℹ No Apply button found — form may already be visible")

            await _fill_sr_form(page, info, profile_text)
            print("    ✓ Form filled!")

            if dry_run:
                await page.screenshot(path=f"screenshots/sr_dry_{job.get('id', 'unknown')}.png", full_page=True)
                print(f"    ✓ DRY RUN — screenshot saved")
                return "dry_run"
            else:
                return await _submit_sr(page, job)

        except Exception as e:
            import traceback
            print(f"    ✗ Error: {e}")
            traceback.print_exc()
            try:
                await page.screenshot(path=f"screenshots/sr_error_{job.get('id', 'unknown')}.png")
            except Exception:
                pass
            return "failed"
        finally:
            await asyncio.sleep(2)
            await browser.close()


async def _fill_sr_form(page, info: dict, profile_text: str):
    """Fill SmartRecruiters application fields."""

    # SmartRecruiters uses id="first_name" / "last_name" etc. or data-hook attributes
    std_map = [
        ("#first_name, [data-hook='first-name'], input[name='firstName']", info.get("first_name", "")),
        ("#last_name,  [data-hook='last-name'],  input[name='lastName']",  info.get("last_name", "")),
        ("#email,      [data-hook='email'],       input[name='email']",     info.get("email", "")),
        ("#phone,      [data-hook='phone'],       input[name='phone']",     info.get("phone", "")),
    ]
    for selector, value in std_map:
        if not value:
            continue
        for sel in [s.strip() for s in selector.split(",")]:
            try:
                el = page.locator(sel)
                if await el.count() > 0 and await el.first.is_visible():
                    await el.first.fill(value)
                    print(f"    ✓ Filled {sel}: '{value}'")
                    break
            except Exception:
                continue

    # Resume upload
    resume_path = info.get("resume_path", "")
    if resume_path and os.path.exists(resume_path):
        for fi_sel in ["input[type='file']", "[data-hook='resume-upload'] input[type='file']"]:
            try:
                fi = page.locator(fi_sel)
                if await fi.count() > 0:
                    await fi.first.set_input_files(resume_path)
                    print("    ✓ Resume uploaded")
                    await asyncio.sleep(2)
                    break
            except Exception:
                pass

    # LinkedIn / social
    social = [
        ("input[name='linkedin'], input[placeholder*='inkedIn']", info.get("linkedin", "")),
        ("input[name='website'], input[placeholder*='ebsite']",   info.get("website", "")),
    ]
    for selector, value in social:
        if not value:
            continue
        try:
            el = page.locator(selector)
            if await el.count() > 0 and await el.first.is_visible():
                await el.first.fill(value)
        except Exception:
            pass

    # Custom questions (SmartRecruiters wraps them in sections)
    await _fill_sr_custom_questions(page, profile_text)


async def _fill_sr_custom_questions(page, profile_text: str):
    """Handle SmartRecruiters custom questions."""

    # Textareas
    textareas = await page.locator("textarea:visible").all()
    for el in textareas:
        try:
            label = await _get_sr_label(page, el)
            if not label:
                continue
            print(f"    ? Textarea: {label[:60]}")
            answer = await get_answer(label, "textarea", profile_text=profile_text)
            if answer:
                await el.fill(answer)
                print(f"    ✓ {answer[:60]}")
        except Exception as e:
            print(f"    ✗ Textarea error: {e}")

    # Text inputs (skip standard fields already filled)
    inputs = await page.locator(
        "input[type='text']:visible, input[type='url']:visible, input[type='number']:visible"
    ).all()
    for el in inputs:
        try:
            el_id = await el.get_attribute("id") or ""
            el_name = await el.get_attribute("name") or ""
            if any(s in (el_id + el_name).lower()
                   for s in ("first", "last", "email", "phone", "linkedin", "website")):
                continue
            val = await el.input_value()
            if val:
                continue
            label = await _get_sr_label(page, el)
            if not label:
                continue
            print(f"    ? Input: {label[:60]}")
            answer = await get_answer(label, "text", profile_text=profile_text)
            if answer:
                await el.fill(answer)
                print(f"    ✓ {answer[:60]}")
        except Exception as e:
            print(f"    ✗ Input error: {e}")

    # Select dropdowns
    selects = await page.locator("select:visible").all()
    for el in selects:
        try:
            label = await _get_sr_label(page, el)
            if not label:
                continue
            options_els = await el.locator("option").all()
            options = [await o.inner_text() for o in options_els if await o.get_attribute("value")]
            if not options:
                continue
            print(f"    ? Select: {label[:60]}")
            answer = await get_answer(f"{label}. Options: {options}", "dropdown", profile_text=profile_text)
            if answer:
                try:
                    await el.select_option(label=answer)
                    print(f"    ✓ Selected: {answer}")
                except Exception:
                    for o_el in options_els:
                        text = await o_el.inner_text()
                        if answer.lower() in text.lower():
                            val = await o_el.get_attribute("value")
                            await el.select_option(value=val)
                            break
        except Exception as e:
            print(f"    ✗ Select error: {e}")

    # Radio buttons
    radio_groups: dict[str, list] = {}
    for el in await page.locator("input[type='radio']:visible").all():
        name = await el.get_attribute("name") or ""
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

            question = await _get_sr_group_label(page, radios[0])
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


async def _get_sr_label(page, el) -> str:
    field_id = await el.get_attribute("id")
    if field_id:
        lbl = page.locator(f"label[for='{field_id}']")
        if await lbl.count() > 0:
            return (await lbl.first.inner_text()).strip()
    aria = await el.get_attribute("aria-label")
    if aria:
        return aria.strip()
    placeholder = await el.get_attribute("placeholder")
    if placeholder:
        return placeholder.strip()
    for levels in ["xpath=..", "xpath=../..", "xpath=../../.."]:
        try:
            parent = el.locator(levels)
            if await parent.count() == 0:
                continue
            for sel in ["label", "[class*='label']", "legend"]:
                lbl = parent.locator(sel)
                if await lbl.count() > 0:
                    text = (await lbl.first.inner_text()).strip()
                    if len(text) > 3:
                        return text
        except Exception:
            continue
    return ""


async def _get_sr_group_label(page, first_el) -> str:
    for levels in ["xpath=..", "xpath=../..", "xpath=../../..", "xpath=../../../.."]:
        try:
            parent = first_el.locator(levels)
            if await parent.count() == 0:
                continue
            for sel in ["legend", "label", "[class*='question']", "[class*='label']", "p"]:
                q = parent.locator(sel)
                if await q.count() > 0:
                    text = (await q.first.inner_text()).strip()
                    if len(text) > 5:
                        return text
        except Exception:
            continue
    return ""


async def _submit_sr(page, job: dict) -> str:
    """Submit SmartRecruiters form."""
    submit = page.locator(
        "button[type='submit']:visible, "
        "button:has-text('Send Application'):visible, "
        "button:has-text('Submit Application'):visible, "
        "button:has-text('Submit'):visible, "
        "[data-hook='submit-btn']:visible"
    )
    if await submit.count() == 0:
        print("    ✗ Submit button not found")
        return "failed"

    print("    → Clicking Submit...")
    await submit.first.click()
    await asyncio.sleep(4)

    try:
        await page.screenshot(path=f"screenshots/sr_submit_{job.get('id', 'unknown')}.png")
    except Exception:
        pass

    url_lower = page.url.lower()
    if any(w in url_lower for w in ("thank", "confirm", "success", "submitted")):
        print("    ✓ Submitted — confirmation URL")
        return "applied"

    success = page.locator(
        "h1:has-text('Thank'), h2:has-text('Thank'), "
        "[class*='success'], [class*='confirmation'], [class*='thank-you']"
    )
    if await success.count() > 0:
        print("    ✓ Submitted — confirmation element")
        return "applied"

    errors = await page.locator("[class*='error']:visible").all()
    if errors:
        print(f"    ✗ {len(errors)} error(s) found")
        return "failed"

    print("    ✓ No errors — treating as applied")
    return "applied"
