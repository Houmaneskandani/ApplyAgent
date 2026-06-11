import asyncio
import os
import anthropic
from playwright.async_api import async_playwright
from config import ANTHROPIC_API_KEY
from applier.greenhouse import get_answer
from applier.browser_utils import stealth_session, wait_for_captcha_if_present, trusted_click

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

os.makedirs("screenshots", exist_ok=True)


async def apply_lever(job: dict, dry_run: bool = True, user_info: dict = None, profile_text: str = None):
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
                await page.goto(job["url"], timeout=60000, wait_until="domcontentloaded")
                await asyncio.sleep(2)
                await wait_for_captcha_if_present(page, source="lever")

                # Try clicking the Apply button first
                apply_btn = page.locator(
                    "a:has-text('Apply for this job'), "
                    "button:has-text('Apply for this job'), "
                    "a:has-text('Apply now'), "
                    "button:has-text('Apply now'), "
                    "a:has-text('Apply'), "
                    "button:has-text('Apply')"
                )

                clicked = False
                if await apply_btn.count() > 0:
                    try:
                        await apply_btn.first.click(timeout=5000)
                        await page.wait_for_load_state("domcontentloaded", timeout=10000)
                        await asyncio.sleep(2)
                        await wait_for_captcha_if_present(page, source="lever")
                        print("    ✓ Clicked Apply button")
                        clicked = True
                    except Exception as e:
                        print(f"    ✗ Apply button click failed: {e}")

                # Fallback: navigate directly to /apply URL
                if not clicked:
                    apply_url = job["url"].rstrip("/") + "/apply"
                    print(f"    → Navigating directly to apply URL: {apply_url}")
                    await page.goto(apply_url, timeout=60000, wait_until="domcontentloaded")
                    await asyncio.sleep(2)

                # Verify we're on the form page
                current_url = page.url
                print(f"    ℹ Current URL: {current_url[:80]}")

                # CLOSED-JOB DETECTION: Lever returns a real apply URL even
                # when the role was filled/closed, just with a "Not found"
                # page. We caught this on Plaid Staff SWE — the bot spent
                # ~30s "filling" zero fields then failed with the cryptic
                # "Submit button not found". Bail early with a clear
                # explanation instead.
                try:
                    page_title = (await page.title()) or ""
                    if "not found" in page_title.lower() or "404" in page_title:
                        print(f"    ✗ Job posting appears closed (page title: {page_title!r})")
                        try:
                            os.makedirs("screenshots", exist_ok=True)
                            await page.screenshot(
                                path=f"screenshots/lever_closed_{job.get('id', 'unknown')}.png"
                            )
                        except Exception:
                            pass
                        if info is not None:
                            info["_reviewer_notes"] = (
                                f"Lever job posting is closed (page title: {page_title}). "
                                f"Mark this row as 'failed' and skip future retries."
                            )
                        return "failed"
                except Exception:
                    pass  # title-check is defensive — never block the apply

                # Solve any CAPTCHA on the apply form itself (hCaptcha blocks clicks)
                await asyncio.sleep(2)
                await wait_for_captcha_if_present(page, source="lever")

                # `fill_lever_form` now returns the number of fields it
                # actually filled. If we got zero, the page is in some
                # state we don't recognize (custom Lever theme, login
                # gate, etc.) — bail with a clear reason instead of
                # the misleading "Submit not found" we used to emit.
                filled_count = await fill_lever_form(
                    page, user_info=info, profile_text=profile_text,
                )
                if filled_count == 0:
                    print(f"    ✗ Form filled 0 fields — page may be using a custom Lever "
                          f"layout or behind a login gate. URL: {current_url}")
                    try:
                        os.makedirs("screenshots", exist_ok=True)
                        await page.screenshot(
                            path=f"screenshots/lever_no_fields_{job.get('id', 'unknown')}.png"
                        )
                    except Exception:
                        pass
                    if info is not None:
                        info["_reviewer_notes"] = (
                            "Lever page returned but no standard fields were detected. "
                            "Likely a custom theme, login wall, or closed job."
                        )
                    return "failed"

                print(f"    ✓ Form filled! ({filled_count} fields)")

                if dry_run:
                    os.makedirs("screenshots", exist_ok=True)
                    screenshot_path = f"screenshots/dry_run_{job.get('id', 'unknown')}.png"
                    await page.screenshot(path=screenshot_path, full_page=True)
                    print(f"    ✓ DRY RUN — screenshot saved to {screenshot_path}")
                else:
                    result = await submit_lever(
                        page,
                        user_info=user_info,
                        profile_text=profile_text,
                        job=job,
                    )
                    return result

            except Exception as e:
                import traceback
                print(f"    ✗ Error: {e}")
                traceback.print_exc()
                try:
                    await page.screenshot(path=f"screenshots/lever_error_{job.get('id', 'unknown')}.png")
                    print(f"    → Screenshot: screenshots/lever_error_{job.get('id', 'unknown')}.png")
                except Exception:
                    pass
                return "failed"
            finally:
                await asyncio.sleep(3)  # pause so you can see the final state
                # cleanup handled by stealth_session

    return "dry_run"


async def fill_lever_form(page, user_info: dict = None, profile_text: str = None) -> int:
    """
    Fill standard Lever application fields. Returns the number of fields
    that were successfully filled — used by the caller to detect "page
    looked OK but nothing matched our selectors" (closed jobs, custom
    Lever themes, login walls, etc.) so we can surface a clear failure
    reason instead of the previous mysterious "Submit not found".
    """
    info = user_info or {}
    filled = 0  # count of fields we successfully wrote a value into

    full_name = f"{info.get('first_name', '')} {info.get('last_name', '')}".strip()

    fields = {
        "name": full_name,
        "email": info.get("email", ""),
        "phone": info.get("phone", ""),
        "location": info.get("location", ""),
        "org": info.get("org", ""),
        "urls[LinkedIn]": info.get("linkedin", ""),
        "urls[GitHub]": info.get("github", ""),
        "urls[Portfolio]": info.get("website", ""),
    }

    for field_name, value in fields.items():
        if not value:
            continue
        try:
            el = page.locator(f"input[name='{field_name}']")
            if await el.count() > 0:
                await el.first.fill(value)
                print(f"    ✓ Filled {field_name}")
                filled += 1
        except Exception as e:
            print(f"    ✗ Could not fill {field_name}: {e}")

    # Resume upload
    resume_input = page.locator("input[type='file']")
    if await resume_input.count() > 0:
        resume_path = info.get("resume_path", "")
        if resume_path and os.path.exists(resume_path):
            await resume_input.first.set_input_files(resume_path)
            print("    ✓ Resume uploaded")
            filled += 1
            await asyncio.sleep(2)
        else:
            print("    ✗ Resume file not found")

    # Cover letter textarea
    cover = page.locator("textarea[name='comments'], textarea[name='cover'], textarea[name='coverLetter']")
    if await cover.count() > 0:
        cl_text = await get_answer("Write a brief cover letter for this position", "textarea", profile_text=profile_text)
        if cl_text:
            await cover.first.fill(cl_text)
            print("    ✓ Cover letter filled")
            filled += 1

    # Custom questions (these add to `filled` opaquely — we don't track them
    # individually, but presence of cards/customs implies a real Lever form.
    # The standard-field count above is enough signal for closed-job
    # detection.)
    await fill_lever_custom_questions(page, profile_text=profile_text)

    return filled


async def fill_lever_custom_questions(page, profile_text: str = None):
    """Handle Lever's custom application questions."""

    # --- 1. TEXT / TEXTAREA fields (exclude radio/checkbox) ---
    inputs = await page.locator(
        "input[name*='cards']:not([type='file']):not([type='hidden'])"
        ":not([type='radio']):not([type='checkbox']), "
        "input[name*='custom']:not([type='file']):not([type='hidden'])"
        ":not([type='radio']):not([type='checkbox'])"
    ).all()
    textareas = await page.locator(
        "textarea[name*='cards'], textarea[name*='custom'], textarea[data-qa]"
    ).all()

    for el in inputs + textareas:
        try:
            if not await el.is_visible():
                continue
            label_text = await _get_lever_label(page, el)
            if not label_text:
                continue
            tag = await el.evaluate("el => el.tagName.toLowerCase()")
            field_type = "textarea" if tag == "textarea" else "text"
            print(f"    ? {label_text[:60]}")
            answer = await get_answer(label_text, field_type, profile_text=profile_text)
            if answer:
                await el.fill(answer)
                print(f"    ✓ {answer[:50]}")
        except Exception as e:
            print(f"    ✗ Text field error: {e}")

    # --- 2. SELECT dropdowns ---
    selects = await page.locator(
        "select[name*='cards'], select[name*='custom'], select[data-qa]"
    ).all()
    for el in selects:
        try:
            if not await el.is_visible():
                continue
            label_text = await _get_lever_label(page, el)
            if not label_text:
                continue
            print(f"    ? Select: {label_text[:60]}")
            options = await el.locator("option").all()
            option_texts = [await o.inner_text() for o in options if await o.get_attribute("value")]
            answer = await get_answer(
                f"{label_text}. Options: {option_texts}", "dropdown", profile_text=profile_text
            )
            if answer:
                try:
                    await el.select_option(label=answer)
                    print(f"    ✓ Selected: {answer}")
                except Exception:
                    for opt in options:
                        text = await opt.inner_text()
                        if answer.lower() in text.lower():
                            val = await opt.get_attribute("value")
                            await el.select_option(value=val)
                            print(f"    ✓ Selected (fuzzy): {text}")
                            break
        except Exception as e:
            print(f"    ✗ Select error: {e}")

    # --- 3. RADIO button groups ---
    radio_inputs = await page.locator(
        "input[type='radio'][name*='cards'], input[type='radio'][name*='custom']"
    ).all()

    radio_groups: dict[str, list] = {}
    for el in radio_inputs:
        name = await el.get_attribute("name") or ""
        if name not in radio_groups:
            radio_groups[name] = []
        radio_groups[name].append(el)

    for group_name, radios in radio_groups.items():
        try:
            # Gather option values/labels
            options = []
            for r in radios:
                val = await r.get_attribute("value") or ""
                # Try to get a label next to the radio
                r_id = await r.get_attribute("id")
                lbl = ""
                if r_id:
                    lbl_el = page.locator(f"label[for='{r_id}']")
                    if await lbl_el.count() > 0:
                        lbl = (await lbl_el.first.inner_text()).strip()
                options.append((r, val, lbl or val))

            # Find the parent question label (walk up from first radio)
            question = await _get_lever_group_label(page, radios[0], group_name)
            if not question:
                continue

            option_labels = [o[2] for o in options]
            print(f"    ? Radio: {question[:60]} → {option_labels}")
            answer = await get_answer(
                f"{question}. Choose one: {option_labels}", "radio", profile_text=profile_text
            )
            if not answer:
                continue

            clicked = False
            for radio, val, lbl in options:
                if answer.lower() in lbl.lower() or lbl.lower() in answer.lower():
                    # Trusted click — Lever's hCaptcha integration rejects synthetic
                    # (isTrusted=false) events, which is what the old JS click produced.
                    await trusted_click(radio)
                    print(f"    ✓ Radio: {lbl}")
                    clicked = True
                    break
            if not clicked:
                # fuzzy fallback: pick first matching word
                for radio, val, lbl in options:
                    if any(w in lbl.lower() for w in answer.lower().split()):
                        await trusted_click(radio)
                        print(f"    ✓ Radio (fuzzy): {lbl}")
                        break
        except Exception as e:
            print(f"    ✗ Radio group error: {e}")

    # --- 4. CHECKBOX groups ---
    checkbox_inputs = await page.locator(
        "input[type='checkbox'][name*='cards'], input[type='checkbox'][name*='custom']"
    ).all()

    checkbox_groups: dict[str, list] = {}
    for el in checkbox_inputs:
        name = await el.get_attribute("name") or ""
        if name not in checkbox_groups:
            checkbox_groups[name] = []
        checkbox_groups[name].append(el)

    for group_name, boxes in checkbox_groups.items():
        try:
            options = []
            for cb in boxes:
                val = await cb.get_attribute("value") or ""
                cb_id = await cb.get_attribute("id")
                lbl = ""
                if cb_id:
                    lbl_el = page.locator(f"label[for='{cb_id}']")
                    if await lbl_el.count() > 0:
                        lbl = (await lbl_el.first.inner_text()).strip()
                options.append((cb, val, lbl or val))

            question = await _get_lever_group_label(page, boxes[0], group_name)
            if not question:
                continue

            option_labels = [o[2] for o in options]
            print(f"    ? Checkbox: {question[:60]} → {option_labels}")
            answer = await get_answer(
                f"{question}. Select all that apply from: {option_labels}. "
                "Reply with comma-separated values.", "checkbox", profile_text=profile_text
            )
            if not answer:
                continue

            answer_lower = answer.lower()
            checked = 0
            for cb, val, lbl in options:
                if lbl.lower() in answer_lower or any(
                    w in lbl.lower() for w in answer_lower.split(",")
                    if len(w.strip()) > 3
                ):
                    await trusted_click(cb)
                    print(f"    ✓ Checked: {lbl}")
                    checked += 1
            if checked == 0 and options:
                await trusted_click(options[0][0])
                print(f"    ✓ Checked (fallback): {options[0][2]}")
        except Exception as e:
            print(f"    ✗ Checkbox group error: {e}")


async def _get_lever_group_label(page, first_el, group_name: str) -> str:
    """Find the question label for a radio/checkbox group by walking up the DOM."""
    # Walk up up to 6 levels looking for a label/legend/div with question text
    for levels in ["xpath=..", "xpath=../..", "xpath=../../..", "xpath=../../../..",
                   "xpath=../../../../..", "xpath=../../../../../.."]:
        try:
            parent = first_el.locator(levels)
            if await parent.count() == 0:
                continue
            # Try legend first (used in fieldsets)
            legend = parent.locator("legend")
            if await legend.count() > 0:
                text = (await legend.first.inner_text()).strip()
                if text and len(text) > 3:
                    return text
            # Try any label that is NOT a sibling of an input (i.e. the group label)
            labels = await parent.locator("label").all()
            for lbl in labels:
                text = (await lbl.inner_text()).strip()
                # Skip if it's just an option label (short Yes/No type)
                if text and len(text) > 5 and text not in ("Yes", "No", "True", "False"):
                    return text
            # Try a div/p with class containing "label" or "question"
            q = parent.locator("[class*='question-label'], [class*='application-label'], [class*='field-label']")
            if await q.count() > 0:
                text = (await q.first.inner_text()).strip()
                if text:
                    return text
        except Exception:
            continue
    return ""


async def _get_lever_label(page, el) -> str:
    """Try several strategies to find a label for a Lever form field."""
    # Strategy 1: label[for=id]
    field_id = await el.get_attribute("id")
    if field_id:
        label_el = page.locator(f"label[for='{field_id}']")
        if await label_el.count() > 0:
            return (await label_el.first.inner_text()).strip()

    # Strategy 2: aria-label
    aria = await el.get_attribute("aria-label")
    if aria:
        return aria.strip()

    # Strategy 3: placeholder
    placeholder = await el.get_attribute("placeholder")
    if placeholder:
        return placeholder.strip()

    # Strategy 4: walk up to parent and find label
    for levels in ["xpath=..", "xpath=../..", "xpath=../../.."]:
        try:
            parent = el.locator(levels)
            if await parent.count() > 0:
                label = parent.locator("label")
                if await label.count() > 0:
                    text = (await label.first.inner_text()).strip()
                    if text:
                        return text
        except Exception:
            continue

    return ""


async def submit_lever(
    page,
    max_retries: int = 3,
    user_info: dict | None = None,
    profile_text: str | None = None,
    job: dict | None = None,
) -> str:
    """Submit Lever form with error retry.

    Runs the pre-submit reviewer agent ONCE (before the first attempt),
    same pattern as Greenhouse. If the reviewer blocks, the applier
    returns "unknown" and the issues land in the user's Needs Review tab.
    """
    from applier.reviewer import run_pre_submit_review

    # Pre-submit audit — only on the first attempt. Retries are for
    # transient submit failures (hCaptcha glitch, network), not for
    # re-auditing the same form.
    blocked = await run_pre_submit_review(
        page,
        user_info=user_info,
        profile_text=profile_text,
        company=(job or {}).get("company", ""),
        job_title=(job or {}).get("title", ""),
        screenshot_prefix="lever_reviewer_blocked",
    )
    if blocked:
        return "unknown"

    for attempt in range(max_retries):
        submit = page.locator(
            "button[type='submit']:has-text('Submit'), "
            "button:has-text('Submit application'), "
            "button:has-text('Submit Application'), "
            "button[data-qa='btn-submit']"
        )
        if await submit.count() == 0:
            print("    ✗ Submit button not found")
            return "failed"

        # hCaptcha only accepts a submission from a TRUSTED (isTrusted=true)
        # event. A JS .click() is isTrusted=false and gets silently rejected —
        # the likely cause of Lever "unknown" outcomes. So try a real Playwright
        # click FIRST; only fall back to the JS click if the overlay iframe
        # actually intercepts the pointer (the original reason JS-click existed).
        clicked = await trusted_click(submit.first)
        if not clicked:
            print("    ⚠ Trusted click intercepted — falling back to JS click")
            try:
                await page.evaluate("""() => {
                    const btn = document.querySelector('[data-qa="btn-submit"]') ||
                                document.querySelector('#btn-submit') ||
                                document.querySelector('button[type="submit"]');
                    if (btn) btn.click();
                }""")
            except Exception:
                pass
        await asyncio.sleep(3)

        success = page.locator(
            "h1:has-text('Thank'), h2:has-text('Thank'), "
            ".thank-you, [class*='confirmation'], [class*='success']"
        )
        if await success.count() > 0:
            print("    ✓ Submitted successfully!")
            return "applied"

        if "thank" in page.url.lower() or "confirmation" in page.url.lower():
            print("    ✓ Submitted — confirmation URL detected")
            return "applied"

        errors = await page.locator(".error, [class*='error-message'], [class*='field-error']").all()
        if errors:
            print(f"    ⚠ Attempt {attempt + 1} — {len(errors)} error(s) found, retrying...")
            await asyncio.sleep(1)
            continue

        try:
            await page.screenshot(path=f"screenshots/lever_unknown_{attempt}.png")
        except Exception:
            pass
        return "unknown"

    return "failed"
