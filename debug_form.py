import asyncio
from playwright.async_api import async_playwright

TEST_URL = "https://stripe.com/jobs/search?gh_jid=7409691"

async def debug():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        await page.goto(TEST_URL, timeout=30000)
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(2)

        apply_btn = page.locator("a:has-text('Apply'), button:has-text('Apply')")
        if await apply_btn.count() > 0:
            await apply_btn.first.click()
            await asyncio.sleep(2)

        frame = None
        for f in page.frames:
            if "job-boards.greenhouse.io" in f.url:
                frame = f
                break
        if not frame:
            frame = page

        comboboxes = await frame.locator("input[role='combobox']").all()
        for el in comboboxes:
            field_id = await el.get_attribute("id") or ""

            # Skip country and hidden fields
            if field_id in ["country", "iti-0__search-input"]:
                continue

            # Skip if not visible
            is_visible = await el.is_visible()
            if not is_visible:
                continue

            print(f"\nField ID: {field_id}")

            # Click to open
            try:
                await el.click(timeout=5000)
                await asyncio.sleep(0.8)
            except:
                print("  Could not click, skipping")
                continue

            # Get all visible options
            options = await frame.locator("div[class*='option']").all()
            option_texts = []
            for opt in options:
                t = (await opt.inner_text()).strip()
                if t:
                    option_texts.append(t)

            print(f"  Options: {option_texts}")

            # Close
            await el.press("Escape")
            await asyncio.sleep(0.5)

        await asyncio.sleep(10)
        await browser.close()

asyncio.run(debug())