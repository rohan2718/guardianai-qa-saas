"""
engines/validation_engine.py — GuardianAI Autonomous QA
Validation Engine: compares expected outcomes vs actual execution results
and produces structured ValidationResult objects.

Designed to be deterministic and fast — no LLM needed.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ── Data Structures ────────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    tc_id: str
    verdict: str                         # pass|fail|partial|inconclusive
    confidence: float                    # 0.0 – 1.0
    expected: str
    actual: str
    failure_reason: Optional[str] = None
    failure_category: Optional[str] = None   # navigation|interaction|error|timeout|assertion
    remediation_hint: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "tc_id":              self.tc_id,
            "verdict":            self.verdict,
            "confidence":         round(self.confidence, 2),
            "expected":           self.expected,
            "actual":             self.actual,
            "failure_reason":     self.failure_reason,
            "failure_category":   self.failure_category,
            "remediation_hint":   self.remediation_hint,
        }


# ── Validation Rules ───────────────────────────────────────────────────────────

# Keywords that indicate success in the actual_result string
_SUCCESS_SIGNALS = frozenset({
    "pass", "passed", "success", "loaded", "redirected", "confirmed",
    "completed", "created", "submitted", "accepted",
})

# Keywords that indicate failure
_FAILURE_SIGNALS = frozenset({
    "fail", "failed", "error", "timeout", "404", "500", "403",
    "not found", "could not", "no matching", "timed out",
    "did not", "skipped", "skip",
})

# HTTP error patterns
_HTTP_ERROR_RE = re.compile(r"\bHTTP\s+([4-5]\d{2})\b", re.IGNORECASE)

# URL pattern
_URL_RE = re.compile(r"https?://\S+|/\S*", re.IGNORECASE)


def _extract_urls(text: str) -> list[str]:
    return _URL_RE.findall(text)


def _has_redirect_signal(expected: str, actual: str) -> bool:
    """Checks if expected mentions a redirect and actual matches."""
    expected_lower = expected.lower()
    actual_lower = actual.lower()
    if "redirect" not in expected_lower:
        return False

    # Try to extract target URL from expected
    expected_urls = _extract_urls(expected)
    if not expected_urls:
        # Generic redirect expected — check if actual says "navigated to"
        return "navigated" in actual_lower or "redirect" in actual_lower

    for exp_url in expected_urls:
        exp_path = urlparse(exp_url).path.rstrip("/")
        for actual_url in _extract_urls(actual):
            actual_path = urlparse(actual_url).path.rstrip("/")
            if exp_path and exp_path in actual_path:
                return True
    return False


def _categorise_failure(actual: str, step_results: list[dict]) -> tuple[str, str]:
    """Returns (failure_category, remediation_hint)."""
    actual_lower = actual.lower()

    http_match = _HTTP_ERROR_RE.search(actual)
    if http_match:
        code = int(http_match.group(1))
        if code == 404:
            return "navigation", "Page not found — check URL configuration and routing"
        if code == 500:
            return "error", "Server error — check application logs and backend error handlers"
        if code == 403:
            return "navigation", "Access denied — verify authentication state before this step"
        return "navigation", f"HTTP {code} — investigate server configuration"

    if "timeout" in actual_lower:
        return "timeout", "Page exceeded load timeout — check server performance and network latency"

    if "could not find" in actual_lower or "no matching" in actual_lower:
        return "interaction", "UI element not found — verify selector is correct and element is visible"

    if "js error" in actual_lower or "javascript" in actual_lower:
        return "error", "JavaScript error on page — check browser console for script failures"

    if "did not navigate" in actual_lower or "no navigation" in actual_lower:
        return "interaction", "Click did not trigger navigation — verify the button/link is interactive"

    # Check step results for more specific clues
    for sr in (step_results or []):
        if sr.get("status") in ("fail", "error"):
            act = (sr.get("actual_outcome") or "").lower()
            if "fill" in (sr.get("action") or ""):
                return "interaction", "Form field is not interactable — check if field is disabled, hidden, or requires prior state"
            if "submit" in (sr.get("action") or ""):
                return "interaction", "Form submission failed — check submit button selector and form validation"

    return "assertion", "Unexpected result — manually inspect the page at the failed step"


# ── Main Validator ─────────────────────────────────────────────────────────────

def validate_test_case(
    tc_id: str,
    expected_result: str,
    execution_result: dict,
) -> ValidationResult:
    """
    Core validation function. Accepts the test case's expected_result and
    the TestCaseResult dict from test_runner.py.

    Returns a ValidationResult with verdict and reasoning.
    """
    status = execution_result.get("status", "fail")
    actual = execution_result.get("actual_result") or ""
    step_results = execution_result.get("step_results") or []
    failure_reason = execution_result.get("failure_reason") or ""

    # ── Hard pass: runner marked it pass ──────────────────────────────────────
    if status == "pass":
        # Sanity-check: if any step has a JS error, downgrade to partial
        js_errors = [
            e for sr in step_results
            for e in (sr.get("js_errors") or [])
        ]
        if js_errors:
            return ValidationResult(
                tc_id=tc_id,
                verdict="partial",
                confidence=0.70,
                expected=expected_result,
                actual=f"{actual} (with {len(js_errors)} JS error(s))",
                failure_reason=f"JS errors detected: {js_errors[0][:100]}",
                failure_category="error",
                remediation_hint="JS errors occurred during test — check browser console output",
            )

        return ValidationResult(
            tc_id=tc_id,
            verdict="pass",
            confidence=0.95,
            expected=expected_result,
            actual=actual,
        )

    # ── Hard fail: timeout or runner error ────────────────────────────────────
    if status in ("timeout", "error"):
        category, hint = _categorise_failure(actual, step_results)
        return ValidationResult(
            tc_id=tc_id,
            verdict="fail",
            confidence=0.95,
            expected=expected_result,
            actual=actual or f"Test {status}",
            failure_reason=failure_reason or actual,
            failure_category=category,
            remediation_hint=hint,
        )

    # ── Skip: insufficient data ───────────────────────────────────────────────
    if status == "skip":
        return ValidationResult(
            tc_id=tc_id,
            verdict="inconclusive",
            confidence=0.50,
            expected=expected_result,
            actual="Test skipped — page did not have enough interaction data",
            failure_reason="Insufficient UI data for automated execution",
            failure_category="interaction",
            remediation_hint="Ensure the crawler discovers the page with form_validation filter enabled",
        )

    # ── Fail: analyse the actual result ──────────────────────────────────────
    actual_lower = actual.lower()
    expected_lower = expected_result.lower()

    # Check for expected redirect
    if "redirect" in expected_lower and _has_redirect_signal(expected_result, actual):
        return ValidationResult(
            tc_id=tc_id, verdict="pass", confidence=0.85,
            expected=expected_result, actual=actual,
        )

    # Check signals
    has_success = any(sig in actual_lower for sig in _SUCCESS_SIGNALS)
    has_failure = any(sig in actual_lower for sig in _FAILURE_SIGNALS)

    if has_success and not has_failure:
        return ValidationResult(
            tc_id=tc_id, verdict="pass", confidence=0.75,
            expected=expected_result, actual=actual,
        )

    category, hint = _categorise_failure(actual, step_results)
    return ValidationResult(
        tc_id=tc_id,
        verdict="fail",
        confidence=0.90,
        expected=expected_result,
        actual=actual,
        failure_reason=failure_reason or actual,
        failure_category=category,
        remediation_hint=hint,
    )


def validate_all(test_cases: list[dict], execution_results: list[dict]) -> list[ValidationResult]:
    """
    Validates all test cases against their execution results.
    Matches by tc_id.
    """
    result_map = {r["tc_id"]: r for r in execution_results}
    validations: list[ValidationResult] = []

    for tc in test_cases:
        tc_id = tc["tc_id"]
        expected = tc.get("expected_result", "Test completes without errors")
        exec_result = result_map.get(tc_id, {"status": "skip", "actual_result": "No execution result found"})
        validations.append(validate_test_case(tc_id, expected, exec_result))

    passed = sum(1 for v in validations if v.verdict == "pass")
    failed = sum(1 for v in validations if v.verdict == "fail")
    logger.info(f"[validation_engine] {passed} pass, {failed} fail from {len(validations)} validations")
    return validations