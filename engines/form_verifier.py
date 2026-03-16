"""
engines/form_verifier.py — GuardianAI Form Submission Verification
===================================================================
Called by test_runner.py after a form submit step to determine whether
the submission actually succeeded.

Four verification strategies (tried in order, first pass wins):
  1. URL redirect  — page URL changed after submit
  2. Success DOM   — success/confirmation message appeared in DOM
  3. Network 2xx   — a POST/PUT response with 2xx status was intercepted
  4. Field reset   — required fields are now empty (form cleared itself)

If none pass → submission is marked FAIL with the specific reason recorded.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# DOM patterns that indicate a successful form submission
_SUCCESS_PATTERNS = [
    r"thank\s*you",
    r"success(fully)?",
    r"submitted",
    r"received\s+your",
    r"we.ll\s+(be\s+in\s+touch|contact\s+you)",
    r"confirm(ation|ed)",
    r"order\s+(placed|received|confirm)",
    r"sent\s+successfully",
    r"message\s+sent",
    r"registered\s+successfully",
    r"account\s+created",
    r"welcome",
    r"logged\s+in",
    r"sign(ed)?\s*in",
    r"dashboard",
]

_SUCCESS_RE = re.compile("|".join(_SUCCESS_PATTERNS), re.IGNORECASE)

# DOM patterns that indicate a failure / validation error
_FAILURE_PATTERNS = [
    r"invalid",
    r"required\s+field",
    r"please\s+(fill|enter|correct)",
    r"error",
    r"failed",
    r"incorrect\s+(password|email|username)",
    r"does\s+not\s+match",
]

_FAILURE_RE = re.compile("|".join(_FAILURE_PATTERNS), re.IGNORECASE)


async def verify_form_submission(
    page,
    url_before: str,
    network_responses: list[dict],
    timeout_ms: int = 5000,
) -> dict:
    """
    Verifies whether a form submission succeeded.

    Parameters
    ----------
    page             : Playwright Page object (after submit click)
    url_before       : URL before the submit click was made
    network_responses: List of {url, status, method} dicts captured during submit
    timeout_ms       : How long to wait for DOM settle

    Returns
    -------
    {
        "success": bool,
        "strategy": str,           # which check passed / all_failed
        "detail": str,             # human-readable explanation
        "failure_reason": str | None,
        "url_after": str,
    }
    """
    try:
        # Give the page a moment to settle
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=2000)
            except Exception:
                pass

        url_after = page.url

        # ── Strategy 1: URL redirect ──────────────────────────────────────────
        if _url_changed(url_before, url_after):
            return {
                "success":        True,
                "strategy":       "url_redirect",
                "detail":         f"Page redirected from {url_before} to {url_after}",
                "failure_reason": None,
                "url_after":      url_after,
            }

        # ── Strategy 2: Success message in DOM ───────────────────────────────
        dom_result = await _check_dom_for_success(page)
        if dom_result["success"]:
            return {
                "success":        True,
                "strategy":       "success_dom",
                "detail":         dom_result["detail"],
                "failure_reason": None,
                "url_after":      url_after,
            }

        # ── Strategy 3: Network 2xx response ─────────────────────────────────
        net_result = _check_network_responses(network_responses)
        if net_result["success"]:
            return {
                "success":        True,
                "strategy":       "network_2xx",
                "detail":         net_result["detail"],
                "failure_reason": None,
                "url_after":      url_after,
            }

        # ── Strategy 4: Form fields were reset ───────────────────────────────
        reset_result = await _check_fields_reset(page)
        if reset_result["success"]:
            return {
                "success":        True,
                "strategy":       "fields_reset",
                "detail":         reset_result["detail"],
                "failure_reason": None,
                "url_after":      url_after,
            }

        # ── All strategies failed — look for a failure reason ─────────────────
        failure_reason = _detect_failure_reason(dom_result, net_result)

        return {
            "success":        False,
            "strategy":       "all_failed",
            "detail":         "No success indicators found after form submission",
            "failure_reason": failure_reason,
            "url_after":      url_after,
        }

    except Exception as e:
        logger.warning(f"[form_verifier] verify_form_submission error: {e}")
        return {
            "success":        False,
            "strategy":       "error",
            "detail":         f"Verification error: {str(e)[:200]}",
            "failure_reason": str(e)[:200],
            "url_after":      getattr(page, "url", url_before),
        }


def _url_changed(before: str, after: str) -> bool:
    """Returns True if the URL meaningfully changed (ignores trailing slash diffs)."""
    return before.rstrip("/") != after.rstrip("/")


async def _check_dom_for_success(page) -> dict:
    """Scans visible page text for success/failure patterns."""
    try:
        visible_text = await page.evaluate("""
            () => {
                // Get all visible text on the page
                const walker = document.createTreeWalker(
                    document.body,
                    NodeFilter.SHOW_TEXT,
                    {
                        acceptNode: function(node) {
                            const el = node.parentElement;
                            if (!el) return NodeFilter.FILTER_REJECT;
                            const s = window.getComputedStyle(el);
                            if (s.display === 'none' || s.visibility === 'hidden') {
                                return NodeFilter.FILTER_REJECT;
                            }
                            return NodeFilter.FILTER_ACCEPT;
                        }
                    }
                );
                const texts = [];
                let node;
                while ((node = walker.nextNode())) {
                    const t = node.textContent.trim();
                    if (t.length > 2) texts.push(t);
                }
                return texts.join(' ').substring(0, 3000);
            }
        """)

        if _SUCCESS_RE.search(visible_text or ""):
            match = _SUCCESS_RE.search(visible_text)
            snippet = visible_text[max(0, match.start()-20):match.end()+40].strip()
            return {
                "success": True,
                "detail":  f"Success indicator found in DOM: '{snippet[:100]}'",
                "raw_text": visible_text[:500],
            }

        # Also check for ARIA live region announcements
        aria_text = await page.evaluate("""
            () => {
                const regions = document.querySelectorAll(
                    '[aria-live], [role="alert"], [role="status"], .alert, .notification, .flash-message, .toast'
                );
                return [...regions].map(el => el.textContent.trim()).join(' ');
            }
        """)

        if _SUCCESS_RE.search(aria_text or ""):
            return {
                "success": True,
                "detail":  f"Success found in ARIA/alert region: '{aria_text[:100]}'",
                "raw_text": aria_text[:500],
            }

        failure_in_dom = bool(_FAILURE_RE.search(visible_text or ""))

        return {
            "success":       False,
            "detail":        "No success message found in visible DOM",
            "failure_in_dom": failure_in_dom,
            "raw_text":      (visible_text or "")[:500],
        }

    except Exception as e:
        logger.debug(f"[form_verifier] DOM check failed: {e}")
        return {"success": False, "detail": str(e)[:100], "failure_in_dom": False}


def _check_network_responses(responses: list[dict]) -> dict:
    """Looks for a POST/PUT/PATCH response with 2xx status."""
    submit_methods = {"POST", "PUT", "PATCH"}

    for resp in responses:
        method = (resp.get("method") or "").upper()
        status = resp.get("status") or 0
        url    = resp.get("url", "")

        if method in submit_methods and 200 <= status < 300:
            return {
                "success": True,
                "detail":  f"Network {method} {url[:80]} → {status}",
            }

    # Check if there was a 4xx on the submit endpoint (explicit failure)
    for resp in responses:
        method = (resp.get("method") or "").upper()
        status = resp.get("status") or 0
        if method in submit_methods and status >= 400:
            return {
                "success":      False,
                "detail":       f"Submit request returned HTTP {status}",
                "http_failure": status,
            }

    return {"success": False, "detail": "No submit network request captured"}


async def _check_fields_reset(page) -> dict:
    """Checks whether required form fields have been cleared (post-submit reset)."""
    try:
        reset = await page.evaluate("""
            () => {
                const inputs = [...document.querySelectorAll('input[required], textarea[required]')];
                if (inputs.length === 0) return { found: false };
                const empty_count = inputs.filter(i => !i.value.trim()).length;
                return {
                    found:       true,
                    total:       inputs.length,
                    empty_count: empty_count,
                    all_empty:   empty_count === inputs.length,
                };
            }
        """)

        if reset.get("found") and reset.get("all_empty"):
            return {
                "success": True,
                "detail":  f"All {reset['total']} required fields were cleared (form reset after submission)",
            }

        return {"success": False, "detail": "Fields not reset"}

    except Exception as e:
        return {"success": False, "detail": str(e)[:100]}


def _detect_failure_reason(dom_result: dict, net_result: dict) -> str:
    """Constructs a human-readable failure reason from the check results."""
    reasons = []

    if dom_result.get("failure_in_dom"):
        raw = dom_result.get("raw_text", "")
        match = _FAILURE_RE.search(raw)
        if match:
            snippet = raw[max(0, match.start()-10):match.end()+40].strip()
            reasons.append(f"Validation error in DOM: '{snippet[:80]}'")

    net_detail = net_result.get("detail", "")
    if "HTTP" in net_detail and "failure" in net_detail.lower():
        reasons.append(net_detail)
    elif net_detail == "No submit network request captured":
        reasons.append("Submit button click did not trigger a network request")

    if not reasons:
        reasons.append("No success signals detected (no redirect, no confirmation message, no 2xx network response, no field reset)")

    return ". ".join(reasons)


def attach_network_interceptor(page, captured_responses: list):
    """
    Attaches a Playwright response listener to capture network responses.
    Call before the submit click; pass the same list to verify_form_submission.

    Usage:
        responses = []
        attach_network_interceptor(page, responses)
        await page.click(submit_selector)
        result = await verify_form_submission(page, url_before, responses)
    """
    async def _on_response(response):
        try:
            captured_responses.append({
                "url":    response.url,
                "status": response.status,
                "method": response.request.method,
            })
        except Exception:
            pass

    page.on("response", _on_response)