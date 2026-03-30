"""
Generic applier — fallback for any unsupported ATS.
Uses AI-driven label detection to fill whatever form fields it finds.
Handles: BambooHR, iCIMS, Rippling, JazzHR, Jobvite, Pinpoint, and any other job board.
"""
import asyncio
import os
from playwright.async_api import async_playwright
from applier.greenhouse import get_answer
from applier.browser_utils import new_stealth_page, wait_for_captcha_if_present

os.makedirs("screenshots", exist_ok=True)

# Fields we can fill directly from user_info without AI
DIRECT_FIELD_KEYWORDS = {
    "first": "first_name", "firstname": "first_name", "given": "first_name",
    "last": "last_name",  "lastname": "last_name",  "surname": "last_name",
    "fullname": None,       "name": None,
    "email": "email",
    "phone": "phone",       "mobile": "phone",       "telephone": "phone",
    "linkedin": "linkedin", "github": "github",
    "website": "website",   "portfolio": "website",
    "city": "city",         "location": "location",  "address": "address",
    "zip": "zip",           "postal": "zip",
    "state": "state",
}


async def apply_generic(job: dict, dry_run: bool = True, user_info: dict = None, profile_text: str = None) -> str:
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
            await wait_for_captcha_if_present(page, source="generic")

            # Try to find and click an Apply button
            apply_btn = page.locator(
                "a:has-text('Apply now'), button:has-text('Apply now'), "
                "a:has-text('Apply for this job'), button:has-text('Apply for this job'), "
                "a:has-text('Apply'), button:has-text('Apply'), "
                "[class*='apply-btn'], [id*='apply-btn'], [aria-label*='Apply']"
            )
            if await apply_btn.count() > 0:
                try:
                    await apply_btn.first.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    await asyncio.sleep(2)
                    await wait_for_captcha_if_present(page, source="generic")
                    print("    ✓ Clicked Apply")
                except Exception as e:
                    print(f"    ✗ Apply click failed: {e}")
            else:
                print("    ℹ No Apply button — attempting to fill visible form")

            # Check if a form is present at all
            form_count = await page.locator("form").count()
            if form_count == 0:
                print("    ✗ No form found on page")
                return "unsupported"

            await _fill_generic_form(page, info, profile_text)
            print("    ✓ Form filled!")

            if dry_run:
                await page.screenshot(path=f"screenshots/generic_dry_{job.get('id', 'unknown')}.png", full_page=True)
                print(f"    ✓ DRY RUN — screenshot saved")
                return "dry_run"
            else:
                return await _submit_generic(page, job)

        except Exception as e:
            import traceback
            print(f"    ✗ Error: {e}")
            traceback.print_exc()
            try:
                await page.screenshot(path=f"screenshots/generic_error_{job.get('id', 'unknown')}.png")
            except Exception:
                pass
            return "failed"
        finally:
            await asyncio.sleep(2)
            await browser.close()


async def _fill_generic_form(page, info: dict, profile_text: str):
    """Intelligently fill any form using label detection + AI answers."""

    full_name = f"{info.get('first_name', '')} {info.get('last_name', '')}".strip()

    # --- Resume upload first (so any resume-parsing fills don't overwrite our data) ---
    resume_path = info.get("resume_path", "")
    if resume_path and os.path.exists(resume_path):
        file_inputs = await page.locator("input[type='file']").all()
        for fi in file_inputs:
            try:
                await fi.set_input_files(resume_path)
                print("    ✓ Resume uploaded")
                await asyncio.sleep(2)
                break
            except Exception:
                pass

    # --- All visible inputs ---
    inputs = await page.locator(
        "input[type='text']:visible, input[type='email']:visible, "
        "input[type='tel']:visible, input[type='url']:visible, "
        "input[type='number']:visible"
    ).all()

    for el in inputs:
        try:
            # Skip already-filled
            val = await el.input_value()
            if val:
                continue

            label = await _get_generic_label(page, el)
            label_lower = label.lower().replace(" ", "").replace("_", "").replace("-", "")

            # Try direct mapping first
            direct_val = _direct_value(label_lower, info, full_name)
            if direct_val is not None:
                await el.fill(direct_val)
                if direct_val:
                    print(f"    ✓ Filled '{label}': '{direct_val}'")
                continue

            if not label:
                continue

            # Use AI for anything we don't recognize
            input_type = await el.get_attribute("type") or "text"
            field_type = "text"
            print(f"    ? Input [{input_type}]: {label[:60]}")
            answer = await get_answer(label, field_type, profile_text=profile_text)
            if answer:
                await el.fill(answer)
                print(f"    ✓ {answer[:60]}")
        except Exception as e:
            print(f"    ✗ Input error: {e}")

    # --- Textareas ---
    textareas = await page.locator("textarea:visible").all()
    for el in textareas:
        try:
            val = await el.input_value()
            if val:
                continue
            label = await _get_generic_label(page, el)
            if not label:
                continue
            print(f"    ? Textarea: {label[:60]}")
            answer = await get_answer(label, "textarea", profile_text=profile_text)
            if answer:
                await el.fill(answer)
                print(f"    ✓ {answer[:60]}")
        except Exception as e:
            print(f"    ✗ Textarea error: {e}")

    # --- <select> dropdowns ---
    selects = await page.locator("select:visible").all()
    for el in selects:
        try:
            label = await _get_generic_label(page, el)
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
                            print(f"    ✓ Selected (fuzzy): {text}")
                            break
        except Exception as e:
            print(f"    ✗ Select error: {e}")

    # --- Radio groups ---
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

            question = await _get_generic_group_label(page, radios[0])
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

    # --- Checkbox groups ---
    checkbox_groups: dict[str, list] = {}
    for el in await page.locator("input[type='checkbox']:visible").all():
        name = await el.get_attribute("name") or ""
        checkbox_groups.setdefault(name, []).append(el)

    for group_name, boxes in checkbox_groups.items():
        try:
            options = []
            for cb in boxes:
                cb_id = await cb.get_attribute("id")
                lbl = ""
                if cb_id:
                    lbl_el = page.locator(f"label[for='{cb_id}']")
                    if await lbl_el.count() > 0:
                        lbl = (await lbl_el.first.inner_text()).strip()
                options.append((cb, lbl or await cb.get_attribute("value") or ""))

            # Skip simple "I agree" / consent checkboxes — always check them
            if len(options) == 1:
                lbl_lower = options[0][1].lower()
                if any(w in lbl_lower for w in ("agree", "consent", "confirm", "acknowledge", "accept", "terms", "privacy")):
                    is_checked = await options[0][0].is_checked()
                    if not is_checked:
                        await options[0][0].evaluate("el => el.click()")
                        print(f"    ✓ Checked consent: {options[0][1][:50]}")
                continue

            question = await _get_generic_group_label(page, boxes[0])
            if not question:
                continue
            option_labels = [o[1] for o in options]
            print(f"    ? Checkbox: {question[:60]} → {option_labels}")
            answer = await get_answer(
                f"{question}. Select applicable: {option_labels}. Reply comma-separated.",
                "checkbox", profile_text=profile_text
            )
            if not answer:
                continue
            answer_lower = answer.lower()
            for cb, lbl in options:
                if lbl.lower() in answer_lower or any(
                    w in lbl.lower() for w in answer_lower.split(",") if len(w.strip()) > 2
                ):
                    await cb.evaluate("el => el.click()")
                    print(f"    ✓ Checked: {lbl}")
        except Exception as e:
            print(f"    ✗ Checkbox error: {e}")


def _direct_value(label_lower: str, info: dict, full_name: str):
    """Return a direct value for known field types without calling AI."""
    for keyword, key in DIRECT_FIELD_KEYWORDS.items():
        if keyword in label_lower:
            if key is None:
                # "name" or "fullname"
                return full_name
            return info.get(key, "")
    return None  # None means "not recognized, use AI"


async def _get_generic_label(page, el) -> str:
    """Universal label detection strategy."""
    # 1. label[for=id]
    field_id = await el.get_attribute("id")
    if field_id:
        lbl = page.locator(f"label[for='{field_id}']")
        if await lbl.count() > 0:
            return (await lbl.first.inner_text()).strip()
    # 2. aria-labelledby
    labelledby = await el.get_attribute("aria-labelledby")
    if labelledby:
        for lid in labelledby.split():
            ref = page.locator(f"#{lid}")
            if await ref.count() > 0:
                text = (await ref.first.inner_text()).strip()
                if text:
                    return text
    # 3. aria-label
    aria = await el.get_attribute("aria-label")
    if aria:
        return aria.strip()
    # 4. placeholder
    placeholder = await el.get_attribute("placeholder")
    if placeholder:
        return placeholder.strip()
    # 5. name attribute (human-readable)
    name = await el.get_attribute("name")
    if name and len(name) > 2:
        import re
        return re.sub(r'([A-Z])', r' \1', name).replace("_", " ").replace("-", " ").strip().lower()
    # 6. Walk up DOM
    for levels in ["xpath=..", "xpath=../..", "xpath=../../.."]:
        try:
            parent = el.locator(levels)
            if await parent.count() == 0:
                continue
            for sel in ["label", "legend", "[class*='label']", "[class*='Label']"]:
                lbl = parent.locator(sel)
                if await lbl.count() > 0:
                    text = (await lbl.first.inner_text()).strip()
                    if len(text) > 2:
                        return text
        except Exception:
            continue
    return ""


async def _get_generic_group_label(page, first_el) -> str:
    for levels in ["xpath=..", "xpath=../..", "xpath=../../..", "xpath=../../../.."]:
        try:
            parent = first_el.locator(levels)
            if await parent.count() == 0:
                continue
            for sel in ["legend", "label", "[class*='question']", "[class*='label']", "p", "span", "h3", "h4"]:
                q = parent.locator(sel)
                if await q.count() > 0:
                    text = (await q.first.inner_text()).strip()
                    if len(text) > 5 and text not in ("Yes", "No"):
                        return text
        except Exception:
            continue
    return ""


async def _submit_generic(page, job: dict) -> str:
    """Generic submit — tries common submit button patterns."""
    submit = page.locator(
        "button[type='submit']:visible, "
        "input[type='submit']:visible, "
        "button:has-text('Submit'):visible, "
        "button:has-text('Apply'):visible, "
        "button:has-text('Send'):visible, "
        "button:has-text('Continue'):visible"
    )
    if await submit.count() == 0:
        print("    ✗ Submit button not found")
        return "failed"

    print("    → Clicking Submit...")
    try:
        await page.evaluate("""() => {
            const btn = document.querySelector('button[type="submit"]') ||
                        document.querySelector('input[type="submit"]');
            if (btn) btn.click();
        }""")
    except Exception:
        await submit.first.click()

    await asyncio.sleep(4)

    try:
        await page.screenshot(path=f"screenshots/generic_submit_{job.get('id', 'unknown')}.png")
    except Exception:
        pass

    url_lower = page.url.lower()
    if any(w in url_lower for w in ("thank", "confirm", "success", "submitted", "complete")):
        print("    ✓ Submitted — confirmation URL")
        return "applied"

    success = page.locator(
        "h1:has-text('Thank'), h2:has-text('Thank'), "
        "h1:has-text('received'), h2:has-text('received'), "
        "[class*='success'], [class*='confirmation'], [class*='thank']"
    )
    if await success.count() > 0:
        print("    ✓ Submitted — confirmation element")
        return "applied"

    errors = await page.locator("[class*='error']:visible, .error:visible").all()
    if errors:
        print(f"    ✗ {len(errors)} error(s) found")
        return "failed"

    print("    ✓ No errors — treating as applied")
    return "applied"
