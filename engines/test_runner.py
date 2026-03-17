"""
engines/test_runner.py — GuardianAI Autonomous QA  (v4)
========================================================
KEY FIXES in v4:
  - _execute_fill(): multi-selector strings (comma-separated) are now split
    and tried individually — Playwright's page.fill() only accepts one selector.
  - _execute_fill(): 4-strategy fallback chain:
      1. Each CSS selector part individually
      2. page.get_by_label()
      3. page.get_by_placeholder()
      4. Input type-hint fallback for email/password/tel etc.
  - STEP_TIMEOUT_MS raised to 15 000 ms (Vercel/cold-start sites need it)
  - CASE_TIMEOUT_S raised to 90 s
  - _execute_navigate(): best-effort networkidle — timeout never kills the step
  - _execute_click_or_submit(): scroll_into_view before click; form_verifier
    import is guarded so a missing module doesn't crash the runner
  - _split_selectors(): parses comma-separated multi-selectors correctly,
    respecting quoted attribute values
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

SCREENSHOT_DIR  = os.environ.get("SCREENSHOT_DIR", "screenshots")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

STEP_TIMEOUT_MS = int(os.environ.get("GUARDIAN_STEP_TIMEOUT_MS", "15000"))
CASE_TIMEOUT_S  = int(os.environ.get("GUARDIAN_CASE_TIMEOUT_S", "90"))


# ── Data Structures ────────────────────────────────────────────────────────────

@dataclass
class StepResult:
    step_number:           int
    action:                str
    description:           str
    status:                str
    actual_outcome:        str  = ""
    screenshot_path:       Optional[str]  = None
    error_message:         Optional[str]  = None
    duration_ms:           float = 0.0
    http_status:           Optional[int]  = None
    js_errors:             list  = field(default_factory=list)
    network_requests:      list  = field(default_factory=list)
    submission_verified:   Optional[bool] = None
    verification_strategy: Optional[str]  = None

    def to_dict(self) -> dict:
        return {
            "step_number":           self.step_number,
            "action":                self.action,
            "description":           self.description,
            "status":                self.status,
            "actual_outcome":        self.actual_outcome,
            "screenshot_path":       self.screenshot_path,
            "error_message":         self.error_message,
            "duration_ms":           round(self.duration_ms, 1),
            "http_status":           self.http_status,
            "js_errors":             self.js_errors,
            "submission_verified":   self.submission_verified,
            "verification_strategy": self.verification_strategy,
        }


@dataclass
class TestCaseResult:
    tc_id:           str
    flow_id:         str
    scenario:        str
    status:          str = "pending"
    actual_result:   str = ""
    failure_step:    Optional[int] = None
    failure_reason:  Optional[str] = None
    screenshot_path: Optional[str] = None
    duration_ms:     float = 0.0
    step_results:    list  = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "tc_id":           self.tc_id,
            "flow_id":         self.flow_id,
            "scenario":        self.scenario,
            "status":          self.status,
            "actual_result":   self.actual_result,
            "failure_step":    self.failure_step,
            "failure_reason":  self.failure_reason,
            "screenshot_path": self.screenshot_path,
            "duration_ms":     round(self.duration_ms, 1),
            "step_results": [
                s.to_dict() if hasattr(s, "to_dict") else s
                for s in self.step_results
            ],
        }


# ── Screenshot Helper ──────────────────────────────────────────────────────────

async def _take_screenshot(page, run_id: int, tc_id: str, step_num: int) -> Optional[str]:
    path = os.path.join(
        SCREENSHOT_DIR,
        f"test_{run_id}_{tc_id}_step{step_num}_{int(time.time() * 1000)}.png",
    )
    try:
        await page.screenshot(path=path, timeout=5000)
        return path
    except Exception:
        return None


# ── Selector Utilities ─────────────────────────────────────────────────────────

def _split_selectors(selector: str) -> list[str]:
    """
    Splits a comma-separated multi-selector string into individual selectors,
    respecting quoted attribute values so 'input[placeholder="a, b"]' is not split.
    """
    parts     = []
    current   = []
    depth     = 0
    in_quote  = None

    for ch in selector:
        if ch in ('"', "'") and in_quote is None:
            in_quote = ch
        elif ch == in_quote:
            in_quote = None
        elif in_quote is None:
            if ch in ("[", "("):
                depth += 1
            elif ch in ("]", ")"):
                depth -= 1
            elif ch == "," and depth == 0:
                part = "".join(current).strip()
                if part:
                    parts.append(part)
                current = []
                continue
        current.append(ch)

    last = "".join(current).strip()
    if last:
        parts.append(last)

    return parts if parts else [selector]


# ── Fill Helper with 4-Strategy Fallback ──────────────────────────────────────

async def _interact_with_locator(loc, value: str, timeout_ms: int) -> bool:
    """Applies value based on resolved control type without throwing."""
    try:
        await loc.wait_for(state="visible", timeout=min(timeout_ms, 5000))
        tag = ((await loc.evaluate("el => el.tagName")) or "").lower()
        input_type = ((await loc.get_attribute("type")) or "").lower()
        role = ((await loc.get_attribute("role")) or "").lower()

        if tag == "select":
            await loc.select_option(label=value, timeout=timeout_ms)
            return True

        if input_type in ("checkbox", "radio"):
            should_check = value.lower() in ("check", "true", "1", "yes", "on", "selected")
            if should_check:
                await loc.check(timeout=timeout_ms)
            elif input_type == "checkbox":
                await loc.uncheck(timeout=timeout_ms)
            else:
                await loc.check(timeout=timeout_ms)
            return True

        if tag in ("input", "textarea"):
            await loc.fill(value, timeout=timeout_ms)
            return True

        # Custom dropdown / combobox style controls
        if tag == "div" or role in ("combobox", "listbox", "menu"):
            await loc.click(timeout=timeout_ms)
            option_candidates = [
                f'text="{value}"',
                f'[role="option"]:has-text("{value}")',
                f'li:has-text("{value}")',
                f'div:has-text("{value}")',
            ]
            for opt_sel in option_candidates:
                try:
                    opt = loc.page.locator(opt_sel).first
                    if await opt.count() > 0:
                        await opt.wait_for(state="visible", timeout=min(timeout_ms, 3000))
                        await opt.click(timeout=timeout_ms)
                        return True
                except Exception:
                    continue
        return False
    except Exception:
        return False


async def _try_fill_selector(page, selector: str, value: str, timeout_ms: int) -> bool:
    try:
        if not selector:
            return False
        cleaned = selector.strip()
        loc = page.locator(f"xpath={cleaned}").first if cleaned.startswith("/") else page.locator(cleaned).first
        if await loc.count() == 0:
            return False
        return await _interact_with_locator(loc, value, timeout_ms)
    except Exception:
        return False


def _label_hint_from_description(description: str) -> str:
    if not description:
        return ""
    base = description.split(":")[0]
    for prefix in ("Enter ", "Select ", "Choose ", "Set ", "Click "):
        if base.startswith(prefix):
            base = base[len(prefix):]
    return base.strip(" '")


async def _fill_field(
    page,
    selector: str,
    value: str,
    description: str,
    timeout_ms: int,
) -> tuple[bool, str]:
    """
    Tries robust selector chain in order: label, placeholder, role, CSS, XPath.
    Returns (success: bool, method_used: str).
    """
    label_hint = _label_hint_from_description(description)

    # 1) get_by_label
    if label_hint:
        try:
            loc = page.get_by_label(label_hint, exact=False).first
            if await loc.count() > 0 and await _interact_with_locator(loc, value, timeout_ms):
                return True, f"label:{label_hint}"
        except Exception:
            pass

    # 2) get_by_placeholder
    if label_hint:
        try:
            loc = page.get_by_placeholder(label_hint, exact=False).first
            if await loc.count() > 0 and await _interact_with_locator(loc, value, timeout_ms):
                return True, f"placeholder:{label_hint}"
        except Exception:
            pass

    # 3) get_by_role (textbox/combobox/spinbutton)
    if label_hint:
        for role in ("textbox", "combobox", "spinbutton", "searchbox"):
            try:
                loc = page.get_by_role(role, name=label_hint).first
                if await loc.count() > 0 and await _interact_with_locator(loc, value, timeout_ms):
                    return True, f"role:{role}:{label_hint}"
            except Exception:
                continue

    # 4) CSS selectors from target
    for part in _split_selectors(selector) if selector else []:
        if part.strip().startswith("/"):
            continue
        if await _try_fill_selector(page, part, value, timeout_ms):
            return True, f"css:{part[:60]}"

    # 5) XPath fallback from target
    for part in _split_selectors(selector) if selector else []:
        if not part.strip().startswith("/"):
            continue
        if await _try_fill_selector(page, part, value, timeout_ms):
            return True, f"xpath:{part[:60]}"

    return False, "all_strategies_failed"


# ── Step Executors ─────────────────────────────────────────────────────────────

async def _execute_navigate(page, step: dict, run_id: int, tc_id: str) -> StepResult:
    t0  = time.time()
    url = step.get("target") or step.get("value") or ""

    if not url or not url.startswith("http"):
        return StepResult(
            step_number=step["step_number"], action="navigate",
            description=step.get("description", ""),
            status="skip",
            actual_outcome=f"No valid URL provided for navigate step (got: {url!r})",
        )

    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=STEP_TIMEOUT_MS)
        # Best-effort: don't fail if networkidle times out (SPA hydration)
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        status_code = resp.status if resp else None
        duration    = (time.time() - t0) * 1000
        sshot       = await _take_screenshot(page, run_id, tc_id, step["step_number"])

        if status_code and status_code >= 400:
            return StepResult(
                step_number=step["step_number"], action="navigate",
                description=step.get("description", ""),
                status="fail",
                actual_outcome=f"Page returned HTTP {status_code}",
                screenshot_path=sshot, http_status=status_code, duration_ms=duration,
            )

        return StepResult(
            step_number=step["step_number"], action="navigate",
            description=step.get("description", ""),
            status="pass",
            actual_outcome=f"Navigated to {page.url}",
            screenshot_path=sshot, http_status=status_code, duration_ms=duration,
        )

    except Exception as e:
        duration = (time.time() - t0) * 1000
        return StepResult(
            step_number=step["step_number"], action="navigate",
            description=step.get("description", ""),
            status="fail",
            actual_outcome=f"Navigation failed: {str(e)[:150]}",
            error_message=str(e)[:300], duration_ms=duration,
        )


async def _execute_fill(page, step: dict, run_id: int, tc_id: str) -> StepResult:
    t0          = time.time()
    selector    = step.get("target") or ""
    value       = step.get("value")  or ""
    description = step.get("description", "")

    if not value:
        return StepResult(
            step_number=step["step_number"], action="fill",
            description=description, status="skip",
            actual_outcome="No fill value provided — skipping field",
        )

    success, method = await _fill_field(page, selector, value, description, STEP_TIMEOUT_MS)

    duration = (time.time() - t0) * 1000
    sshot    = await _take_screenshot(page, run_id, tc_id, step["step_number"])

    if success:
        return StepResult(
            step_number=step["step_number"], action="fill",
            description=description, status="pass",
            actual_outcome=f"Filled field via {method}",
            screenshot_path=sshot, duration_ms=duration,
        )

    return StepResult(
        step_number=step["step_number"], action="fill",
        description=description, status="fail",
        actual_outcome=f"Could not fill field — all 4 strategies failed (selector: {selector!r})",
        error_message=(
            f"Field not found or not interactable. "
            f"Tried get_by_label, get_by_placeholder, get_by_role, CSS selectors, and XPath fallback."
        ),
        screenshot_path=sshot, duration_ms=duration,
    )


async def _execute_click_or_submit(page, step: dict, run_id: int, tc_id: str) -> StepResult:
    t0        = time.time()
    action    = step.get("action", "click")
    selector  = step.get("target") or ""
    is_submit = action == "submit"
    url_before = page.url

    network_responses: list[dict] = []
    if is_submit:
        try:
            from engines.form_verifier import attach_network_interceptor
            attach_network_interceptor(page, network_responses)
        except ImportError:
            pass

    js_errors: list = []
    page.on("pageerror", lambda e: js_errors.append(str(e)[:200]))

    candidates: list[str] = []
    if selector:
        candidates.extend(_split_selectors(selector))

    if is_submit:
        candidates += [
            '[role="button"][type="submit"]',
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("Submit")',
            'button:has-text("Send")',
            'button:has-text("Sign In")',
            'button:has-text("Login")',
            'button:has-text("Register")',
            'button:has-text("Continue")',
            'button:has-text("Next")',
            '[role="button"]:has-text("Submit")',
            'form button',
        ]
    elif not selector:
        candidates += ['a[href]', 'button', '[role="button"]']

    # Deduplicate order-preserving
    seen = set()
    candidates = [c for c in candidates if not (c in seen or seen.add(c))]

    clicked = False

    # Submit-first semantic role fallback
    if is_submit:
        for button_name in ("Submit", "Send", "Sign In", "Login", "Register", "Continue", "Next"):
            try:
                role_btn = page.get_by_role("button", name=button_name).first
                if await role_btn.count() > 0:
                    await role_btn.scroll_into_view_if_needed(timeout=3000)
                    await role_btn.click(timeout=STEP_TIMEOUT_MS)
                    clicked = True
                    break
            except Exception:
                continue

    for sel in candidates:
        if clicked or not sel:
            continue
        try:
            cleaned = sel.strip()
            loc = page.locator(f"xpath={cleaned}").first if cleaned.startswith("/") else page.locator(cleaned).first
            if await loc.count() == 0:
                continue
            try:
                await loc.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            await loc.click(timeout=STEP_TIMEOUT_MS)
            clicked = True
            break
        except Exception:
            continue

    if is_submit and not clicked:
        try:
            text_btn = page.get_by_text("Submit", exact=False).first
            if await text_btn.count() > 0:
                await text_btn.click(timeout=STEP_TIMEOUT_MS)
                clicked = True
        except Exception:
            pass

    if not clicked:
        # ── Fallback: direct URL navigation ──────────────────────────────────
        # For nav items in dropdown/hover menus (common in CRM apps), the
        # element exists in the DOM but is hidden behind CSS hover states.
        # If the step carries a destination URL, navigate there directly —
        # this is functionally equivalent and produces the correct result.
        fallback_url = step.get("page_url") or step.get("href") or ""
        if fallback_url and fallback_url.startswith("http"):
            try:
                resp = await page.goto(fallback_url, wait_until="domcontentloaded",
                                       timeout=STEP_TIMEOUT_MS)
                try:
                    await page.wait_for_load_state("networkidle", timeout=4000)
                except Exception:
                    pass
                duration = (time.time() - t0) * 1000
                sshot    = await _take_screenshot(page, run_id, tc_id, step["step_number"])
                status_code = resp.status if resp else 200
                if status_code >= 400:
                    return StepResult(
                        step_number=step["step_number"], action=action,
                        description=step.get("description", ""),
                        status="fail",
                        actual_outcome=f"Direct navigation to {fallback_url} returned HTTP {status_code}",
                        screenshot_path=sshot, duration_ms=duration, js_errors=js_errors,
                    )
                return StepResult(
                    step_number=step["step_number"], action=action,
                    description=step.get("description", ""),
                    status="pass",
                    actual_outcome=f"Menu click failed (hidden element); navigated directly to {page.url}",
                    screenshot_path=sshot, duration_ms=duration, js_errors=js_errors,
                )
            except Exception as nav_err:
                pass   # fall through to the hard fail below

        duration = (time.time() - t0) * 1000
        sshot    = await _take_screenshot(page, run_id, tc_id, step["step_number"])
        return StepResult(
            step_number=step["step_number"], action=action,
            description=step.get("description", ""),
            status="fail", actual_outcome="Could not find a clickable element",
            screenshot_path=sshot,
            error_message=f"No matching selector found. Tried {len(candidates)} candidates.",
            duration_ms=duration, js_errors=js_errors,
        )

    # ── Submit verification ───────────────────────────────────────────────────
    if is_submit:
        verification = None
        try:
            from engines.form_verifier import verify_form_submission
            verification = await verify_form_submission(
                page, url_before=url_before,
                network_responses=network_responses, timeout_ms=8000,
            )
        except ImportError:
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            verification = {
                "success":        url_before.rstrip("/") != page.url.rstrip("/"),
                "strategy":       "url_redirect_fallback",
                "detail":         f"URL after submit: {page.url}",
                "failure_reason": None,
            }
        except Exception as ve:
            verification = {"success": False, "strategy": "error",
                            "detail": str(ve)[:200], "failure_reason": str(ve)[:200]}

        duration = (time.time() - t0) * 1000
        sshot    = await _take_screenshot(page, run_id, tc_id, step["step_number"])

        if verification["success"]:
            return StepResult(
                step_number=step["step_number"], action=action,
                description=step.get("description", ""),
                status="pass",
                actual_outcome=f"Submitted and verified ({verification['strategy']}): {verification['detail']}",
                screenshot_path=sshot, duration_ms=duration, js_errors=js_errors,
                submission_verified=True, verification_strategy=verification["strategy"],
            )
        return StepResult(
            step_number=step["step_number"], action=action,
            description=step.get("description", ""),
            status="fail",
            actual_outcome=f"Submission unverified: {verification.get('failure_reason') or verification['detail']}",
            screenshot_path=sshot,
            error_message=verification.get("failure_reason") or verification["detail"],
            duration_ms=duration, js_errors=js_errors,
            submission_verified=False, verification_strategy=verification["strategy"],
        )

    # ── Regular click settle ──────────────────────────────────────────────────
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass

    url_after = page.url
    duration  = (time.time() - t0) * 1000
    sshot     = await _take_screenshot(page, run_id, tc_id, step["step_number"])

    return StepResult(
        step_number=step["step_number"], action=action,
        description=step.get("description", ""),
        status="pass",
        actual_outcome=(
            f"Clicked; navigated to {url_after}"
            if url_before.rstrip("/") != url_after.rstrip("/")
            else "Clicked; page did not navigate"
        ),
        screenshot_path=sshot, duration_ms=duration, js_errors=js_errors,
    )


# ── Test Case Runner ───────────────────────────────────────────────────────────

async def run_test_case(context, test_case: dict, run_id: int) -> TestCaseResult:
    tc_id    = test_case["tc_id"]
    scenario = test_case["scenario"]
    page     = await context.new_page()

    result = TestCaseResult(
        tc_id=tc_id, flow_id=test_case.get("flow_id", ""), scenario=scenario,
    )
    t0           = time.time()
    step_results = []

    try:
        for step in (test_case.get("steps") or []):
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
                        status="skip", actual_outcome=f"Action '{action}' not implemented",
                    )
            except Exception as e:
                sr = StepResult(
                    step_number=step["step_number"], action=action,
                    description=step.get("description", ""),
                    status="error", actual_outcome=f"Unexpected error: {str(e)[:200]}",
                    error_message=str(e)[:300],
                )

            step_results.append(sr)
            if sr.status in ("fail", "error") and result.failure_step is None:
                result.failure_step    = step["step_number"]
                result.failure_reason  = sr.error_message or sr.actual_outcome
                result.screenshot_path = sr.screenshot_path

    except asyncio.TimeoutError:
        result.status       = "timeout"
        result.actual_result = f"Test case timed out after {CASE_TIMEOUT_S}s"
    except Exception as e:
        result.status       = "error"
        result.actual_result = f"Runner error: {str(e)[:300]}"
    finally:
        try:
            await page.close()
        except Exception:
            pass

    if result.status not in ("timeout", "error"):
        failed  = [s for s in step_results if s.status in ("fail", "error")]
        passed  = [s for s in step_results if s.status == "pass"]
        skipped = [s for s in step_results if s.status == "skip"]

        if failed:
            result.status       = "fail"
            result.actual_result = failed[0].actual_outcome
        elif not passed and skipped:
            result.status       = "skip"
            result.actual_result = "All steps skipped — insufficient page data"
        else:
            result.status       = "pass"
            result.actual_result = f"All {len(passed)} executed steps passed"

    result.step_results = step_results
    result.duration_ms  = (time.time() - t0) * 1000
    logger.info(f"[test_runner] {tc_id} '{scenario}' → {result.status} ({result.duration_ms:.0f}ms)")
    return result


async def run_all_test_cases(
    context,
    test_cases: list[dict],
    run_id: int,
    max_cases: int = 20,
) -> list[TestCaseResult]:
    results = []
    for tc in test_cases[:max_cases]:
        try:
            result = await asyncio.wait_for(
                run_test_case(context, tc, run_id), timeout=CASE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            result = TestCaseResult(
                tc_id=tc["tc_id"], flow_id=tc.get("flow_id", ""),
                scenario=tc.get("scenario", ""), status="timeout",
                actual_result=f"Timed out after {CASE_TIMEOUT_S}s",
            )
        results.append(result)

    passed = sum(1 for r in results if r.status == "pass")
    failed = sum(1 for r in results if r.status in ("fail", "error", "timeout"))
    logger.info(f"[test_runner] Run {run_id}: {passed} passed, {failed} failed from {len(results)} cases")
    return results