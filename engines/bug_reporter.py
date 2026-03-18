"""
engines/bug_reporter.py — GuardianAI Autonomous QA  (v2)
Bug Report Generator: two modes of operation:

1. PASSIVE MODE (scan findings) — converts page_object raw findings into
   structured bug reports. No test execution needed.

2. ACTIVE MODE (test failures) — converts ValidationResult + TestCaseResult
   into structured bug reports with reproduction steps and Playwright snippets.

KEY CHANGES v2:
  - Cross-page deduplication: identical rule hits across pages are merged into
    a single bug report with an "affected_pages" list. Eliminates the repeated
    "Page Served Over HTTP" bug per page pattern.
  - JS error reports now include message + stack trace + source location
    (sourced from the enriched js_errors[] dict format from crawler v2).
  - Accessibility reports list the specific violation categories found
    (missing alt, unlabeled inputs, empty links, etc.).
  - Security reports include the full list of affected URLs in description
    when the same issue spans multiple pages.
  - _make_sitewide_title() rewrites per-page titles to site-level titles.
  - Bug fingerprinting uses rule_id + severity for stable cross-page grouping.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
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
    component: Optional[str]
    description: str
    impact: str
    steps_to_reproduce: list[str]
    expected_result: str
    actual_result: str
    suggested_fix: str
    screenshot_path: Optional[str] = None
    source: str = "scan"
    tc_id: Optional[str] = None
    flow_id: Optional[str] = None
    run_id: Optional[int] = None
    playwright_snippet: Optional[str] = None
    # v2: list of all affected page URLs (populated by deduplication pass)
    affected_pages: list[str] = field(default_factory=list)

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
            "affected_pages":     self.affected_pages,
        }


# ── Helpers ────────────────────────────────────────────────────────────────────

def _path(url: str) -> str:
    return urlparse(url).path or "/"


_CDN_HOSTS = [
    "cdn.jsdelivr.net", "cdnjs.cloudflare.com", "googleapis.com",
    "gstatic.com", "unpkg.com", "ajax.googleapis.com",
]

def _non_cdn_js_errors(js_errors: list) -> list:
    """Returns only JS errors that are not external CDN 404s."""
    result = []
    for e in js_errors:
        msg = e.get("message", str(e)) if isinstance(e, dict) else str(e)
        if not any(cdn in msg for cdn in _CDN_HOSTS):
            result.append(e)
    return result


def _js_error_summary(js_errors: list) -> str:
    """
    Formats JS errors for bug description.
    Handles both legacy string format and v2 dict format
    {message, stack, source, location}.
    Filters out external CDN 404s — not actionable by the site owner.
    """
    # Filter out external CDN 404s
    filtered = _non_cdn_js_errors(js_errors)
    # Use filtered list if it has items, otherwise keep all (avoid empty report)
    if filtered:
        js_errors = filtered

    if not js_errors:
        return "No JS errors recorded."

    lines = []
    for i, err in enumerate(js_errors[:5], 1):
        if isinstance(err, dict):
            msg      = err.get("message", "Unknown error")[:200]
            stack    = err.get("stack")
            source   = err.get("source", "pageerror")
            location = err.get("location")

            line = f"  {i}. [{source}] {msg}"
            if location:
                line += f"\n     Location: {location}"
            if stack and stack != msg:
                # Show first 2 lines of stack trace
                stack_lines = [s.strip() for s in stack.split("\n") if s.strip()][:2]
                if stack_lines:
                    line += "\n     Stack: " + " | ".join(stack_lines)
            lines.append(line)
        else:
            # Legacy: plain string
            lines.append(f"  {i}. {str(err)[:200]}")

    if len(js_errors) > 5:
        lines.append(f"  ... and {len(js_errors) - 5} more errors")

    return "\n".join(lines)


def _a11y_violation_summary(a11y_data: dict) -> str:
    """
    Formats accessibility violation details from the accessibility engine output.
    Groups violations by category and reports counts.
    """
    if not a11y_data:
        return "Accessibility data not available."

    issues = a11y_data.get("issues") or []
    if not issues:
        total = a11y_data.get("total_issues", 0)
        return f"{total} accessibility violations detected (run axe DevTools for details)."

    # Group by category
    by_category: dict[str, list] = defaultdict(list)
    for issue in issues:
        cat = issue.get("category", "unknown")
        by_category[cat].append(issue)

    category_labels = {
        "missing_alt":       "Images missing alt text",
        "unlabeled_input":   "Form inputs without labels",
        "unnamed_button":    "Buttons without accessible names",
        "empty_link":        "Links without accessible text",
        "heading_hierarchy": "Heading hierarchy issues",
        "color_contrast":    "Insufficient color contrast",
        "missing_lang":      "Missing language attribute",
        "aria_role":         "Invalid ARIA roles",
    }

    lines = []
    for cat, cat_issues in sorted(by_category.items()):
        label = category_labels.get(cat, cat.replace("_", " ").title())
        sev   = (cat_issues[0].get("severity") or "medium") if cat_issues else "medium"
        lines.append(f"  • [{sev.upper()}] {label}: {len(cat_issues)} instance(s)")
        # Show first example
        if cat_issues and cat_issues[0].get("element"):
            el = cat_issues[0]["element"][:80]
            lines.append(f"    Example: {el}")

    return "\n".join(lines) if lines else f"{len(issues)} violations detected."


# ── Scan Rules ─────────────────────────────────────────────────────────────────
#
# Each rule dict:
#   id         — stable string key, used for deduplication
#   check      — lambda(page) → bool
#   severity   — critical|high|medium|low
#   bug_type   — performance|security|accessibility|functional|navigation
#   title      — str or lambda(page) → str
#   description— str or lambda(page) → str
#   impact     — str
#   fix        — str
#   steps      — list[str] or lambda(page) → list[str]
#   expected   — str
#   actual     — str or lambda(page) → str

_SCAN_RULES = [

    # ── Critical ──────────────────────────────────────────────────────────────

    {
        "id": "http_5xx",
        "check": lambda p: (p.get("status") or 200) >= 500,
        "severity": "critical",
        "bug_type": "functional",
        "title": lambda p: f"Server Error ({p['status']}) on {_path(p['url'])}",
        "description": lambda p: (
            f"The page at {p['url']} returned HTTP {p['status']}, "
            f"indicating a server-side failure. Users cannot load this page."
        ),
        "impact": "Page is completely inaccessible. Users receive an error page. All downstream links from this page are effectively broken.",
        "fix": "Check server logs for stack traces. Ensure all required services (database, cache, API) are running. Add error monitoring alerts.",
        "steps": lambda p: [f"Navigate to {p['url']}", "Observe HTTP 5xx error response"],
        "expected": "Page loads with HTTP 200",
        "actual": lambda p: f"Server returned HTTP {p['status']}",
    },
    {
        "id": "js_errors",
        "check": lambda p: bool(_non_cdn_js_errors(p.get("js_errors") or [])),
        "severity": "medium",
        "bug_type": "functional",
        "title": lambda p: f"JavaScript Errors Detected ({len(_non_cdn_js_errors(p['js_errors']))} error{'s' if len(_non_cdn_js_errors(p['js_errors'])) != 1 else ''}) — {_path(p['url'])}",
        "description": lambda p: (
            f"JavaScript errors were detected on {p['url']} during scan.\n\n"
            f"Error details:\n{_js_error_summary(p['js_errors'])}"
        ),
        "impact": "Script errors can prevent UI from rendering correctly, disable interactive features, or cause data loss. Users may see a broken or partially functional page.",
        "fix": "Open browser DevTools Console on the affected page. Reproduce the errors and fix the root cause (undefined variables, failed network requests, syntax errors, etc.).",
        "steps": lambda p: [
            f"Open {p['url']} in browser",
            "Open DevTools → Console tab",
            "Reload page and observe error messages",
            "Fix each error in the source code",
        ],
        "expected": "No JavaScript errors in browser console",
        "actual": lambda p: f"{len(_non_cdn_js_errors(p['js_errors']))} JavaScript error(s) detected",
    },
    {
        "id": "http_no_tls",
        "check": lambda p: p.get("is_https") is False,
        "severity": "critical",
        "bug_type": "security",
        "title": "Page Served Over HTTP (No TLS Encryption)",
        "description": (
            "This page is served over unencrypted HTTP. All data exchanged between "
            "the user and server — including form inputs, cookies, and session tokens — "
            "is transmitted in plaintext and can be intercepted by a network attacker (MITM)."
        ),
        "impact": "Login credentials, personal data, and session tokens can be stolen. Modern browsers show 'Not Secure' warnings. SEO rankings penalised. GDPR/PCI-DSS compliance risk.",
        "fix": "Install a TLS certificate (free via Let's Encrypt). Configure web server to redirect all HTTP traffic to HTTPS. Set HSTS header: Strict-Transport-Security: max-age=31536000.",
        "steps": lambda p: [
            f"Navigate to {p['url']}",
            "Check browser address bar — padlock icon is absent or shows 'Not Secure'",
            "Inspect network traffic to confirm HTTP protocol",
        ],
        "expected": "All pages served over HTTPS with valid TLS certificate",
        "actual": lambda p: f"Page served over HTTP: {p['url']}",
    },

    # ── High ──────────────────────────────────────────────────────────────────

    {
        "id": "http_4xx",
        "check": lambda p: 400 <= (p.get("status") or 200) < 500,
        "severity": "high",
        "bug_type": "functional",
        "title": lambda p: f"Client Error ({p['status']}) — {_path(p['url'])}",
        "description": lambda p: (
            f"The page at {p['url']} returned HTTP {p['status']}. "
            f"{'Page not found — the URL may have changed or been deleted.' if p['status'] == 404 else ''}"
            f"{'Access denied — page requires authentication or different permissions.' if p['status'] in (401, 403) else ''}"
        ),
        "impact": "Users encounter an error page instead of content. Links pointing to this page are broken. SEO impact from 404 errors.",
        "fix": "For 404: implement a redirect from the old URL to the new one. For 403: verify authentication and permission logic. Check server routing configuration.",
        "steps": lambda p: [f"Navigate to {p['url']}", f"Observe HTTP {p['status']} response"],
        "expected": "Page loads with HTTP 200",
        "actual": lambda p: f"Server returned HTTP {p['status']}",
    },
    {
        "id": "broken_nav_links",
        "check": lambda p: bool(p.get("broken_navigation_links") or p.get("broken_links")),
        "severity": "high",
        "bug_type": "navigation",
        "title": lambda p: (
            f"Broken Navigation Links ({len(p.get('broken_navigation_links') or p.get('broken_links') or [])}) — {_path(p['url'])}"
        ),
        "description": lambda p: (
            f"The following navigation links on {p['url']} return errors:\n"
            + "\n".join(
                f"  • {lnk['url']} → HTTP {lnk.get('status', 'timeout/error')}"
                for lnk in (p.get("broken_navigation_links") or p.get("broken_links") or [])[:5]
            )
            + ("\n  (and more...)" if len(p.get("broken_navigation_links") or []) > 5 else "")
        ),
        "impact": "Users click links and land on error pages. Internal link equity is lost. Critical user flows are blocked. Crawlability reduced.",
        "fix": "Audit all internal links. Update or remove links pointing to deleted/moved pages. Implement 301 redirects for moved content. Add automated link checking to CI pipeline.",
        "steps": lambda p: [
            f"Open {p['url']}",
            "Click each navigation link",
            "Verify each link returns HTTP 200",
        ],
        "expected": "All navigation links return HTTP 200",
        "actual": lambda p: f"{len(p.get('broken_navigation_links') or p.get('broken_links') or [])} broken link(s) found",
    },
    {
        "id": "high_accessibility",
        "check": lambda p: (p.get("accessibility_issues") or 0) > 5,
        "severity": "high",
        "bug_type": "accessibility",
        "title": lambda p: f"High Accessibility Violations ({p['accessibility_issues']}) — {_path(p['url'])}",
        "description": lambda p: (
            f"The page at {p['url']} has {p['accessibility_issues']} accessibility violations "
            f"that violate WCAG 2.1 guidelines.\n\n"
            + _a11y_violation_summary(p.get("accessibility_data") or {})
        ),
        "impact": "Users with disabilities (visual, motor, cognitive) may be unable to use this page. Legal liability under ADA, Section 508, and EU Accessibility Act.",
        "fix": "Install axe DevTools browser extension and run a scan. Prioritise: (1) images missing alt text, (2) unlabeled form inputs, (3) insufficient color contrast, (4) keyboard navigation.",
        "steps": lambda p: [
            "Install axe DevTools browser extension",
            f"Navigate to {p['url']}",
            "Click 'Analyze' in axe DevTools panel",
            "Review and fix all violations in priority order",
        ],
        "expected": "Zero critical/high accessibility violations (WCAG 2.1 AA compliance)",
        "actual": lambda p: f"{p['accessibility_issues']} violations detected",
    },
    {
        "id": "slow_ttfb",
        "check": lambda p: (p.get("ttfb_ms") or 0) > 800,
        "severity": "high",
        "bug_type": "performance",
        "title": lambda p: f"Slow Server Response Time (TTFB {p['ttfb_ms']}ms) — {_path(p['url'])}",
        "description": lambda p: (
            f"Time To First Byte on {p['url']} is {p['ttfb_ms']}ms, "
            f"which exceeds the 800ms threshold for a good user experience. "
            f"TTFB measures how long the server takes to start sending a response."
        ),
        "impact": "Users perceive the site as slow before the page even starts loading. Google Core Web Vitals TTFB target is <800ms (good <200ms). Bounce rate increases significantly above 1s.",
        "fix": "Profile server-side code for slow queries. Enable page/query caching. Check database connection pool exhaustion. Consider CDN for static assets. Review hosting tier capacity.",
        "steps": lambda p: [
            "Open Chrome DevTools → Network tab",
            f"Navigate to {p['url']}",
            "Click on the document request (first row)",
            "Check TTFB under Timing tab",
        ],
        "expected": "TTFB under 200ms (excellent) or under 800ms (acceptable)",
        "actual": lambda p: f"TTFB is {p['ttfb_ms']}ms",
    },

    # ── Medium ─────────────────────────────────────────────────────────────────

    {
        "id": "slow_lcp",
        "check": lambda p: (p.get("lcp_ms") or 0) > 2500,
        "severity": "medium",
        "bug_type": "performance",
        "title": lambda p: f"Poor Largest Contentful Paint (LCP {p['lcp_ms']}ms) — {_path(p['url'])}",
        "description": lambda p: (
            f"Largest Contentful Paint on {p['url']} is {p['lcp_ms']}ms. "
            f"Google considers LCP > 2500ms as 'needs improvement' and > 4000ms as 'poor'. "
            f"LCP measures when the main content becomes visible to the user."
        ),
        "impact": "Poor LCP is a Core Web Vitals failure. Google uses CWV as a ranking signal. Users perceive the page as slow to load. High bounce rates expected.",
        "fix": "Optimise the largest element (hero image, heading, or block). Use lazy loading for below-fold images. Preload critical assets. Reduce render-blocking CSS/JS.",
        "steps": lambda p: [
            "Open Chrome DevTools → Lighthouse tab",
            f"Run audit on {p['url']}",
            "Review LCP element identified in report",
            "Optimise image size, format (WebP), and loading strategy",
        ],
        "expected": "LCP under 2500ms",
        "actual": lambda p: f"LCP is {p['lcp_ms']}ms",
    },
    {
        "id": "missing_meta_desc",
        "check": lambda p: (p.get("security_data") or {}).get("missing_meta_description") is True,
        "severity": "medium",
        "bug_type": "functional",
        "title": "Missing Meta Description",
        "description": lambda p: (
            f"The page at {p['url']} is missing a <meta name='description'> tag. "
            f"Search engines use this as the snippet in search results."
        ),
        "impact": "Search engines generate arbitrary snippets, which can reduce CTR. Missing descriptions are a basic on-page SEO failure.",
        "fix": "Add a unique, descriptive <meta name='description' content='...'> tag (150-160 chars) to every page.",
        "steps": lambda p: [
            f"Open {p['url']}",
            "View Page Source (Ctrl+U)",
            "Search for 'meta name=\"description\"'",
            "Observe it is absent",
        ],
        "expected": "Every page has a unique meta description of 50-160 characters",
        "actual": lambda p: f"Meta description absent on {p['url']}",
    },
    {
        "id": "moderate_accessibility",
        "check": lambda p: 1 <= (p.get("accessibility_issues") or 0) <= 5,
        "severity": "medium",
        "bug_type": "accessibility",
        "title": lambda p: f"Accessibility Violations ({p['accessibility_issues']}) — {_path(p['url'])}",
        "description": lambda p: (
            f"The page at {p['url']} has {p['accessibility_issues']} accessibility violations.\n\n"
            + _a11y_violation_summary(p.get("accessibility_data") or {})
        ),
        "impact": "Some users relying on assistive technologies may encounter issues. WCAG 2.1 AA compliance requires zero violations.",
        "fix": "Address each violation reported. Common fixes: add alt text to images, associate labels with inputs, ensure sufficient color contrast.",
        "steps": lambda p: [
            f"Navigate to {p['url']}",
            "Run axe DevTools or WAVE accessibility checker",
            "Fix each reported violation",
        ],
        "expected": "Zero accessibility violations",
        "actual": lambda p: f"{p['accessibility_issues']} violation(s) found",
    },

    # ── Low ───────────────────────────────────────────────────────────────────

    {
        "id": "moderate_ttfb",
        "check": lambda p: 400 <= (p.get("ttfb_ms") or 0) <= 800,
        "severity": "low",
        "bug_type": "performance",
        "title": lambda p: f"Moderate Server Response Time (TTFB {p['ttfb_ms']}ms) — {_path(p['url'])}",
        "description": lambda p: (
            f"TTFB is {p['ttfb_ms']}ms on {p['url']}. Acceptable but approaching "
            f"the poor threshold (800ms). Optimisation recommended before traffic scales."
        ),
        "impact": "Minor user experience impact now; may become high severity under increased load.",
        "fix": "Consider enabling HTTP caching headers, connection pooling, and query optimisation to stay ahead of performance degradation.",
        "steps": lambda p: [
            "Open DevTools Network tab",
            f"Navigate to {p['url']}",
            "Measure TTFB on document request",
        ],
        "expected": "TTFB under 200ms",
        "actual": lambda p: f"TTFB is {p['ttfb_ms']}ms",
    },
]


# ── Per-Page Bug Generation ────────────────────────────────────────────────────

def generate_bugs_from_page(page: dict, run_id: int) -> list[BugReport]:
    """
    Generates BugReport objects from a single page_object dict.
    Each rule fires at most once per page (seen_ids guard).
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
            title       = rule["title"](page)       if callable(rule["title"])       else rule["title"]
            description = rule["description"](page) if callable(rule["description"]) else rule["description"]
            actual      = rule["actual"](page)      if callable(rule["actual"])      else rule["actual"]
            steps       = rule["steps"](page)       if callable(rule["steps"])       else rule["steps"]

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


# ── Cross-Page Deduplication ───────────────────────────────────────────────────

def _make_sitewide_title(rule_id: str, title: str, count: int) -> str:
    """
    Rewrites a per-page bug title into a site-wide issue title.
    Only applies to rules that naturally repeat across many pages.
    """
    sitewide_map = {
        "http_no_tls":       f"Application Does Not Enforce HTTPS ({count} pages affected)",
        "missing_meta_desc": f"Missing Meta Descriptions Site-Wide ({count} pages)",
        "js_errors":         f"JavaScript Errors Across {count} Pages",
        "high_accessibility":f"High Accessibility Violations Across {count} Pages",
        "moderate_accessibility": f"Accessibility Violations Across {count} Pages",
        "broken_nav_links":  f"Broken Navigation Links Across {count} Pages",
        "slow_ttfb":         f"Slow Server Response Time on {count} Pages",
        "moderate_ttfb":     f"Moderate Server Response Time on {count} Pages",
    }
    return sitewide_map.get(rule_id, f"{title} ({count} pages affected)")


def _deduplicate_bugs(bugs: list[BugReport]) -> list[BugReport]:
    """
    Merges duplicate bug reports across pages.

    Grouping key: rule_id is embedded in bug_title via a stable prefix per rule.
    We use bug_type + severity + normalised title as the grouping fingerprint.

    For groups with 2+ bugs:
      - The first (canonical) bug is kept
      - Its description is updated to list all affected pages
      - Its title is rewritten to a site-wide form
      - affected_pages is populated with all URLs

    For unique bugs: returned as-is.
    """
    # Build fingerprint → list[BugReport]
    groups: dict[str, list[BugReport]] = defaultdict(list)

    for bug in bugs:
        # Normalise title: strip per-page specifics (URLs, counts, ms values)
        norm_title = re.sub(r"https?://\S+", "URL", bug.bug_title)
        norm_title = re.sub(r"\d+ms", "Xms", norm_title)
        norm_title = re.sub(r"\(\d+ error[s]?\)", "(N errors)", norm_title)
        norm_title = re.sub(r"\(\d+\)", "(N)", norm_title)
        norm_title = re.sub(r"— /\S*", "", norm_title).strip()

        key = f"{bug.bug_type}::{bug.severity}::{norm_title}"
        groups[key].append(bug)

    # Also extract rule_id from each bug for sitewide title lookup
    # We map norm_title back to rule_id via _SCAN_RULES title patterns
    rule_id_by_title: dict[str, str] = {}
    for rule in _SCAN_RULES:
        rule_id_by_title[rule["id"]] = rule["id"]

    def _guess_rule_id(bug: BugReport) -> str:
        """Match bug back to rule_id by bug_type+severity."""
        for rule in _SCAN_RULES:
            if rule["bug_type"] == bug.bug_type and rule["severity"] == bug.severity:
                return rule["id"]
        return ""

    merged: list[BugReport] = []

    for key, group in groups.items():
        if len(group) == 1:
            b = group[0]
            b.affected_pages = [b.page_url]
            merged.append(b)
            continue

        # Multi-page: merge into canonical (first) bug
        canonical     = group[0]
        affected_urls = list(dict.fromkeys(b.page_url for b in group))
        rule_id       = _guess_rule_id(canonical)

        # Rewrite title to site-wide form
        canonical.bug_title = _make_sitewide_title(
            rule_id, canonical.bug_title, len(affected_urls)
        )

        # Rewrite description to reference all affected pages
        url_list = "\n".join(f"  • {u}" for u in affected_urls[:10])
        more     = f"\n  ... and {len(affected_urls) - 10} more pages" if len(affected_urls) > 10 else ""
        canonical.description = (
            canonical.description.split("\n\n")[0]  # keep first paragraph
            + f"\n\n⚠️ This issue affects {len(affected_urls)} pages:\n{url_list}{more}"
        )

        canonical.affected_pages = affected_urls

        # Update page_url to the most representative URL (root if present)
        root_urls = [u for u in affected_urls if urlparse(u).path in ("", "/")]
        if root_urls:
            canonical.page_url = root_urls[0]

        merged.append(canonical)

    return merged


# ── Scan-Level Entry Point ─────────────────────────────────────────────────────

def generate_bugs_from_scan(page_data: list[dict], run_id: int) -> list[BugReport]:
    """
    Generate and deduplicate bugs from all pages in a scan.
    Returns deduplicated, severity-sorted bug reports.
    """
    all_bugs: list[BugReport] = []
    for page in page_data:
        all_bugs.extend(generate_bugs_from_page(page, run_id))

    before = len(all_bugs)
    all_bugs = _deduplicate_bugs(all_bugs)
    after   = len(all_bugs)

    if before != after:
        logger.info(
            f"[bug_reporter] Deduplication: {before} raw bugs → {after} unique bugs "
            f"({before - after} merged)"
        )

    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    all_bugs.sort(key=lambda b: sev_order.get(b.severity, 9))

    logger.info(
        f"[bug_reporter] {len(all_bugs)} bugs from {len(page_data)} pages "
        f"(critical={sum(1 for b in all_bugs if b.severity=='critical')}, "
        f"high={sum(1 for b in all_bugs if b.severity=='high')})"
    )
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

    tc_id    = test_case["tc_id"]
    scenario = test_case.get("scenario", "Unknown flow")

    sev_map  = {"critical": "critical", "high": "high", "medium": "medium", "low": "low"}
    severity = sev_map.get(test_case.get("severity"), "medium")

    steps = [
        f"Step {s['step_number']}: {s['description']}"
        for s in (test_case.get("steps") or [])
    ]

    failure_sshot = execution_result.get("screenshot_path")
    for sr in (execution_result.get("step_results") or []):
        if sr.get("status") in ("fail", "error") and sr.get("screenshot_path"):
            failure_sshot = sr["screenshot_path"]
            break

    bug_type_map = {
        "navigation":  "navigation",
        "interaction": "functional",
        "error":       "functional",
        "timeout":     "performance",
        "assertion":   "functional",
    }
    bug_type = bug_type_map.get(validation.get("failure_category"), "functional")

    entry_url = ""
    step_results = execution_result.get("step_results") or []
    if step_results:
        entry_url = step_results[0].get("actual_outcome", "")

    return BugReport(
        bug_title=f"{scenario} — Automated Test Failure",
        page_url=entry_url or execution_result.get("entry_url", "N/A"),
        bug_type=bug_type,
        severity=severity,
        component=None,
        description=(
            f"Automated test '{scenario}' (ID: {tc_id}) failed during execution.\n"
            f"Failure category: {validation.get('failure_category', 'unknown')}\n"
            f"Failure reason: {validation.get('failure_reason', 'N/A')}"
        ),
        impact=f"The '{scenario}' user flow cannot be completed, blocking users from core functionality.",
        steps_to_reproduce=steps,
        expected_result=validation.get("expected", test_case.get("expected_result", "")),
        actual_result=validation.get("actual", execution_result.get("actual_result", "")),
        suggested_fix=validation.get(
            "remediation_hint",
            "Investigate the failing step, check the selector validity, and verify the page state at that point."
        ),
        screenshot_path=failure_sshot,
        source="test_runner",
        tc_id=tc_id,
        flow_id=test_case.get("flow_id"),
        run_id=run_id,
        playwright_snippet=test_case.get("playwright_snippet"),
        affected_pages=[entry_url] if entry_url else [],
    )


def generate_bugs_from_test_run(
    test_cases: list[dict],
    execution_results: list[dict],
    validations: list[dict],
    run_id: int,
) -> list[BugReport]:
    """Batch generator for all test failure bugs."""
    result_map     = {r["tc_id"]: r for r in execution_results}
    validation_map = {v["tc_id"]: v for v in validations}

    bugs: list[BugReport] = []
    for tc in test_cases:
        tc_id = tc["tc_id"]
        er    = result_map.get(tc_id, {})
        vr    = validation_map.get(tc_id, {"verdict": "inconclusive"})
        bug   = generate_bugs_from_test_failure(tc, er, vr, run_id)
        if bug:
            bugs.append(bug)

    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    bugs.sort(key=lambda b: sev_order.get(b.severity, 9))
    logger.info(
        f"[bug_reporter] {len(bugs)} test-failure bugs from {len(test_cases)} test cases"
    )
    return bugs