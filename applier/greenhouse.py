import asyncio
import os
import anthropic
from playwright.async_api import async_playwright
from config import ANTHROPIC_API_KEY

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

MY_PROFILE = """
Name: Houman Eskandani
Email: eskandanihouman@gmail.com
Phone: 949-870-0432
Location: Irvine, CA
LinkedIn: https://linkedin.com/in/houman-eskandani-347b2016b
GitHub: https://github.com/Houmaneskandani
Website: https://github.com/Houmaneskandani
Experience: 4+ years of software engineering experience
Skills: Go (Golang), Python, Java, JavaScript, SQL, GraphQL, backend development, APIs, PostgreSQL, MongoDB, Docker, AWS, GCP
Current/last role: Software Engineer at THE VPORT
Education: B.S. in Computer Science, University of California Riverside
Work authorization: Open to discuss
Salary expectation: Open to discuss
Notice period: Open to discuss
Open to remote: Yes
"""

async def get_answer(question: str, field_type: str, profile_text: str = None) -> str:
    profile = profile_text or MY_PROFILE
    prompt = f"""
You are an expert job application assistant filling out a form on behalf of this candidate:

{profile}

Your job is to answer ANY question intelligently — whether it's about the candidate's background, 
behavioral questions, technical questions, or general questions.

Question: "{question}"
Field type: {field_type}

Instructions:
- For factual questions (name, phone, location, salary etc): use the candidate's profile data
- For yes/no questions: reply with just "Yes" or "No" based on their profile
- For dropdown/multiple choice: reply with the single best answer
- For behavioral questions ("describe a time when...", "what's your greatest strength"):
  Write a concise, professional answer using their real experience from the profile.
  Make it sound natural and genuine, not generic.
- For motivation questions ("why do you want to work here", "what interests you"):
  Write enthusiastically based on their skills and the role
- For technical questions ("describe your experience with X"):
  Answer honestly based on their skills — if they have it, elaborate; if not, say they're familiar with similar technologies
- For demographic questions (veteran, disability, gender, race):
  Use their exact profile data — never assume or guess these
- For questions about work authorization, sponsorship:
  Use their exact work_auth from profile
- For unknown questions with no profile data:
  Give a professional, reasonable answer that a strong software engineer candidate would give
- Keep answers concise:
  - Yes/No fields: just "Yes" or "No"
  - Short text: under 15 words
  - Textarea: 3-5 sentences max, specific and genuine
- Reply with ONLY the answer text, no explanation, no preamble
"""
    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()
    except Exception as e:
        print(f"    ✗ AI error: {e}")
        return ""

MY_INFO = {
    "first_name": "Houman",
    "last_name": "Eskandani",
    "email": "eskandanihouman@gmail.com",
    "phone": "949-870-0432",
    "resume_path": "/Users/houmaneskandani/Jobfinder/job-bot/resume.pdf",
    "location": "Irvine, CA",
}

async def get_frame(page):
    """Get the correct frame where the form lives."""
    await asyncio.sleep(2)
    for f in page.frames:
        if "job-boards.greenhouse.io" in f.url or "boards.greenhouse.io" in f.url:
            print(f"    ✓ Found form frame: {f.url[:60]}")
            return f
    print("    ℹ No iframe found, using main page")
    return page

async def apply_greenhouse(job: dict, dry_run: bool = True, user_info: dict = None, profile_text: str = None):
    info = user_info or MY_INFO
    profile = profile_text or MY_PROFILE

    print(f"\n  Applying to: {job['title']} @ {job['company']}")
    print(f"  URL: {job['url']}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        try:
            await page.goto(job["url"], timeout=30000)
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(2)

            apply_btn = page.locator("a:has-text('Apply'), button:has-text('Apply')")
            if await apply_btn.count() > 0:
                await apply_btn.first.click()
                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(2)
                print("    ✓ Clicked Apply on main page")
            else:
                print("    ℹ No Apply button, form may already be visible")

            frame = await get_frame(page)

            await fill_by_id(frame, "first_name", info.get("first_name", ""))
            await fill_by_id(frame, "last_name", info.get("last_name", ""))
            await fill_by_id(frame, "email", info.get("email", ""))
            await fill_by_id(frame, "phone", info.get("phone", ""))
            await fill_by_id(frame, "candidate-location", info.get("location", ""))

            await fill_react_select(frame, "country", "United States", "Country", MY_PROFILE)

            resume_input = frame.locator("input#resume[type='file']")
            if await resume_input.count() > 0:
                resume_path = info.get("resume_path", MY_INFO.get("resume_path", ""))
                if resume_path and os.path.exists(resume_path):
                    await resume_input.set_input_files(resume_path)
                    print("    ✓ Resume uploaded")
                    await asyncio.sleep(2)
                else:
                    print("    ✗ Resume file not found")

            await fill_custom_questions_with_ai(frame, profile)

            print("    ✓ Form filled!")

            if dry_run:
                print("    ⚠ DRY RUN — not submitting")
                await asyncio.sleep(15)
            else:
                result = await handle_errors_and_retry(frame, page, profile_text=profile)
                return result

        except Exception as e:
            print(f"    ✗ Error: {e}")
            return "failed"
        finally:
            await browser.close()

    return "dry_run"

async def fill_by_id(frame, field_id: str, value: str):
    try:
        el = frame.locator(f"#{field_id}")
        if await el.count() > 0:
            await el.first.fill(value)
            print(f"    ✓ Filled #{field_id}")
            return True
    except Exception as e:
        print(f"    ✗ Could not fill #{field_id}: {e}")
    return False


async def handle_errors_and_retry(frame, page, max_retries: int = 3, profile_text: str = None) -> str:
    """
    After submit, check for validation errors, fix them, and retry.
    """
    for attempt in range(max_retries):
        submit = frame.locator("button:has-text('Submit application')")
        if await submit.count() == 0:
            print("    ✗ Submit button not found")
            return "failed"

        await submit.click()
        await asyncio.sleep(2)

        success = frame.locator("h1:has-text('Thank'), h1:has-text('Application received'), h1:has-text('Success')")
        if await success.count() > 0:
            print("    ✓ Application submitted successfully!")
            return "applied"

        if "confirmation" in page.url or "thank" in page.url.lower():
            print("    ✓ Submitted — confirmation page detected")
            return "applied"

        print(f"\n    ⚠ Attempt {attempt + 1} — checking for errors...")
        errors = await find_errors(frame)

        if not errors:
            print("    ? No errors found but no success either — taking screenshot")
            await page.screenshot(path=f"screenshots/unknown_{attempt}.png")
            return "unknown"

        print(f"    Found {len(errors)} error(s):")
        fixed = 0
        for field_id, error_text in errors:
            print(f"      ✗ {field_id}: {error_text}")
            result = await fix_error(frame, field_id, error_text, profile_text=profile_text)
            if result:
                fixed += 1
                print(f"      ✓ Fixed {field_id}")

        if fixed == 0:
            print("    ✗ Could not fix errors — taking screenshot")
            await page.screenshot(path=f"screenshots/error_{attempt}.png")
            return "failed"

        print(f"    Fixed {fixed} errors, retrying submit...")
        await asyncio.sleep(1)

    return "failed"


async def find_errors(frame) -> list[tuple[str, str]]:
    """Find all validation error messages on the page."""
    errors = []

    error_els = await frame.locator(
        "[aria-invalid='true'], .field_error, [class*='error']:not([class*='error-message'])"
    ).all()

    for el in error_els:
        field_id = await el.get_attribute("id") or ""

        error_msg = ""

        error_id = await el.get_attribute("aria-errormessage")
        if error_id:
            msg_el = frame.locator(f"#{error_id}")
            if await msg_el.count() > 0:
                error_msg = await msg_el.inner_text()

        if not error_msg and field_id:
            parent = frame.locator(f"#{field_id}").locator("xpath=..")
            if await parent.count() > 0:
                sibling = parent.locator("[class*='error'], .field_error")
                if await sibling.count() > 0:
                    error_msg = await sibling.first.inner_text()

        if field_id or error_msg:
            errors.append((field_id or "unknown", error_msg.strip()))

    required = await frame.locator("input[required], input[aria-required='true'], input[aria-invalid='true']").all()
    for el in required:
        field_id = await el.get_attribute("id") or ""
        value = await el.input_value()
        if not value and field_id:
            errors.append((field_id, "Field is required but empty"))

    return errors


async def fix_error(frame, field_id: str, error_text: str, profile_text: str = None) -> bool:
    """Try to fix a specific field error."""
    if not field_id or field_id == "unknown":
        return False

    el = frame.locator(f"#{field_id}")
    if await el.count() == 0:
        return False

    label_el = frame.locator(f"label[for='{field_id}']")
    label_text = field_id
    if await label_el.count() > 0:
        label_text = await label_el.first.inner_text()

    tag = await el.evaluate("el => el.tagName.toLowerCase()")
    role = await el.get_attribute("role") or ""
    type_ = await el.get_attribute("type") or ""

    print(f"      Fixing: {label_text[:50]} (tag={tag}, type={type_})")

    if role == "combobox":
        answer = await get_answer(label_text, "dropdown", profile_text=profile_text)
        return await fill_react_select(frame, field_id, answer, label_text, profile_text)

    elif type_ == "checkbox":
        await el.check()
        return True

    elif tag == "input":
        answer = await get_answer(label_text, "text", profile_text=profile_text)
        if answer:
            await el.fill(answer)
            return True

    elif tag == "textarea":
        answer = await get_answer(label_text, "textarea", profile_text=profile_text)
        if answer:
            await el.fill(answer)
            return True

    return False


async def fill_react_select(frame, field_id: str, value: str = None, label: str = "", profile_text: str = None):
    try:
        if field_id.isdigit():
            el = frame.locator(f"[id='{field_id}']")
        else:
            el = frame.locator(f"#{field_id}")

        if await el.count() == 0 or not await el.is_visible():
            return False

        # Open the dropdown
        await el.click(timeout=5000)
        await asyncio.sleep(0.8)

        # Get ALL visible options
        options = frame.locator("div[class*='option']")
        count = await options.count()
        option_texts = []
        for i in range(count):
            text = (await options.nth(i).inner_text()).strip()
            if text:
                option_texts.append(text)

        if not option_texts:
            await el.press("Escape")
            print(f"    - No options found for {field_id}")
            return False

        print(f"    ? AI choosing from: {option_texts[:5]}...")

        # Ask Claude to pick the best option
        prompt = f"""
You are filling out a job application for this candidate:
{profile_text or MY_PROFILE}

Question: "{label}"
Available options: {option_texts}

Pick the BEST option from the list above that fits this candidate.
Reply with ONLY the exact option text, nothing else.
If it's about declining to share personal info (gender, race, etc), pick the "decline" option.
If unsure, pick the most neutral or positive option.
"""
        try:
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=50,
                messages=[{"role": "user", "content": prompt}]
            )
            ai_choice = message.content[0].text.strip()
            print(f"    → AI chose: {ai_choice}")
        except Exception as e:
            print(f"    ✗ AI error: {e}")
            # Fall back to first non-empty option
            ai_choice = option_texts[0] if option_texts else ""

        if not ai_choice:
            await el.press("Escape")
            return False

        # Find and click the matching option
        for i in range(count):
            opt = options.nth(i)
            text = (await opt.inner_text()).strip()
            if text.lower() == ai_choice.lower() or ai_choice.lower() in text.lower():
                await opt.click()
                print(f"    ✓ Selected: {text}")
                await asyncio.sleep(0.3)
                return True

        # Fuzzy match if exact fails
        for i in range(count):
            opt = options.nth(i)
            text = (await opt.inner_text()).strip()
            if any(word in text.lower() for word in ai_choice.lower().split()):
                await opt.click()
                print(f"    ✓ Selected (fuzzy): {text}")
                return True

        await el.press("Escape")
        print(f"    ✗ Could not match AI choice to options")
        return False

    except Exception as e:
        print(f"    ✗ Combobox failed {field_id}: {e}")
        return False


# Common country names to skip — these are dropdown option labels, not real questions
COUNTRY_NAMES = {
    "australia", "belgium", "brazil", "canada", "france", "germany",
    "india", "indonesia", "ireland", "israel", "italy", "japan",
    "malaysia", "mexico", "new zealand", "poland", "portugal", "romania",
    "singapore", "south korea", "spain", "sweden", "switzerland",
    "thailand", "the netherlands", "uae", "uk", "us", "united states",
    "united kingdom", "china", "hong kong"
}


async def fill_custom_questions_with_ai(frame, profile_text: str = None):
    profile = profile_text or MY_PROFILE

    # 1. Handle ALL React Select comboboxes
    comboboxes = await frame.locator("input[role='combobox']").all()
    for el in comboboxes:
        field_id = await el.get_attribute("id")
        if not field_id:
            continue
        if field_id == "country":
            continue
        if not await el.is_visible():
            continue

        # Find label
        if field_id.isdigit():
            label_el = frame.locator(f"label[for='{field_id}']")
        else:
            label_el = frame.locator(f"label[for='{field_id}'], [id='{field_id}-label']")

        label_text = ""
        if await label_el.count() > 0:
            label_text = await label_el.first.inner_text()

        if not label_text or label_text.strip().lower() in COUNTRY_NAMES:
            continue

        print(f"    ? Combobox: {label_text[:60]}")
        answer = await get_answer(label_text, "dropdown", profile_text=profile)
        if answer:
            await fill_react_select(frame, field_id, answer, label_text, profile)
        else:
            await fill_react_select(frame, field_id, "", label_text, profile)

    # 2. Handle native SELECT dropdowns
    selects = await frame.locator("select[id^='question_']").all()
    for el in selects:
        field_id = await el.get_attribute("id")
        if not field_id:
            continue
        label_el = frame.locator(f"label[for='{field_id}']")
        label_text = ""
        if await label_el.count() > 0:
            label_text = await label_el.first.inner_text()
        if not label_text:
            continue

        print(f"    ? Select: {label_text[:60]}")
        answer = await get_answer(label_text, "dropdown", profile_text=profile_text)
        if answer:
            try:
                await el.select_option(label=answer)
                print(f"    ✓ Selected: {answer}")
            except Exception:
                try:
                    options = await el.locator("option").all()
                    for opt in options:
                        opt_text = await opt.inner_text()
                        if answer.lower() in opt_text.lower():
                            val = await opt.get_attribute("value")
                            await el.select_option(value=val)
                            print(f"    ✓ Selected: {opt_text}")
                            break
                except Exception as e:
                    print(f"    ✗ Select failed: {e}")

    # 3. Handle CHECKBOX groups
    checkboxes = await frame.locator("input[type='checkbox'][id^='question_']").all()
    checkbox_groups = {}
    for cb in checkboxes:
        name = await cb.get_attribute("name")
        if not name:
            continue
        if name not in checkbox_groups:
            checkbox_groups[name] = []
        checkbox_groups[name].append(cb)

    for group_name, boxes in checkbox_groups.items():
        option_texts = []
        for cb in boxes:
            cb_id = await cb.get_attribute("id")
            cb_label = frame.locator(f"label[for='{cb_id}']")
            if await cb_label.count() > 0:
                option_texts.append((cb, await cb_label.first.inner_text()))

        first_cb_id = await boxes[0].get_attribute("id")
        group_label = frame.locator(f"label[for='{group_name}'], [id='{group_name}-label']")
        label_text = group_name

        print(f"    ? Checkbox group: {group_name[:40]} options={[t for _, t in option_texts]}")
        answer = await get_answer(f"{group_name} pick from: {[t for _, t in option_texts]}", "checkbox", profile_text=profile_text)

        for cb, text in option_texts:
            if answer.lower() in text.lower() or text.lower() in answer.lower():
                await cb.check()
                print(f"    ✓ Checked: {text}")

    # 4. Handle RADIO groups
    radios = await frame.locator("input[type='radio'][id^='question_']").all()
    radio_groups = {}
    for radio in radios:
        name = await radio.get_attribute("name")
        if not name:
            continue
        if name not in radio_groups:
            radio_groups[name] = []
        radio_groups[name].append(radio)

    for group_name, options in radio_groups.items():
        option_texts = []
        for opt in options:
            opt_id = await opt.get_attribute("id")
            opt_label = frame.locator(f"label[for='{opt_id}']")
            if await opt_label.count() > 0:
                option_texts.append(await opt_label.first.inner_text())

        print(f"    ? Radio: {group_name[:40]} → {option_texts}")
        answer = await get_answer(f"{group_name} choose from: {option_texts}", "radio", profile_text=profile_text)
        if answer:
            for i, opt in enumerate(options):
                if i < len(option_texts) and answer.lower() in option_texts[i].lower():
                    await opt.click()
                    print(f"    ✓ Radio: {option_texts[i]}")
                    break

    # 5. Handle TEXT inputs and TEXTAREAS
    inputs = await frame.locator(
        "input[id^='question_']:not([type='radio']):not([type='checkbox']):not([type='hidden']):not([type='file']):not([role='combobox'])"
    ).all()
    textareas = await frame.locator("textarea[id^='question_']").all()

    for el in inputs + textareas:
        field_id = await el.get_attribute("id")
        if not field_id or field_id == "country":
            continue

        label_el = frame.locator(f"label[for='{field_id}']")
        label_text = ""
        if await label_el.count() > 0:
            label_text = await label_el.first.inner_text()
        if not label_text or label_text.strip().lower() in COUNTRY_NAMES:
            continue

        tag = await el.evaluate("el => el.tagName.toLowerCase()")
        field_type = "textarea" if tag == "textarea" else "text"

        print(f"    ? Text: {label_text[:60]}")
        answer = await get_answer(label_text, field_type, profile_text=profile_text)
        if answer:
            await el.fill(answer)
            print(f"    ✓ {answer[:50]}")
        else:
            print(f"    - Skipped: no match for '{label_text[:40]}'")