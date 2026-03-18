"""
inspect_site2.py
Run from project root: python inspect_site2.py
"""
import asyncio
from playwright.async_api import async_playwright

TARGET_URL = "http://120.72.91.205:8035/"

async def inspect():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        print(f"\nNavigating to {TARGET_URL} ...")
        try:
            await page.goto(TARGET_URL, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception as e:
            print(f"  WARNING: {e}")

        print(f"Current URL: {page.url}")
        print(f"Page title:  {await page.title()}")

        print("\n=== INPUTS ===")
        inputs = await page.query_selector_all("input")
        for inp in inputs:
            id_   = await inp.get_attribute("id")
            name  = await inp.get_attribute("name")
            type_ = await inp.get_attribute("type")
            ph    = await inp.get_attribute("placeholder")
            print(f"  id={id_!r:30s}  name={name!r:30s}  type={type_!r:12s}  placeholder={ph!r}")

        print("\n=== BUTTONS ===")
        buttons = await page.query_selector_all("button, input[type='submit']")
        for btn in buttons:
            id_   = await btn.get_attribute("id")
            type_ = await btn.get_attribute("type")
            try:
                txt = await btn.inner_text()
            except Exception:
                txt = ""
            print(f"  id={id_!r:30s}  type={type_!r:12s}  text={txt!r}")

        await browser.close()

asyncio.run(inspect())