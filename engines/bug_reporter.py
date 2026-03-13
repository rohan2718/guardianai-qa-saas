"""
engines/bug_reporter.py — GuardianAI Autonomous QA
Bug Report Generator: two modes of operation:

1. PASSIVE MODE (scan findings) — converts page_object raw findings into
   structured bug reports. No test execution needed.

2. ACTIVE MODE (test failures) — converts ValidationResult + TestCaseResult
   into structured bug reports with reproduction steps and Playwright snippets.

All bug reports share the same BugReport dataclass and are stored in DB.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ── Data Structures ────────────────────────────────────────────────────────────

@dataclass
class BugReport:
    bug_title: str
    page_url: str
    bug_type: str              # performance|security|accessibility|functional|interaction|navigation
    severity: str              # critical|high|medium|low
    component: Optional[str]  # CSS selector or element description
    description: str
    impact: str
    steps_to_reproduce: list[str]
    expected_result: str
    actual_result: str
    suggested_fix: str
    screenshot_path: Optional[str] = None
    source: str = "scan"       # scan|test_runner
    tc_id: Optional[str] = None
    flow_id: Optional[str] = None
    run_id: Optional[int] = None
    playwright_snippet: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "bug_title":          self.bug_title,
            "page_url":           self.page_url,
            "bug_type":           self.bug_type,
            "severity":           self.severity,
            "component":          self.component,
            "description":        self.description,
            "impact":             self.impact,
            "steps_to_reproduce": self.steps_to_reproduce,
            "expected_result":    self.expected_result,
            "actual_result":      self.actual_result,
            "suggested_fix":      self.suggested_fix,
            "screenshot_path":    self.screenshot_path,
            "source":             self.source,
            "tc_id":              self.tc_id,
            "flow_id":            self.flow_id,
            "run_id":             self.run_id,
            "playwright_snippet": self.playwright_snippet,
        }


# ── Severity Rules (Passive / Scan Mode) ──────────────────────────────────────

# Each rule: (check_fn, severity, bug_type, title_template, description_template, fix_template, impact)
_SCAN_RULES = [
    # ── Critical ──────────────────────────────────────────────────────────────
    {
        "id": "http_5xx",
        "check": lambda p: (p.get("status") or 200) >= 500,
        "severity": "critical",
        "bug_type": "functional",
        "title": lambda p: f"Server Error ({p['status']}) on {_path(p['url'])}",
        "description": lambda p: f"The page at {p['url']} returned HTTP {p['status']}, indicating a server-side failure. The page is completely inaccessible to users.",
        "impact": "All users are unable to access this page. Crawlers and search engines receive server errors, harming SEO.",
        "fix": "Investigate application logs for stack traces. Check database connectivity, exception handlers, and recent deployments.",
        "steps": lambda p: [f"Open browser", f"Navigate to {p['url']}", "Observe HTTP response status"],
        "expected": "Page loads successfully with HTTP 200",
        "actual": lambda p: f"Server returned HTTP {p['status']}",
    },
    {
        "id": "no_https",
        "check": lambda p: p.get("is_https") is False,
        "severity": "critical",
        "bug_type": "security",
        "title": lambda p: f"Page Served Over HTTP (No TLS) — {_path(p['url'])}",
        "description": lambda p: f"The page {p['url']} is served over plain HTTP without encryption. User data, credentials, and session tokens are transmitted in cleartext.",
        "impact": "User credentials and session data are exposed to network interception (MITM attacks). Browser security warnings will be shown.",
        "fix": "Enforce HTTPS at the server level with a 301 permanent redirect from HTTP to HTTPS. Install or renew an SSL/TLS certificate.",
        "steps": lambda p: [f"Navigate to {p['url']}", "Check browser address bar for HTTPS padlock"],
        "expected": "Page loads over HTTPS with valid certificate",
        "actual": lambda p: "Page served over HTTP — no encryption",
    },

    # ── High ──────────────────────────────────────────────────────────────────
    {
        "id": "http_4xx",
        "check": lambda p: 400 <= (p.get("status") or 200) < 500,
        "severity": "high",
        "bug_type": "navigation",
        "title": lambda p: f"Page Not Accessible ({p['status']}) — {_path(p['url'])}",
        "description": lambda p: f"The page at {p['url']} returned HTTP {p['status']}. This may indicate a missing page, broken internal link, or authentication requirement.",
        "impact": "Users clicking links to this page will encounter an error. Internal linking structure is broken.",
        "fix": "Check if the URL is correct. Implement proper 301 redirects for moved content. Add a helpful 404 page.",
        "steps": lambda p: [f"Navigate to {p['url']}", "Observe error page or HTTP status"],
        "expected": "Page loads with HTTP 200",
        "actual": lambda p: f"HTTP {p['status']} returned",
    },
    {
        "id": "slow_ttfb",
        "check": lambda p: (p.get("ttfb_ms") or 0) > 800,
        "severity": "high",
        "bug_type": "performance",
        "title": lambda p: f"Slow Server Response (TTFB {p['ttfb_ms']}ms) — {_path(p['url'])}",
        "description": lambda p: f"Time to First Byte is {p['ttfb_ms']}ms on {p['url']}. Industry standard is under 200ms. Users experience a blank screen for nearly {p['ttfb_ms'] // 1000 + 1} second(s) before any content appears.",
        "impact": "High TTFB degrades user experience, increases bounce rates, and hurts Core Web Vitals scores which directly impact Google search ranking.",
        "fix": "Implement server-side caching (Redis/Memcached), optimise database queries, enable CDN, or upgrade server tier.",
        "steps": lambda p: [f"Open DevTools Network tab", f"Navigate to {p['url']}", "Measure TTFB on the document request"],
        "expected": "TTFB under 200ms",
        "actual": lambda p: f"TTFB measured at {p['ttfb_ms']}ms",
    },
    {
        "id": "slow_lcp",
        "check": lambda p: (p.get("lcp_ms") or 0) > 4000,
        "severity": "high",
        "bug_type": "performance",
        "title": lambda p: f"Largest Contentful Paint Too Slow ({p['lcp_ms']}ms) — {_path(p['url'])}",
        "description": lambda p: f"LCP is {p['lcp_ms']}ms on {p['url']}. Google classifies LCP above 4000ms as 'Poor'. The main content is not visible to users for over 4 seconds.",
        "impact": "Poor LCP directly reduces search ranking (Core Web Vitals). Users perceive the page as broken and abandon it.",
        "fix": "Optimise hero images (WebP format, appropriate sizing), preload critical resources, eliminate render-blocking scripts.",
        "steps": lambda p: [f"Open Chrome DevTools Performance tab", f"Navigate to {p['url']}", "Measure LCP in Core Web Vitals section"],
        "expected": "LCP under 2500ms (Good)",
        "actual": lambda p: f"LCP is {p['lcp_ms']}ms (Poor)",
    },
    {
        "id": "broken_nav_links",
        "check": lambda p: len(p.get("broken_navigation_links") or []) > 0,
        "severity": "high",
        "bug_type": "navigation",
        "title": lambda p: f"{len(p['broken_navigation_links'])} Broken Navigation Link(s) on {_path(p['url'])}",
        "description": lambda p: f"Found {len(p['broken_navigation_links'])} broken internal navigation link(s) on {p['url']}. Affected URLs: {', '.join(str(l.get('url','') if isinstance(l,dict) else l) for l in p['broken_navigation_links'][:3])}",
        "impact": "Users clicking these links encounter error pages, degrading trust and retention. Search engine crawlers fail to index linked content.",
        "fix": "Update or remove broken links. Implement 301 redirects for moved pages. Set up automated link monitoring.",
        "steps": lambda p: [f"Navigate to {p['url']}", "Click each navigation link", "Observe HTTP response for each"],
        "expected": "All navigation links return HTTP 200",
        "actual": lambda p: f"{len(p['broken_navigation_links'])} link(s) return 4xx or 5xx status",
    },

    # ── Medium ─────────────────────────────────────────────────────────────────
    {
        "id": "many_js_errors",
        "check": lambda p: len(p.get("js_errors") or []) > 0,
        "severity": "medium",
        "bug_type": "functional",
        "title": lambda p: f"{len(p['js_errors'])} JavaScript Error(s) on {_path(p['url'])}",
        "description": lambda p: f"The browser console reports {len(p['js_errors'])} JavaScript error(s) on {p['url']}. JS errors can prevent interactive features from functioning correctly.",
        "impact": "Broken JavaScript may disable forms, navigation, dynamic content, and third-party integrations for affected users.",
        "fix": "Open browser DevTools console, reproduce the errors, and fix or handle exceptions in JavaScript code.",
        "steps": lambda p: [f"Open DevTools Console tab", f"Navigate to {p['url']}", "Observe JavaScript errors in console"],
        "expected": "No JavaScript errors in console",
        "actual": lambda p: f"{len(p['js_errors'])} error(s) logged",
    },
    {
        "id": "high_a11y_issues",
        "check": lambda p: (p.get("accessibility_issues") or 0) > 5,
        "severity": "medium",
        "bug_type": "accessibility",
        "title": lambda p: f"High Accessibility Issue Density ({p['accessibility_issues']} violations) — {_path(p['url'])}",
        "description": lambda p: f"Axe-core detected {p['accessibility_issues']} accessibility violations on {p['url']}. These represent WCAG 2.1 guideline failures affecting users with disabilities.",
        "impact": "Users relying on screen readers, keyboard navigation, or other assistive technologies may be unable to use this page. Potential legal liability under ADA/EAA.",
        "fix": "Run axe DevTools browser extension on this page. Address failures in order: images missing alt text, missing form labels, insufficient color contrast, keyboard navigation issues.",
        "steps": lambda p: [f"Install axe DevTools browser extension", f"Navigate to {p['url']}", "Run accessibility scan", "Review and address reported violations"],
        "expected": "Zero critical accessibility violations (WCAG 2.1 AA)",
        "actual": lambda p: f"{p['accessibility_issues']} violations detected",
    },

    # ── Low ───────────────────────────────────────────────────────────────────
    {
        "id": "moderate_ttfb",
        "check": lambda p: 400 <= (p.get("ttfb_ms") or 0) <= 800,
        "severity": "low",
        "bug_type": "performance",
        "title": lambda p: f"Moderate Server Response Time (TTFB {p['ttfb_ms']}ms) — {_path(p['url'])}",
        "description": lambda p: f"TTFB is {p['ttfb_ms']}ms on {p['url']}. Acceptable but approaching the poor threshold. Proactive optimisation recommended.",
        "impact": "Marginal user experience degradation. May worsen under peak traffic load.",
        "fix": "Consider enabling HTTP caching headers, connection pooling, and query optimisation.",
        "steps": lambda p: [f"Open DevTools Network tab", f"Navigate to {p['url']}", "Measure TTFB on document request"],
        "expected": "TTFB under 200ms",
        "actual": lambda p: f"TTFB is {p['ttfb_ms']}ms",
    },
]


def _path(url: str) -> str:
    return urlparse(url).path or "/"


# ── Passive Mode: Scan-based Bug Reports ──────────────────────────────────────

def generate_bugs_from_page(page: dict, run_id: int) -> list[BugReport]:
    """
    Generates BugReport objects from a single page_object dict.
    Called after each page is scanned in tasks.py.
    """
    bugs: list[BugReport] = []
    seen_ids: set[str] = set()

    for rule in _SCAN_RULES:
        try:
            if not rule["check"](page):
                continue
        except Exception:
            continue

        rule_id = rule["id"]
        if rule_id in seen_ids:
            continue
        seen_ids.add(rule_id)

        try:
            title = rule["title"](page) if callable(rule["title"]) else rule["title"]
            description = rule["description"](page) if callable(rule["description"]) else rule["description"]
            actual = rule["actual"](page) if callable(rule["actual"]) else rule["actual"]
            steps = rule["steps"](page) if callable(rule["steps"]) else rule["steps"]

            bugs.append(BugReport(
                bug_title=title,
                page_url=page["url"],
                bug_type=rule["bug_type"],
                severity=rule["severity"],
                component=None,
                description=description,
                impact=rule["impact"],
                steps_to_reproduce=steps,
                expected_result=rule["expected"],
                actual_result=actual,
                suggested_fix=rule["fix"],
                screenshot_path=page.get("screenshot"),
                source="scan",
                run_id=run_id,
            ))
        except Exception as e:
            logger.warning(f"[bug_reporter] Rule '{rule_id}' error on {page.get('url')}: {e}")

    return bugs


def generate_bugs_from_scan(page_data: list[dict], run_id: int) -> list[BugReport]:
    """Generate bugs from all pages in a scan."""
    all_bugs: list[BugReport] = []
    for page in page_data:
        all_bugs.extend(generate_bugs_from_page(page, run_id))

    # Sort by severity
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    all_bugs.sort(key=lambda b: sev_order.get(b.severity, 9))
    logger.info(f"[bug_reporter] Generated {len(all_bugs)} scan bugs from {len(page_data)} pages")
    return all_bugs


# ── Active Mode: Test-execution Bug Reports ───────────────────────────────────

def generate_bugs_from_test_failure(
    test_case: dict,
    execution_result: dict,
    validation: dict,
    run_id: int,
) -> Optional[BugReport]:
    """
    Generates a bug report from a failed test case execution.
    Returns None if the test passed.
    """
    if validation.get("verdict") == "pass":
        return None

    tc_id = test_case["tc_id"]
    scenario = test_case.get("scenario", "Unknown flow")
    entry_url = execution_result.get("step_results", [{}])[0].get("actual_outcome", "")

    # Determine severity from test case severity field
    sev_map = {"critical": "critical", "high": "high", "medium": "medium", "low": "low"}
    severity = sev_map.get(test_case.get("severity"), "medium")

    # Build steps to reproduce from the test steps
    steps = []
    for s in (test_case.get("steps") or []):
        steps.append(f"Step {s['step_number']}: {s['description']}")

    # Find the failure screenshot
    failure_sshot = execution_result.get("screenshot_path")
    for sr in (execution_result.get("step_results") or []):
        if sr.get("status") in ("fail", "error") and sr.get("screenshot_path"):
            failure_sshot = sr["screenshot_path"]
            break

    # Bug type from failure category
    bug_type_map = {
        "navigation": "navigation",
        "interaction": "functional",
        "error": "functional",
        "timeout": "performance",
        "assertion": "functional",
    }
    bug_type = bug_type_map.get(validation.get("failure_category"), "functional")

    return BugReport(
        bug_title=f"{scenario} — Test Failure",
        page_url=execution_result.get("step_results", [{}])[0].get("actual_outcome", "N/A"),
        bug_type=bug_type,
        severity=severity,
        component=None,
        description=(
            f"Automated test '{scenario}' (ID: {tc_id}) failed during execution. "
            f"Failure category: {validation.get('failure_category', 'unknown')}. "
            f"Reason: {validation.get('failure_reason', 'N/A')}"
        ),
        impact=f"The '{scenario}' user flow cannot be completed successfully, blocking users from core functionality.",
        steps_to_reproduce=steps,
        expected_result=validation.get("expected", test_case.get("expected_result", "")),
        actual_result=validation.get("actual", execution_result.get("actual_result", "")),
        suggested_fix=validation.get("remediation_hint", "Investigate the failure step and fix the underlying issue."),
        screenshot_path=failure_sshot,
        source="test_runner",
        tc_id=tc_id,
        flow_id=test_case.get("flow_id"),
        run_id=run_id,
        playwright_snippet=test_case.get("playwright_snippet"),
    )


def generate_bugs_from_test_run(
    test_cases: list[dict],
    execution_results: list[dict],
    validations: list[dict],
    run_id: int,
) -> list[BugReport]:
    """Batch generator for all test failure bugs."""
    result_map = {r["tc_id"]: r for r in execution_results}
    validation_map = {v["tc_id"]: v for v in validations}

    bugs: list[BugReport] = []
    for tc in test_cases:
        tc_id = tc["tc_id"]
        er = result_map.get(tc_id, {})
        vr = validation_map.get(tc_id, {"verdict": "inconclusive"})
        bug = generate_bugs_from_test_failure(tc, er, vr, run_id)
        if bug:
            bugs.append(bug)

    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    bugs.sort(key=lambda b: sev_order.get(b.severity, 9))
    logger.info(f"[bug_reporter] Generated {len(bugs)} test-failure bugs from {len(test_cases)} test cases")
    return bugs