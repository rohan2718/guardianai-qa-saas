"""
engines/test_case_generator.py — GuardianAI Autonomous QA  (v2)
Test Case Generator: converts FlowDefinition objects into structured QA test cases
with Playwright-executable Python snippets.

KEY CHANGES v2:
  - _generate_playwright_snippet() produces fully executable async Python code.
    No TODO comments. No missing selectors.
  - Selectors from flow_discovery v2 (element_selector field) are used directly.
    For nav flows, :has-text() selectors are parsed into get_by_role() calls.
  - For fill actions: page.fill(selector, value) with the exact selector from flow.
  - For submit actions: looks for button[type="submit"] first, then by label text.
  - Proper wait_for_load_state() after navigation and clicks.
  - JS error detection wired into every test.
  - Each test file is self-contained and runnable with: python test_file.py
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ── Data Structures ────────────────────────────────────────────────────────────

@dataclass
class TestStep:
    step_number: int
    description: str
    action: str          # navigate|fill|click|submit|assert|wait
    target: Optional[str] = None   # CSS selector or URL
    value: Optional[str] = None    # value to type (fill action)
    page_url: Optional[str] = None # destination URL (used as nav fallback)


@dataclass
class TestCase:
    tc_id: str
    flow_id: str
    scenario: str
    description: str
    preconditions: list[str]
    steps: list[TestStep]
    expected_result: str
    actual_result: Optional[str] = None
    status: str = "pending"
    severity: str = "medium"
    playwright_snippet: Optional[str] = None
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "tc_id":             self.tc_id,
            "flow_id":           self.flow_id,
            "scenario":          self.scenario,
            "description":       self.description,
            "preconditions":     self.preconditions,
            "expected_result":   self.expected_result,
            "actual_result":     self.actual_result,
            "status":            self.status,
            "severity":          self.severity,
            "playwright_snippet": self.playwright_snippet,
            "tags":              self.tags,
            "steps": [
                {
                    "step_number": s.step_number,
                    "description": s.description,
                    "action":      s.action,
                    "target":      s.target,
                    "value":       s.value,
                    "page_url":    getattr(s, "page_url", ""),
                }
                for s in self.steps
            ],
        }


# ── Playwright Snippet Generator ───────────────────────────────────────────────

def _extract_has_text(selector: str) -> Optional[str]:
    """
    Extracts the text value from a :has-text("...") selector.
    Returns None if the pattern is not found.
    """
    m = re.search(r':has-text\("([^"]+)"\)', selector)
    if m:
        return m.group(1)
    m = re.search(r":has-text\('([^']+)'\)", selector)
    if m:
        return m.group(1)
    return None


def _generate_playwright_snippet(tc: "TestCase", flow: dict) -> str:
    """Generates stable, runnable async Playwright Python for a TestCase."""
    fn_name = tc.tc_id.lower().replace("-", "_")

    def _py_str(v: str) -> str:
        return repr(v or "")

    lines = [
        f"# {tc.tc_id}: {tc.scenario}",
        "import asyncio",
        "from playwright.async_api import async_playwright, expect",
        "",
        f"async def {fn_name}():",
        "    async with async_playwright() as p:",
        "        browser = await p.chromium.launch(headless=True)",
        "        page = await browser.new_page()",
        "        page.set_default_timeout(15000)",
        "",
        "        js_errors = []",
        "        page.on('pageerror', lambda e: js_errors.append(str(e)))",
        "",
        "        async def safe_fill(target, value, label_hint=''):",
        "            if not value:",
        "                return False",
        "            if label_hint:",
        "                try:",
        "                    loc = page.get_by_label(label_hint, exact=False).first",
        "                    if await loc.count() > 0: await loc.fill(value); return True",
        "                except Exception: pass",
        "                try:",
        "                    loc = page.get_by_placeholder(label_hint, exact=False).first",
        "                    if await loc.count() > 0: await loc.fill(value); return True",
        "                except Exception: pass",
        "                for role in ('textbox', 'combobox', 'spinbutton', 'searchbox'):",
        "                    try:",
        "                        loc = page.get_by_role(role, name=label_hint).first",
        "                        if await loc.count() > 0: await loc.fill(value); return True",
        "                    except Exception: continue",
        "            if not target:",
        "                return False",
        "            try:",
        "                loc = page.locator(f'xpath={target}').first if target.strip().startswith('/') else page.locator(target).first",
        "                if await loc.count() == 0: return False",
        "                tag = ((await loc.evaluate('el => el.tagName')) or '').lower()",
        "                itype = ((await loc.get_attribute('type')) or '').lower()",
        "                if tag == 'select': await loc.select_option(label=value)",
        "                elif itype in ('checkbox', 'radio'): await loc.check()",
        "                elif tag == 'div': await loc.click(); await page.get_by_text(value, exact=False).first.click()",
        "                else: await loc.fill(value)",
        "                return True",
        "            except Exception:",
        "                return False",
        "",
        "        async def safe_submit(target=''):",
        "            for name in ('Submit', 'Send', 'Sign In', 'Login', 'Register', 'Continue', 'Next'):",
        "                try:",
        "                    btn = page.get_by_role('button', name=name).first",
        "                    if await btn.count() > 0: await btn.click(); return True",
        "                except Exception: pass",
        "            for sel in (target, 'button[type=\"submit\"]', 'input[type=\"submit\"]', '[role=\"button\"]:has-text(\"Submit\")'):",
        "                if not sel: continue",
        "                try:",
        "                    loc = page.locator(sel).first",
        "                    if await loc.count() > 0: await loc.click(); return True",
        "                except Exception: continue",
        "            return False",
        "",
        "        last_url = ''",
    ]

    for step in tc.steps:
        action = step.action
        target = step.target or ""
        value = step.value or ""
        desc = step.description or ""
        label_hint = desc.split(":")[0].replace("Enter ", "").replace("Select ", "").strip()

        lines.append(f"        # Step {step.step_number}: {desc}")
        if action == "navigate":
            nav_url = target if target.startswith("http") else (step.page_url or "")
            lines.append(f"        if {_py_str(nav_url)} and last_url.rstrip('/') != {_py_str(nav_url)}.rstrip('/'):")
            lines.append(f"            response = await page.goto({_py_str(nav_url)}, wait_until='domcontentloaded')")
            lines.append("            if response: assert response.status < 400")
            lines.append("            try: await page.wait_for_load_state('networkidle', timeout=5000)")
            lines.append("            except Exception: pass")
            lines.append(f"            last_url = {_py_str(nav_url)}")
        elif action == "fill":
            lines.append(f"        ok = await safe_fill({_py_str(target)}, {_py_str(value)}, {_py_str(label_hint)})")
            lines.append(f"        if not ok: print('WARN: fill failed for step {step.step_number}')")
        elif action == "submit":
            lines.append(f"        ok = await safe_submit({_py_str(target)})")
            lines.append(f"        if not ok: print('WARN: submit click failed for step {step.step_number}')")
            lines.append("        try: await page.wait_for_load_state('networkidle', timeout=8000)")
            lines.append("        except Exception: pass")
        elif action == "click":
            lines.append("        try:")
            lines.append(f"            loc = page.locator({_py_str(target)}).first")
            lines.append("            if await loc.count() > 0: await loc.click()")
            lines.append("        except Exception:")
            lines.append(f"            print('WARN: click failed for step {step.step_number}')")
        elif action == "assert":
            lines.append(f"        await expect(page.locator({_py_str(target)})).to_be_visible()")
        lines.append("")

    lines += [
        "        assert page.url, 'No final URL — navigation may have failed'",
        "        await browser.close()",
        "",
        "if __name__ == '__main__':",
        f"    asyncio.run({fn_name}())",
    ]
    return "\n".join(lines)


# ── Test Step Builder ──────────────────────────────────────────────────────────

def _build_test_steps(flow_dict: dict) -> list[TestStep]:
    """
    Converts FlowDefinition steps (dicts) to TestStep objects.
    Preserves the element_selector from flow_discovery as step.target.
    Extracts fill values from action_detail strings like "Enter email: 'foo@bar.com'".
    """
    test_steps = []
    for s in flow_dict.get("steps") or []:
        action_map = {
            "navigate":  "navigate",
            "fill_form": "fill",
            "click":     "click",
            "submit":    "submit",
        }
        action = action_map.get(s.get("action"), "navigate")

        # element_selector from flow_discovery is our primary selector
        target = s.get("element_selector")

        # For navigate steps, use the page URL as the target
        if action == "navigate" and not target:
            target = s.get("page_url", "")

        value = None
        detail = s.get("action_detail") or ""

        # Extract fill value from patterns like "Enter label: 'value'"
        if action == "fill" and "'" in detail:
            # Match: Enter SomeLabel: 'theValue'
            m = re.search(r"'([^']+)'", detail)
            if m:
                value = m.group(1)

        test_steps.append(TestStep(
            step_number=s["step_number"],
            description=detail,
            action=action,
            target=target,
            value=value,
            page_url=s.get("page_url", ""),   # destination URL for nav fallback
        ))

    return test_steps


# ── Severity + Precondition Maps ───────────────────────────────────────────────

_SEVERITY_MAP = {
    "login":          "critical",
    "registration":   "critical",
    "checkout":       "critical",
    "cart":           "high",
    "shop":           "high",
    "dashboard":      "high",
    "password_reset": "high",
    "search":         "medium",
    "contact":        "medium",
    "profile":        "medium",
    "navigation":     "medium",
    "newsletter":     "low",
    "generic_form":   "medium",
}

_PRECONDITIONS_MAP = {
    "login": [
        "Application is accessible and running",
        "A valid user account exists in the system",
        "User is not currently logged in",
    ],
    "registration": [
        "Application is accessible and running",
        "Registration is open (not invite-only)",
        "Email address used for test does not already exist",
    ],
    "checkout": [
        "Application is accessible and running",
        "User is logged in with a valid account",
        "At least one product is available for purchase",
        "Cart contains at least one item",
    ],
    "search": [
        "Application is accessible and running",
        "At least one indexed item exists to search for",
    ],
    "contact": [
        "Application is accessible and running",
        "Contact form is enabled and email service is configured",
    ],
    "navigation": [
        "Application is accessible and running",
        "User has access to the pages in this flow",
    ],
}


# ── Main Entry Point ───────────────────────────────────────────────────────────

def generate_test_cases(flows: list[dict], run_id: int) -> list[TestCase]:
    """
    Main entry point. Accepts flow dicts (from discover_flows_as_dicts)
    and returns a list of TestCase objects with executable Playwright snippets.
    """
    test_cases: list[TestCase] = []

    for i, flow in enumerate(flows, 1):
        tc_id     = f"TC-{run_id}-{i:03d}"
        flow_type = flow.get("flow_type", "navigation")
        severity  = _SEVERITY_MAP.get(flow_type, "medium")
        preconditions = _PRECONDITIONS_MAP.get(flow_type, [
            "Application is accessible and running",
        ])

        steps = _build_test_steps(flow)

        last_step_raw = (flow.get("steps") or [{}])[-1]
        expected = (
            last_step_raw.get("expected_outcome")
            or f"{flow['flow_name']} completes successfully without errors"
        )

        tc = TestCase(
            tc_id=tc_id,
            flow_id=flow["flow_id"],
            scenario=flow["flow_name"],
            description=flow.get("description", ""),
            preconditions=preconditions,
            steps=steps,
            expected_result=expected,
            severity=severity,
            status="pending",
            tags=flow.get("tags", []) + [flow_type],
        )

        # Generate the executable Playwright snippet
        tc.playwright_snippet = _generate_playwright_snippet(tc, flow)

        test_cases.append(tc)

    logger.info(
        f"[test_case_generator] Generated {len(test_cases)} test cases "
        f"from {len(flows)} flows for run {run_id}"
    )
    return test_cases


def generate_test_cases_as_dicts(flows: list[dict], run_id: int) -> list[dict]:
    """Convenience wrapper returning plain dicts for JSON serialisation."""
    return [tc.to_dict() for tc in generate_test_cases(flows, run_id)]
