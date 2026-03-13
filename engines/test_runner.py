"""
engines/test_runner.py — GuardianAI Autonomous QA
Test Execution Engine: runs TestCase steps against the live site using Playwright.

Uses the SAME Playwright browser context pattern as crawler.py — no new browser
infrastructure. Each test case runs in an isolated page within the shared context.

Key design decisions:
  - All execution is async (mirrors crawler.py pattern)
  - Each step produces a StepResult with screenshot, timing, errors
  - Runner never crashes the job — exceptions are caught and recorded as FAIL
  - Screenshots saved to screenshots/ with prefix "test_{run_id}_{tc_id}_step_{n}"
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

SCREENSHOT_DIR = os.environ.get("SCREENSHOT_DIR", "screenshots")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

# Max seconds per individual test step before timeout
STEP_TIMEOUT_MS = int(os.environ.get("GUARDIAN_STEP_TIMEOUT_MS", "8000"))

# Max seconds per full test case execution
CASE_TIMEOUT_S = int(os.environ.get("GUARDIAN_CASE_TIMEOUT_S", "60"))


# ── Data Structures ────────────────────────────────────────────────────────────

@dataclass
class StepResult:
    step_number: int
    action: str
    description: str
    status: str                        # pass|fail|skip|error
    actual_outcome: str = ""
    screenshot_path: Optional[str] = None
    error_message: Optional[str] = None
    duration_ms: float = 0.0
    http_status: Optional[int] = None
    js_errors: list[str] = field(default_factory=list)
    network_requests: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "step_number":      self.step_number,
            "action":           self.action,
            "description":      self.description,
            "status":           self.status,
            "actual_outcome":   self.actual_outcome,
            "screenshot_path":  self.screenshot_path,
            "error_message":    self.error_message,
            "duration_ms":      round(self.duration_ms, 1),
            "http_status":      self.http_status,
            "js_errors":        self.js_errors,
        }


@dataclass
class TestCaseResult:
    tc_id: str
    flow_id: str
    scenario: str
    status: str                        # pass|fail|error|timeout|skip
    step_results: list[StepResult] = field(default_factory=list)
    actual_result: str = ""
    failure_step: Optional[int] = None
    failure_reason: Optional[str] = None
    duration_ms: float = 0.0
    screenshot_path: Optional[str] = None  # final page screenshot on failure

    def to_dict(self) -> dict:
        return {
            "tc_id":          self.tc_id,
            "flow_id":        self.flow_id,
            "scenario":       self.scenario,
            "status":         self.status,
            "actual_result":  self.actual_result,
            "failure_step":   self.failure_step,
            "failure_reason": self.failure_reason,
            "duration_ms":    round(self.duration_ms, 1),
            "screenshot_path": self.screenshot_path,
            "step_results":   [s.to_dict() for s in self.step_results],
        }


# ── Screenshot Helper ──────────────────────────────────────────────────────────

async def _take_screenshot(page, run_id: int, tc_id: str, step: int) -> Optional[str]:
    try:
        fname = f"test_{run_id}_{tc_id}_step{step}_{int(time.time()*1000)}.png"
        path = os.path.join(SCREENSHOT_DIR, fname)
        await page.screenshot(path=path, full_page=False, timeout=5000)
        return f"screenshots/{fname}"
    except Exception as e:
        logger.debug(f"Screenshot failed: {e}")
        return None


# ── Step Executors ─────────────────────────────────────────────────────────────

async def _execute_navigate(page, step: dict, run_id: int, tc_id: str) -> StepResult:
    t0 = time.time()
    http_status = None
    js_errors: list[str] = []
    page.on("pageerror", lambda e: js_errors.append(str(e)[:200]))

    target = step.get("target") or ""
    if not target:
        # Try to get from description
        desc = step.get("description") or ""
        if desc.startswith("http"):
            target = desc.split()[0]

    try:
        resp = await page.goto(target, wait_until="domcontentloaded", timeout=STEP_TIMEOUT_MS)
        http_status = resp.status if resp else None
        await page.wait_for_load_state("networkidle", timeout=5000)

        duration = (time.time() - t0) * 1000
        sshot = await _take_screenshot(page, run_id, tc_id, step["step_number"])

        if http_status and http_status >= 400:
            return StepResult(
                step_number=step["step_number"],
                action="navigate",
                description=step.get("description", ""),
                status="fail",
                actual_outcome=f"HTTP {http_status} — page returned error",
                screenshot_path=sshot,
                duration_ms=duration,
                http_status=http_status,
                js_errors=js_errors,
                error_message=f"HTTP {http_status}",
            )

        title = await page.title()
        return StepResult(
            step_number=step["step_number"],
            action="navigate",
            description=step.get("description", ""),
            status="pass",
            actual_outcome=f"Page loaded: '{title}' (HTTP {http_status or 200})",
            screenshot_path=sshot,
            duration_ms=duration,
            http_status=http_status,
            js_errors=js_errors,
        )

    except asyncio.TimeoutError:
        duration = (time.time() - t0) * 1000
        return StepResult(
            step_number=step["step_number"], action="navigate",
            description=step.get("description", ""),
            status="fail",
            actual_outcome="Page did not load within timeout",
            error_message="Navigation timeout",
            duration_ms=duration,
        )
    except Exception as e:
        duration = (time.time() - t0) * 1000
        return StepResult(
            step_number=step["step_number"], action="navigate",
            description=step.get("description", ""),
            status="error",
            actual_outcome=f"Navigation error: {str(e)[:200]}",
            error_message=str(e)[:300],
            duration_ms=duration,
        )


async def _execute_fill(page, step: dict, run_id: int, tc_id: str) -> StepResult:
    t0 = time.time()
    selector = step.get("target") or ""
    value = step.get("value") or ""

    if not selector:
        return StepResult(
            step_number=step["step_number"], action="fill",
            description=step.get("description", ""),
            status="skip",
            actual_outcome="No selector available — step skipped",
        )

    try:
        await page.wait_for_selector(selector, state="visible", timeout=STEP_TIMEOUT_MS)
        await page.fill(selector, value)
        duration = (time.time() - t0) * 1000
        return StepResult(
            step_number=step["step_number"], action="fill",
            description=step.get("description", ""),
            status="pass",
            actual_outcome=f"Filled '{selector}' with test value",
            duration_ms=duration,
        )
    except Exception as e:
        duration = (time.time() - t0) * 1000
        sshot = await _take_screenshot(page, run_id, tc_id, step["step_number"])
        return StepResult(
            step_number=step["step_number"], action="fill",
            description=step.get("description", ""),
            status="fail",
            actual_outcome=f"Could not interact with field: {str(e)[:200]}",
            screenshot_path=sshot,
            error_message=str(e)[:300],
            duration_ms=duration,
        )


async def _execute_click_or_submit(page, step: dict, run_id: int, tc_id: str) -> StepResult:
    t0 = time.time()
    action = step.get("action", "click")

    # Build selector candidates
    selector = step.get("target") or ""
    candidates = [selector] if selector else []

    if action == "submit" or not candidates:
        candidates += [
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("Submit")',
            'button:has-text("Login")',
            'button:has-text("Sign In")',
            'button:has-text("Register")',
            'button:has-text("Checkout")',
            'button:has-text("Pay")',
            'button:has-text("Search")',
            'button:has-text("Send")',
            'form button',
        ]

    url_before = page.url
    js_errors: list[str] = []
    page.on("pageerror", lambda e: js_errors.append(str(e)[:200]))

    for sel in candidates:
        if not sel:
            continue
        try:
            elem = page.locator(sel).first
            if await elem.count() == 0:
                continue
            await elem.click(timeout=STEP_TIMEOUT_MS)
            await page.wait_for_load_state("networkidle", timeout=8000)
            break
        except Exception:
            continue
    else:
        # No selector worked
        duration = (time.time() - t0) * 1000
        sshot = await _take_screenshot(page, run_id, tc_id, step["step_number"])
        return StepResult(
            step_number=step["step_number"], action=action,
            description=step.get("description", ""),
            status="fail",
            actual_outcome="Could not find a clickable element",
            screenshot_path=sshot,
            error_message="No matching selector found",
            duration_ms=duration,
            js_errors=js_errors,
        )

    url_after = page.url
    duration = (time.time() - t0) * 1000
    sshot = await _take_screenshot(page, run_id, tc_id, step["step_number"])

    navigated = url_after != url_before
    outcome = (
        f"Clicked element; navigated to {url_after}"
        if navigated
        else "Clicked element; page did not navigate"
    )

    return StepResult(
        step_number=step["step_number"], action=action,
        description=step.get("description", ""),
        status="pass",
        actual_outcome=outcome,
        screenshot_path=sshot,
        duration_ms=duration,
        js_errors=js_errors,
    )


# ── Test Case Runner ───────────────────────────────────────────────────────────

async def run_test_case(
    context,                 # Playwright BrowserContext (shared with crawler)
    test_case: dict,
    run_id: int,
) -> TestCaseResult:
    """
    Executes a single test case's steps in a fresh page tab.
    Returns TestCaseResult with pass/fail per step.
    """
    tc_id = test_case["tc_id"]
    scenario = test_case["scenario"]
    page = await context.new_page()

    result = TestCaseResult(
        tc_id=tc_id,
        flow_id=test_case.get("flow_id", ""),
        scenario=scenario,
    )

    t0 = time.time()
    step_results: list[StepResult] = []

    try:
        steps = test_case.get("steps") or []

        for step in steps:
            action = step.get("action", "navigate")

            try:
                if action == "navigate":
                    sr = await _execute_navigate(page, step, run_id, tc_id)
                elif action == "fill":
                    sr = await _execute_fill(page, step, run_id, tc_id)
                elif action in ("click", "submit"):
                    sr = await _execute_click_or_submit(page, step, run_id, tc_id)
                else:
                    sr = StepResult(
                        step_number=step["step_number"], action=action,
                        description=step.get("description", ""),
                        status="skip",
                        actual_outcome=f"Action '{action}' not yet implemented",
                    )
            except Exception as e:
                sr = StepResult(
                    step_number=step["step_number"], action=action,
                    description=step.get("description", ""),
                    status="error",
                    actual_outcome=f"Unexpected error: {str(e)[:200]}",
                    error_message=str(e)[:300],
                )

            step_results.append(sr)

            # Stop on first hard failure
            if sr.status in ("fail", "error"):
                result.failure_step = step["step_number"]
                result.failure_reason = sr.error_message or sr.actual_outcome
                result.screenshot_path = sr.screenshot_path
                break

    except asyncio.TimeoutError:
        result.status = "timeout"
        result.actual_result = f"Test case timed out after {CASE_TIMEOUT_S}s"
    except Exception as e:
        result.status = "error"
        result.actual_result = f"Runner error: {str(e)[:300]}"
    finally:
        try:
            await page.close()
        except Exception:
            pass

    # Determine overall status
    if result.status not in ("timeout", "error"):
        failed = [s for s in step_results if s.status in ("fail", "error")]
        skipped = [s for s in step_results if s.status == "skip"]
        passed = [s for s in step_results if s.status == "pass"]

        if failed:
            result.status = "fail"
            result.actual_result = failed[0].actual_outcome
        elif len(passed) == 0 and skipped:
            result.status = "skip"
            result.actual_result = "All steps were skipped — insufficient page data"
        else:
            result.status = "pass"
            result.actual_result = f"All {len(passed)} executed steps passed"

    result.step_results = step_results
    result.duration_ms = (time.time() - t0) * 1000

    logger.info(f"[test_runner] {tc_id} '{scenario}' → {result.status} ({result.duration_ms:.0f}ms)")
    return result


async def run_all_test_cases(
    context,
    test_cases: list[dict],
    run_id: int,
    max_cases: int = 20,
) -> list[TestCaseResult]:
    """
    Runs all test cases sequentially (to avoid race conditions with shared session state).
    Cap at max_cases to prevent runaway execution time.
    """
    results: list[TestCaseResult] = []
    cases_to_run = test_cases[:max_cases]

    for tc in cases_to_run:
        try:
            result = await asyncio.wait_for(
                run_test_case(context, tc, run_id),
                timeout=CASE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            result = TestCaseResult(
                tc_id=tc["tc_id"],
                flow_id=tc.get("flow_id", ""),
                scenario=tc.get("scenario", ""),
                status="timeout",
                actual_result=f"Timed out after {CASE_TIMEOUT_S}s",
            )
        results.append(result)

    passed = sum(1 for r in results if r.status == "pass")
    failed = sum(1 for r in results if r.status in ("fail", "error", "timeout"))
    logger.info(f"[test_runner] Run {run_id}: {passed} passed, {failed} failed from {len(results)} cases")
    return results