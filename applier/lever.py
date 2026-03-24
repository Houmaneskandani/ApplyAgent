import asyncio
import os
import anthropic
from playwright.async_api import async_playwright
from config import ANTHROPIC_API_KEY
from applier.greenhouse import get_answer, MY_INFO

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


async def apply_lever(job: dict, dry_run: bool = True, user_info: dict = None, profile_text: str = None):
    info = user_info or MY_INFO
    print(f"\n  Applying to: {job['title']} @ {job['company']}")
    print(f"  URL: {job['url']}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        try:
            await page.goto(job["url"], timeout=30000)
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(2)

            # Click apply button — Lever uses "Apply for this job"
            apply_btn = page.locator(
                "a:has-text('Apply for this job'), "
                "button:has-text('Apply for this job'), "
                "a:has-text('Apply'), "
                "button:has-text('Apply')"
            )
            if await apply_btn.count() > 0:
                await apply_btn.first.click()
                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(2)
                print("    ✓ Clicked Apply")

            # Lever forms are on the main page — no iframe needed
            await fill_lever_form(page, user_info=info, profile_text=profile_text)

            print("    ✓ Form filled!")

            if dry_run:
                print("    ⚠ DRY RUN — inspect browser, not submitting")
                await asyncio.sleep(15)
            else:
                result = await submit_lever(page)
                return result

        except Exception as e:
            print(f"    ✗ Error: {e}")
            await page.screenshot(path=f"screenshots/lever_error_{job['id']}.png")
            return "failed"
        finally:
            await browser.close()

    return "dry_run"


async def fill_lever_form(page, user_info: dict = None, profile_text: str = None):
    """Fill standard Lever application fields."""
    info = user_info or MY_INFO

    full_name = f"{info.get('first_name', '')} {info.get('last_name', '')}".strip()
    if not full_name:
        full_name = f"{MY_INFO['first_name']} {MY_INFO['last_name']}"

    fields = {
        "name": full_name,
        "email": info.get("email", MY_INFO["email"]),
        "phone": info.get("phone", MY_INFO["phone"]),
        "location": info.get("location", MY_INFO["location"]),
        "org": info.get("org", "THE VPORT"),
        "urls[LinkedIn]": info.get("linkedin", MY_INFO.get("linkedin", "https://linkedin.com/in/houman-eskandani-347b2016b")),
        "urls[GitHub]": info.get("github", MY_INFO.get("github", "https://github.com/Houmaneskandani")),
        "urls[Portfolio]": info.get("website", MY_INFO.get("website", "https://github.com/Houmaneskandani")),
    }

    for field_name, value in fields.items():
        try:
            el = page.locator(f"input[name='{field_name}']")
            if await el.count() > 0:
                await el.first.fill(value)
                print(f"    ✓ Filled {field_name}")
        except Exception as e:
            print(f"    ✗ Could not fill {field_name}: {e}")

    # Resume upload
    resume_input = page.locator("input[type='file']")
    if await resume_input.count() > 0:
        resume_path = info.get("resume_path", MY_INFO.get("resume_path", ""))
        if resume_path and os.path.exists(resume_path):
            await resume_input.first.set_input_files(resume_path)
            print("    ✓ Resume uploaded")
            await asyncio.sleep(2)
        else:
            print("    ✗ Resume file not found")

    # Cover letter / additional info textarea
    cover = page.locator("textarea[name='comments'], textarea[name='cover']")
    if await cover.count() > 0:
        await cover.first.fill(
            "I am a software engineer with 4+ years of experience in Go, Python, "
            "and backend development. I am excited about this opportunity and believe "
            "my skills align well with your team's needs."
        )
        print("    ✓ Cover letter filled")

    # Custom questions
    await fill_lever_custom_questions(page, profile_text=profile_text)


async def fill_lever_custom_questions(page, profile_text: str = None):
    """Handle Lever's custom application questions."""

    inputs = await page.locator("input[name*='cards']:not([type='file']):not([type='hidden'])").all()
    textareas = await page.locator("textarea[name*='cards']").all()
    selects = await page.locator("select[name*='cards']").all()

    for el in inputs + textareas:
        name = await el.get_attribute("name") or ""
        if not name:
            continue

        label_text = ""
        parent = el.locator("xpath=../..")
        label = parent.locator("label")
        if await label.count() > 0:
            label_text = await label.first.inner_text()

        if not label_text:
            continue

        tag = await el.evaluate("el => el.tagName.toLowerCase()")
        field_type = "textarea" if tag == "textarea" else "text"

        print(f"    ? {label_text[:60]}")
        answer = await get_answer(label_text, field_type, profile_text=profile_text)
        if answer:
            await el.fill(answer)
            print(f"    ✓ {answer[:50]}")
        else:
            print(f"    - Skipped: no match")

    for el in selects:
        name = await el.get_attribute("name") or ""
        if not name:
            continue

        label_text = ""
        parent = el.locator("xpath=../..")
        label = parent.locator("label")
        if await label.count() > 0:
            label_text = await label.first.inner_text()

        if not label_text:
            continue

        print(f"    ? Select: {label_text[:60]}")
        answer = await get_answer(label_text, "dropdown", profile_text=profile_text)
        if answer:
            try:
                await el.select_option(label=answer)
                print(f"    ✓ Selected: {answer}")
            except Exception:
                print(f"    - Could not select: {answer}")


async def submit_lever(page, max_retries: int = 3) -> str:
    """Submit Lever form with error retry."""
    for attempt in range(max_retries):
        submit = page.locator("button[type='submit']:has-text('Submit'), button:has-text('Submit application')")
        if await submit.count() == 0:
            print("    ✗ Submit button not found")
            return "failed"

        await submit.click()
        await asyncio.sleep(2)

        success = page.locator(
            "h1:has-text('Thank'), "
            "h2:has-text('Thank'), "
            ".thank-you, "
            "[class*='confirmation']"
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

        await page.screenshot(path=f"screenshots/lever_unknown_{attempt}.png")
        return "unknown"

    return "failed"
