"""
inspect_nav.py
Run from project root: python inspect_nav.py

Logs into ATIRA CRM and inspects the actual DOM structure of the
navigation menu on the home page — so we know exactly which
CSS selectors to use to capture nav links.
"""
import asyncio
from playwright.async_api import async_playwright
from dotenv import load_dotenv
from pathlib import Path
import os

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=True)

LOGIN_URL  = os.environ.get("CRAWLER_LOGIN_URL",  "http://103.108.207.222:5555")
USERNAME   = os.environ.get("CRAWLER_USERNAME",   "EMP002")
PASSWORD   = os.environ.get("CRAWLER_PASSWORD",   "EMP002")
USER_FIELD = os.environ.get("CRAWLER_USERNAME_FIELD", "#txtUserName")
PASS_FIELD = os.environ.get("CRAWLER_PASSWORD_FIELD", "#txtPwd")
SUBMIT     = os.environ.get("CRAWLER_SUBMIT", "button:has-text('Sign In')")
SUCCESS    = os.environ.get("CRAWLER_SUCCESS_URL", "/Account/Home")


async def inspect():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        # ── Login ──────────────────────────────────────────────────────────
        print(f"Navigating to {LOGIN_URL} ...")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1000)
        await page.fill(USER_FIELD, USERNAME)
        await page.fill(PASS_FIELD, PASSWORD)
        await page.click(SUBMIT)
        await page.wait_for_load_state("networkidle", timeout=20000)
        await page.wait_for_timeout(2000)

        print(f"After login URL: {page.url}")
        if "login" in page.url.lower() or "account/login" in page.url.lower():
            print("ERROR: Still on login page — check credentials")
            await browser.close()
            return

        print(f"\n✓ Logged in. Page title: {await page.title()}\n")

        # ── Check what nav elements exist ──────────────────────────────────
        result = await page.evaluate("""() => {
            const report = {};

            // 1. Standard nav tags
            const navTags = document.querySelectorAll('nav');
            report.nav_tags = navTags.length;

            // 2. role=navigation
            const roleNav = document.querySelectorAll('[role="navigation"]');
            report.role_navigation = roleNav.length;

            // 3. Common sidebar/menu class patterns
            const patterns = [
                '.navbar', '.sidebar', '.side-nav', '.sidenav', '#sidebar',
                '.menu', '.nav-menu', '.main-menu', '.left-menu',
                '.nav-left', '.navigation', '#nav', '#menu',
                '[class*="sidebar"]', '[class*="nav-"]', '[id*="sidebar"]',
                '[id*="menu"]', '.aside', 'aside',
            ];
            report.pattern_matches = {};
            for (const p of patterns) {
                const els = document.querySelectorAll(p);
                if (els.length > 0) {
                    report.pattern_matches[p] = {
                        count: els.length,
                        // Get first few link texts
                        sample_links: [...els[0].querySelectorAll('a[href]')]
                            .slice(0, 5)
                            .map(a => ({text: a.textContent.trim().substring(0,40), href: a.href}))
                    };
                }
            }

            // 4. All <a> tags on the page (count + sample)
            const allLinks = document.querySelectorAll('a[href]');
            report.total_links = allLinks.length;
            report.sample_links = [...allLinks].slice(0, 20).map(a => ({
                text: a.textContent.trim().replace(/\\s+/g,' ').substring(0,50),
                href: a.href,
                parent_tag: a.parentElement ? a.parentElement.tagName : null,
                parent_class: a.parentElement ? a.parentElement.className.substring(0,60) : null,
            }));

            // 5. Body class/id for framework detection
            report.body_class = document.body.className.substring(0, 200);
            report.body_id = document.body.id;

            // 6. Check for common frameworks
            report.has_bootstrap = !!document.querySelector('[class*="navbar"]');
            report.has_jquery_ui = typeof jQuery !== 'undefined';

            return report;
        }""")

        print("=" * 60)
        print(f"nav_tags found:        {result['nav_tags']}")
        print(f"role=navigation found: {result['role_navigation']}")
        print(f"total <a> links:       {result['total_links']}")
        print(f"body class:            {result['body_class'][:100]}")
        print(f"has Bootstrap navbar:  {result['has_bootstrap']}")
        print()

        print("── Pattern matches ──────────────────────────────────────")
        if result['pattern_matches']:
            for pattern, info in result['pattern_matches'].items():
                print(f"  {pattern}: {info['count']} element(s)")
                for link in info['sample_links']:
                    print(f"    → [{link['text']}] {link['href']}")
        else:
            print("  None of the standard patterns matched!")

        print()
        print("── Sample page links (first 20) ─────────────────────────")
        for link in result['sample_links']:
            print(f"  [{link['text']:40s}] parent={link['parent_tag']}.{link['parent_class'][:30]}")
            print(f"    {link['href']}")

        print()
        print("=" * 60)
        print("WHAT TO LOOK FOR:")
        print("  Find the pattern that contains the main navigation links")
        print("  (CountryMaster, StateMaster, etc.)")
        print("  That pattern needs to be added to capture_dom_elements()")

        await browser.close()


asyncio.run(inspect())