"""
Ashby ATS applier.
Handles: jobs.ashbyhq.com, ashby.io
"""
import asyncio
import os
from playwright.async_api import async_playwright
from applier.greenhouse import get_answer
from applier.browser_utils import new_stealth_page, wait_for_captcha_if_present

os.makedirs("screenshots", exist_ok=True)


async def apply_ashby(job: dict, dry_run: bool = True, user_info: dict = None, profile_text: str = None) -> str:
    if not user_info or not profile_text:
        raise ValueError("user_info and profile_text are required")

    info = user_info
    print(f"\n  Applying to: {job['title']} @ {job['company']}")
    print(f"  URL: {job['url']}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = await new_stealth_page(browser)

        try:
            # Ashby application page is always at /application suffix
            url = job["url"].rstrip("/")
            if "/application" not in url:
                url = url + "/application"
            print(f"    → Loading: {url}")
            await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            await asyncio.sleep(2)
            await wait_for_captcha_if_present(page, source="ashby")

            await _fill_ashby_form(page, info, profile_text)
            print("    ✓ Form filled!")

            if dry_run:
                await page.screenshot(path=f"screenshots/ashby_dry_{job.get('id', 'unknown')}.png", full_page=True)
                print(f"    ✓ DRY RUN — screenshot saved")
                return "dry_run"
            else:
                return await _submit_ashby(page, job)

        except Exception as e:
            import traceback
            print(f"    ✗ Error: {e}")
            traceback.print_exc()
            try:
                await page.screenshot(path=f"screenshots/ashby_error_{job.get('id', 'unknown')}.png")
            except Exception:
                pass
            return "failed"
        finally:
            await asyncio.sleep(2)
            await browser.close()


async def _fill_ashby_form(page, info: dict, profile_text: str):
    """Fill standard Ashby application form fields."""

    # --- Standard text fields by common Ashby selectors ---
    std_fields = [
        ("#input-firstName",  info.get("first_name", "")),
        ("#input-lastName",   info.get("last_name", "")),
        ("#input-email",      info.get("email", "")),
        ("#input-phone",      info.get("phone", "")),
        # Ashby also uses these name attributes
        ("input[name='firstName']",  info.get("first_name", "")),
        ("input[name='lastName']",   info.get("last_name", "")),
        ("input[name='email']",      info.get("email", "")),
        ("input[name='phoneNumber']", info.get("phone", "")),
        ("input[name='phone']",      info.get("phone", "")),
    ]
    seen = set()
    for selector, value in std_fields:
        if not value or selector in seen:
            continue
        try:
            el = page.locator(selector)
            if await el.count() > 0 and await el.first.is_visible():
                await el.first.fill(value)
                print(f"    ✓ Filled {selector.split('[')[0]}: '{value}'")
                seen.add(selector)
        except Exception:
            pass

    # --- Location ---
    location = info.get("location", "")
    for loc_sel in ["#input-location", "input[name='location']", "input[placeholder*='ocation']"]:
        try:
            el = page.locator(loc_sel)
            if await el.count() > 0 and await el.first.is_visible():
                await el.first.fill(location)
                await asyncio.sleep(0.8)
                # Accept first autocomplete suggestion if present
                suggestion = page.locator("[class*='suggestion']:first-child, [class*='option']:first-child, li:first-child")
                if await suggestion.count() > 0:
                    await suggestion.first.click()
                    await asyncio.sleep(0.5)
                print(f"    ✓ Filled location: '{location}'")
                break
        except Exception:
            pass

    # --- LinkedIn / Portfolio / Website ---
    social_fields = [
        ("input[name='linkedIn'], input[name='linkedin'], input[placeholder*='inkedIn']", info.get("linkedin", "")),
        ("input[name='github'], input[placeholder*='ithub']", info.get("github", "")),
        ("input[name='website'], input[name='portfolioUrl'], input[placeholder*='ortfolio']", info.get("website", "")),
    ]
    for selector, value in social_fields:
        if not value:
            continue
        try:
            el = page.locator(selector)
            if await el.count() > 0 and await el.first.is_visible():
                await el.first.fill(value)
        except Exception:
            pass

    # --- Resume upload ---
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
    else:
        print("    ✗ Resume file not found")

    # --- Custom / additional questions ---
    await _fill_ashby_custom_questions(page, profile_text)


async def _fill_ashby_custom_questions(page, profile_text: str):
    """Handle Ashby's custom form questions (labeled inputs, textareas, selects)."""

    # Text inputs and textareas not yet filled (exclude already-filled std fields)
    std_ids = {"input-firstName", "input-lastName", "input-email", "input-phone", "input-location"}

    # Textareas
    textareas = await page.locator("textarea:visible").all()
    for el in textareas:
        try:
            label = await _get_ashby_label(page, el)
            if not label:
                continue
            print(f"    ? Textarea: {label[:60]}")
            answer = await get_answer(label, "textarea", profile_text=profile_text)
            if answer:
                await el.fill(answer)
                print(f"    ✓ {answer[:60]}")
        except Exception as e:
            print(f"    ✗ Textarea error: {e}")

    # Text inputs (skip already-filled standard fields)
    inputs = await page.locator("input[type='text']:visible, input[type='url']:visible").all()
    for el in inputs:
        try:
            el_id = await el.get_attribute("id") or ""
            el_name = await el.get_attribute("name") or ""
            # Skip already-filled standard fields
            if any(s in el_id.lower() or s in el_name.lower()
                   for s in ("first", "last", "email", "phone", "location", "linkedin", "github", "website", "portfolio")):
                continue
            val = await el.input_value()
            if val:  # skip already-filled
                continue
            label = await _get_ashby_label(page, el)
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
            label = await _get_ashby_label(page, el)
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

    # Checkboxes / radio groups (Ashby uses div-based custom selects)
    await _fill_ashby_button_groups(page, profile_text)


async def _fill_ashby_button_groups(page, profile_text: str):
    """Handle Ashby's button-group style questions (styled radio/checkbox buttons)."""
    # Ashby often renders questions as button groups with role="group"
    groups = await page.locator("[role='group']:visible, [class*='buttonGroup']:visible, [class*='RadioGroup']:visible").all()
    for group in groups:
        try:
            # Get the question label (usually a sibling or parent label)
            question = ""
            for q_sel in ["legend", "label", "[class*='label']", "[class*='question']", "p", "span"]:
                q_el = group.locator(q_sel)
                if await q_el.count() > 0:
                    text = (await q_el.first.inner_text()).strip()
                    if len(text) > 5:
                        question = text
                        break
            if not question:
                continue

            buttons = await group.locator("button:visible, [role='radio']:visible, [role='checkbox']:visible").all()
            if not buttons:
                continue

            option_texts = []
            for b in buttons:
                txt = (await b.inner_text()).strip()
                if txt:
                    option_texts.append(txt)

            if not option_texts:
                continue

            print(f"    ? Button group: {question[:60]} → {option_texts}")
            answer = await get_answer(f"{question}. Options: {option_texts}", "radio", profile_text=profile_text)
            if not answer:
                continue

            clicked = False
            for b, txt in zip(buttons, option_texts):
                if answer.lower() in txt.lower() or txt.lower() in answer.lower():
                    await b.click()
                    print(f"    ✓ Selected: {txt}")
                    clicked = True
                    break
            if not clicked and buttons:
                # fuzzy word match
                for b, txt in zip(buttons, option_texts):
                    if any(w in txt.lower() for w in answer.lower().split() if len(w) > 2):
                        await b.click()
                        print(f"    ✓ Selected (fuzzy): {txt}")
                        break
        except Exception as e:
            print(f"    ✗ Button group error: {e}")


async def _get_ashby_label(page, el) -> str:
    """Find the label for an Ashby form field."""
    # 1. label[for=id]
    field_id = await el.get_attribute("id")
    if field_id:
        lbl = page.locator(f"label[for='{field_id}']")
        if await lbl.count() > 0:
            return (await lbl.first.inner_text()).strip()

    # 2. aria-label
    aria = await el.get_attribute("aria-label")
    if aria:
        return aria.strip()

    # 3. aria-labelledby
    labelledby = await el.get_attribute("aria-labelledby")
    if labelledby:
        ref = page.locator(f"#{labelledby}")
        if await ref.count() > 0:
            return (await ref.first.inner_text()).strip()

    # 4. placeholder
    placeholder = await el.get_attribute("placeholder")
    if placeholder:
        return placeholder.strip()

    # 5. Walk up DOM
    for levels in ["xpath=..", "xpath=../..", "xpath=../../.."]:
        try:
            parent = el.locator(levels)
            if await parent.count() == 0:
                continue
            for sel in ["label", "[class*='label']", "[class*='Label']", "legend", "p"]:
                lbl = parent.locator(sel)
                if await lbl.count() > 0:
                    text = (await lbl.first.inner_text()).strip()
                    if len(text) > 3:
                        return text
        except Exception:
            continue
    return ""


async def _submit_ashby(page, job: dict) -> str:
    """Submit the Ashby form and detect success."""
    submit = page.locator(
        "button[type='submit']:visible, "
        "button:has-text('Submit application'):visible, "
        "button:has-text('Submit Application'):visible, "
        "button:has-text('Submit'):visible"
    )
    if await submit.count() == 0:
        print("    ✗ Submit button not found")
        return "failed"

    print("    → Clicking Submit...")
    await submit.first.click()
    await asyncio.sleep(4)

    try:
        await page.screenshot(path=f"screenshots/ashby_submit_{job.get('id', 'unknown')}.png")
    except Exception:
        pass

    # Check URL
    url_lower = page.url.lower()
    if any(w in url_lower for w in ("thank", "confirm", "success", "submitted")):
        print("    ✓ Application submitted — confirmation URL")
        return "applied"

    # Check page text
    success = page.locator(
        "h1:has-text('Thank'), h2:has-text('Thank'), "
        "h1:has-text('received'), h2:has-text('received'), "
        "[class*='success'], [class*='confirmation'], [class*='thank']"
    )
    if await success.count() > 0:
        print("    ✓ Application submitted — confirmation element")
        return "applied"

    # Check for errors
    errors = await page.locator("[class*='error']:visible, [class*='Error']:visible").all()
    if errors:
        print(f"    ✗ {len(errors)} validation error(s)")
        return "failed"

    print("    ✓ No errors detected — treating as applied")
    return "applied"
