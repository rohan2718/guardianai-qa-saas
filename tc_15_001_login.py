"""
TC-15-001: Login Flow — ATIRA CRM
Fix: DO NOT touch the toggle at all. Just enter credentials and sign in.
The toggle default position is ATIRA (left) — clicking it switches to Customer (wrong).
Run: python tc_15_001_login.py
"""

import asyncio
from playwright.async_api import async_playwright

TARGET_URL = "http://103.108.207.222:5555"
USERNAME   = "EMP002"
PASSWORD   = "EMP002"


async def tc_15_001():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        page.set_default_timeout(15000)

        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        # Step 1: Navigate to login page
        print("Step 1: Navigate to login page...")
        response = await page.goto(TARGET_URL, wait_until="domcontentloaded")
        assert response.status < 400, f"Got {response.status}"
        print(f"  ✓ Page loaded (HTTP {response.status})")

        # Step 2: DO NOT touch the toggle — leave it on ATIRA (default left position)
        print("Step 2: Toggle left untouched (ATIRA = default)...")
        print("  ✓ Skipped")

        # Step 3: Enter username
        print("Step 3: Enter username...")
        await page.fill("#txtUserName", USERNAME)
        print(f"  ✓ {USERNAME}")

        # Step 4: Enter password
        print("Step 4: Enter password...")
        await page.fill("#txtPwd", PASSWORD)
        print("  ✓ Password entered")

        # Step 5: Click Sign In and wait for dashboard
        print("Step 5: Click Sign In...")
        await asyncio.gather(
            page.wait_for_load_state("networkidle", timeout=20000),
            page.click("button:has-text('Sign In')")
        )
        await page.wait_for_timeout(2000)

        # Results
        current_url = page.url
        title = await page.title()
        print(f"\n📄 Page title : {title}")
        print(f"🌐 Current URL: {current_url}")

        if "login" in current_url.lower() or current_url.rstrip("/") == TARGET_URL:
            print("✗ FAIL — Still on login page.")
        else:
            print("✓ PASS — Dashboard loaded as ATIRA employee.")

        await page.screenshot(path="post_login.png", full_page=True)
        print("📸 Screenshot saved: post_login.png")

        if errors:
            print(f"\n⚠ JS Errors ({len(errors)}):")
            for e in errors:
                print(f"  - {e}")
        else:
            print("✓ No JS errors")

        print("\n⏸  Staying open 30 seconds...")
        await asyncio.sleep(30)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(tc_15_001())