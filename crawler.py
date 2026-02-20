"""
crawler.py — GuardianAI Production
Full-spectrum QA crawler integrating all engine modules.
Extended: scan filters, ETA calculation, confidence scoring, AI learning fields.

FIX: All mutable state (visited, page_data, MAX_PAGES) is now scoped inside
     run_crawler() and passed explicitly to crawl_site(). No module-level
     globals — safe for concurrent RQ jobs in forked workers.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, UTC
from urllib.parse import urlparse, urlunparse

import pandas as pd
from playwright.async_api import async_playwright

from ai_analyzer import analyze_site
from engines.performance_engine import capture_performance_metrics, compute_performance_score
from engines.accessibility_engine import capture_accessibility_data, compute_accessibility_score
from engines.security_engine import capture_security_data, compute_security_score
from engines.scoring_engine import (
    compute_functional_score,
    compute_ui_form_score,
    compute_page_health_score,
    compute_site_health_score,
)
from engines.form_analyzer import analyze_all_forms
from confidence_engine import enrich_page_with_ai_fields, compute_run_confidence

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

os.makedirs("screenshots", exist_ok=True)
os.makedirs("reports", exist_ok=True)
os.makedirs("raw", exist_ok=True)

# Valid filter keys (read-only constant — safe at module level)
VALID_FILTERS = frozenset({
    "ui_elements", "form_validation", "functional",
    "accessibility", "performance", "security",
})


# ── URL Utilities ──────────────────────────────────────────────────────────────

def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    parsed = parsed._replace(fragment="")
    return urlunparse(parsed).rstrip("/")


def same_domain(base: str, url: str) -> bool:
    return urlparse(base).netloc == urlparse(url).netloc


# ── Filter Helper ──────────────────────────────────────────────────────────────

def _filter_active(active_filters, key: str) -> bool:
    if not active_filters:
        return True
    return key in active_filters


# ── ETA Tracker ───────────────────────────────────────────────────────────────

class ETATracker:
    def __init__(self):
        self.page_times: list[float] = []

    def record_page(self, elapsed_s: float):
        self.page_times.append(elapsed_s)

    def avg_time(self) -> float | None:
        if not self.page_times:
            return None
        return sum(self.page_times) / len(self.page_times)

    def eta_seconds(self, remaining: int) -> float | None:
        avg = self.avg_time()
        if avg is None or remaining <= 0:
            return None
        return round(avg * remaining, 1)


# ── DOM Intelligence Layer ─────────────────────────────────────────────────────

async def capture_dom_elements(page) -> dict:
    """
    Deep DOM inspection: UI elements, forms, pagination, dropdowns,
    accordion, tabs, modals, breadcrumbs, sidebar, navigation.
    """
    try:
        data = await page.evaluate("""() => {
            const ui_elements = [];
            const interactive = document.querySelectorAll(
                'button, a[href], input, select, textarea, [role="button"], [role="link"]'
            );
            // Build a minimal XPath for an element (id-based or positional)
            function getXPath(el) {
                if (el.id) return '//*[@id="' + el.id + '"]';
                const parts = [];
                let cur = el;
                while (cur && cur.nodeType === 1) {
                    let idx = 1;
                    let sib = cur.previousSibling;
                    while (sib) { if (sib.nodeType === 1 && sib.tagName === cur.tagName) idx++; sib = sib.previousSibling; }
                    parts.unshift(cur.tagName.toLowerCase() + (idx > 1 ? '[' + idx + ']' : ''));
                    cur = cur.parentNode;
                    if (cur === document.body) { parts.unshift('body'); break; }
                }
                return '/' + parts.join('/');
            }

            interactive.forEach(el => {
                ui_elements.push({
                    tag:   el.tagName.toLowerCase(),
                    type:  el.type || el.getAttribute('role') || null,
                    text:  (el.innerText || el.value || el.placeholder || '').substring(0, 80),
                    id:    el.id || null,
                    name:  el.name || null,
                    href:  el.href || null,
                    xpath: getXPath(el),
                    visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
                });
            });

            // Forms
            const forms = [];
            document.querySelectorAll('form').forEach(form => {
                const fields = [];
                form.querySelectorAll('input, select, textarea, button').forEach(f => {
                    fields.push({
                        tag:      f.tagName.toLowerCase(),
                        type:     f.type || null,
                        name:     f.name || null,
                        id:       f.id || null,
                        required: f.required,
                        label:    (document.querySelector('label[for="' + f.id + '"]') || {}).innerText || null,
                    });
                });
                forms.push({
                    action:      form.action || '',
                    method:      form.method || 'get',
                    field_count: fields.length,
                    fields:      fields,
                });
            });

            // Navigation / dropdowns / tabs / modals / etc.
            const nav_menus    = [...document.querySelectorAll('nav, [role="navigation"]')].map(n => ({id: n.id || null, items: n.querySelectorAll('a').length}));
            const dropdowns    = [...document.querySelectorAll('select, [role="listbox"], .dropdown')].map(d => ({id: d.id || null}));
            const tabs         = [...document.querySelectorAll('[role="tab"], .tab')].map(t => ({text: (t.innerText || '').substring(0, 60)}));
            const modals       = [...document.querySelectorAll('[role="dialog"], .modal, [aria-modal="true"]')].map(m => ({id: m.id || null}));
            const accordions   = [...document.querySelectorAll('details, [role="region"]')].map(a => ({id: a.id || null}));
            const pagination   = [...document.querySelectorAll('[aria-label*="paginat"], .pagination, [class*="paginat"]')].map(p => ({id: p.id || null}));
            const breadcrumbs_el = document.querySelector('[aria-label*="breadcrumb"], .breadcrumb, nav[aria-label]');
            const sidebar_el     = document.querySelector('aside, [role="complementary"], .sidebar');

            const images  = document.querySelectorAll('img').length;
            const buttons = document.querySelectorAll('button, [role="button"]').length;
            const links   = document.querySelectorAll('a[href]').length;

            return {
                ui_elements,
                ui_summary: { images, buttons, links },
                forms,
                nav_menus,
                dropdowns,
                tabs,
                modals,
                accordions,
                pagination,
                breadcrumbs: { found: !!breadcrumbs_el },
                sidebar:     { found: !!sidebar_el },
            };
        }""")
        return data
    except Exception as e:
        logger.warning(f"capture_dom_elements failed: {e}")
        return {}


# ── Main Crawl Loop ────────────────────────────────────────────────────────────

async def crawl_site(
    context,
    base_url: str,
    run_id: int,
    visited: set,
    page_data: list,
    max_pages: int | None,
    active_filters: list | None = None,
    update_fn=None,
):
    """
    BFS crawler. All mutable state (visited, page_data) is passed in
    explicitly — no module-level globals.
    """
    queue = [normalize_url(base_url)]
    eta_tracker = ETATracker()

    while queue:
        if max_pages and len(page_data) >= max_pages:
            break

        current_url = queue.pop(0)
        if current_url in visited:
            continue
        if not same_domain(base_url, current_url):
            continue

        visited.add(current_url)
        page_start = time.time()
        discovered_count = len(visited) + len(queue)

        logger.info(f"Scanning [{len(page_data)+1}/{max_pages or '?'}]: {current_url}")

        page = await context.new_page()
        try:
            failed_requests = []
            js_errors       = []
            page.on("requestfailed", lambda req: failed_requests.append(req.url))
            page.on("pageerror",     lambda err: js_errors.append(str(err)))

            error_responses  = []
            redirect_count   = [0]

            def handle_response(response):
                if 300 <= response.status < 400:
                    redirect_count[0] += 1
                if response.status >= 400:
                    ct = response.headers.get("content-type") or ""
                    if "text/html" in ct:
                        error_responses.append({"url": response.url, "status": response.status})

            page.on("response", handle_response)

            # Navigate with graceful fallback
            try:
                response = await page.goto(current_url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                try:
                    response = await page.goto(current_url, wait_until="load", timeout=30000)
                except Exception:
                    response = await page.goto(current_url, wait_until="commit", timeout=15000)

            await page.wait_for_timeout(2000)

            # ── Engine execution gated by active filters ──
            dom_data    = {}
            perf_raw    = {}
            a11y_raw    = {}
            security_raw = {}

            if _filter_active(active_filters, "ui_elements") or \
               _filter_active(active_filters, "form_validation") or \
               not active_filters:
                dom_data = await capture_dom_elements(page)

            if _filter_active(active_filters, "performance"):
                perf_raw = await capture_performance_metrics(page)

            if _filter_active(active_filters, "accessibility"):
                a11y_raw = await capture_accessibility_data(page)

            if _filter_active(active_filters, "security"):
                security_raw = await capture_security_data(page, response, current_url)

            title  = await page.title()
            status = response.status if response else 0

            screenshot_path = f"screenshots/run_{run_id}_{int(time.time()*1000)}.png"
            try:
                await page.screenshot(path=screenshot_path, full_page=True)
            except Exception:
                screenshot_path = None

            links = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
            internal_links = list(set(
                normalize_url(l) for l in links
                if same_domain(base_url, l)
            ))
            for link in internal_links:
                if link not in visited and link not in queue:
                    queue.append(link)

            # ── Compute scores ──
            perf_score_data  = compute_performance_score(perf_raw)  if perf_raw  else {"score": None, "grade": None, "slow_indicators": []}
            a11y_score_data  = compute_accessibility_score(a11y_raw) if a11y_raw  else {"score": None, "risk_level": None, "wcag_violations": []}
            sec_score_data   = compute_security_score(security_raw)  if security_raw else {"score": None, "risk_level": None}
            analyzed_forms   = analyze_all_forms(dom_data.get("forms") or []) if dom_data else []

            # Broken link check (error responses from this page's resources)
            broken_links_raw = [e["url"] for e in error_responses]

            page_object = {
                "url":        current_url,
                "title":      title,
                "timestamp":  datetime.now(UTC).isoformat(),
                "status":     status,
                "result":     "pass" if status == 200 else "fail",

                # DOM & UI
                "ui_elements": dom_data.get("ui_elements", []),
                "ui_summary":  dom_data.get("ui_summary", {}),
                "forms":       analyzed_forms,
                "dropdowns":   dom_data.get("dropdowns", []),
                "pagination":  dom_data.get("pagination", []),
                "nav_menus":   dom_data.get("nav_menus", []),
                "tabs":        dom_data.get("tabs", []),
                "modals":      dom_data.get("modals", []),
                "accordions":  dom_data.get("accordions", []),
                "breadcrumbs": dom_data.get("breadcrumbs", {}),
                "sidebar":     dom_data.get("sidebar", {}),

                # Topology — internal links found on this page (drives the site map graph)
                "connected_pages": internal_links,

                # Performance
                "performance_metrics": perf_raw,
                "performance_score":   perf_score_data.get("score"),
                "performance_grade":   perf_score_data.get("grade"),
                "load_time": (perf_raw or {}).get("load_event_end_ms", 0) / 1000 if perf_raw else None,
                "fcp_ms":    (perf_raw or {}).get("fcp_ms"),
                "lcp_ms":    (perf_raw or {}).get("lcp_ms"),
                "ttfb_ms":   (perf_raw or {}).get("ttfb_ms"),

                # Accessibility
                "accessibility_data":   a11y_raw,
                "accessibility_score":  a11y_score_data.get("score"),
                "accessibility_risk":   a11y_score_data.get("risk_level"),
                "accessibility_issues": (a11y_raw or {}).get("total_issues", 0),

                # Security
                "security_data":  security_raw,
                "security_score": sec_score_data.get("score"),
                "security_risk":  sec_score_data.get("risk_level"),
                "is_https":       (security_raw or {}).get("is_https"),

                # Functional
                "broken_links":          broken_links_raw,
                "js_errors":             js_errors,
                "failed_requests":       failed_requests,
                "redirect_chain_length": redirect_count[0],

                # Screenshot
                "screenshot": screenshot_path,
            }

            # Component scores
            if _filter_active(active_filters, "functional"):
                func_score_data = compute_functional_score(page_object)
                page_object["functional_score"] = func_score_data.get("score")
            else:
                page_object["functional_score"] = None

            if _filter_active(active_filters, "form_validation") or \
               _filter_active(active_filters, "ui_elements"):
                ui_form_score_data = compute_ui_form_score(page_object)
                page_object["ui_form_score"] = ui_form_score_data.get("score")
            else:
                page_object["ui_form_score"] = None

            # Composite health score
            health_data = compute_page_health_score(
                performance_score=page_object["performance_score"],
                accessibility_score=page_object["accessibility_score"],
                security_score=page_object["security_score"],
                functional_score=page_object["functional_score"],
                ui_form_score=page_object["ui_form_score"],
            )
            page_object["health_score"]     = health_data.get("health_score")
            page_object["risk_category"]    = health_data.get("risk_category")
            page_object["health_breakdown"] = health_data

            # Confidence + AI learning fields
            enrich_page_with_ai_fields(page_object, active_filters)

            page_data.append(page_object)

            # ETA update
            elapsed   = time.time() - page_start
            eta_tracker.record_page(elapsed)
            remaining = len(queue)
            if max_pages:
                remaining = max(0, max_pages - len(page_data))
            avg_ms = round((eta_tracker.avg_time() or 0) * 1000, 1)
            eta_s  = eta_tracker.eta_seconds(remaining)

            if update_fn:
                try:
                    update_fn(
                        scanned=len(page_data),
                        total=max_pages or (len(page_data) + len(queue)),
                        discovered=discovered_count,
                        avg_ms=avg_ms,
                        eta_seconds=eta_s,
                    )
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Error crawling {current_url}: {e}")
        finally:
            await page.close()


# ── Entry Point ────────────────────────────────────────────────────────────────

async def main(
    run_id:       int,
    start_url:    str,
    user_id:      int,
    page_limit=None,
    update_fn=None,
    active_filters: list | None = None,
):
    """
    Entry point called by tasks.py via asyncio.run().
    All mutable crawl state is local to this call — fully isolated per job.
    """
    # ── Scoped state — NOT module-level globals ──
    visited:   set  = set()
    page_data: list = []
    max_pages: int | None = int(page_limit) if page_limit and str(page_limit).isdigit() else None

    # Validate + normalise filters
    # "accessibility_audit" is the key used by confidence_engine; map it to "accessibility"
    FILTER_ALIASES = {"accessibility_audit": "accessibility"}
    if active_filters:
        active_filters = [
            FILTER_ALIASES.get(f, f)
            for f in active_filters
            if FILTER_ALIASES.get(f, f) in VALID_FILTERS
        ] or None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="GuardianAI/4.0 QA Bot",
        )
        await crawl_site(
            context,
            start_url,
            run_id,
            visited=visited,
            page_data=page_data,
            max_pages=max_pages,
            active_filters=active_filters,
            update_fn=update_fn,
        )
        await browser.close()

    # ── Build Excel report ──
    rows = []
    for pg in page_data:
        ui_s = pg.get("ui_summary") or {}
        rows.append({
            "URL":                  pg["url"],
            "Title":                pg["title"],
            "Status":               pg["status"],
            "Result":               pg["result"],
            "Load Time (s)":        pg.get("load_time"),
            "FCP (ms)":             pg.get("fcp_ms"),
            "LCP (ms)":             pg.get("lcp_ms"),
            "TTFB (ms)":            pg.get("ttfb_ms"),
            "Performance Score":    pg.get("performance_score"),
            "Accessibility Score":  pg.get("accessibility_score"),
            "Accessibility Issues": pg.get("accessibility_issues"),
            "Accessibility Risk":   pg.get("accessibility_risk"),
            "Security Score":       pg.get("security_score"),
            "Security Risk":        pg.get("security_risk"),
            "Is HTTPS":             pg.get("is_https"),
            "Functional Score":     pg.get("functional_score"),
            "UI/Form Score":        pg.get("ui_form_score"),
            "Health Score":         pg.get("health_score"),
            "Risk Category":        pg.get("risk_category"),
            "Confidence Score":     pg.get("confidence_score"),
            "Failure Pattern ID":   pg.get("failure_pattern_id"),
            "Root Cause Tag":       pg.get("root_cause_tag"),
            "Broken Links":         len(pg.get("broken_links") or []),
            "JS Errors":            len(pg.get("js_errors") or []),
            "Redirect Chain":       pg.get("redirect_chain_length", 0),
            "Forms Count":          len(pg.get("forms") or []),
            "Buttons":              ui_s.get("buttons", 0),
            "Links":                ui_s.get("links", 0),
            "Images":               ui_s.get("images", 0),
            "Elements Found":       len(pg.get("ui_elements") or []),
            "Screenshot":           pg.get("screenshot"),
        })

    df = pd.DataFrame(rows)
    timestamp   = int(time.time())
    report_file = f"reports/qa_report_{timestamp}.xlsx"
    df.to_excel(report_file, index=False)

    raw_file = f"raw/qa_raw_{timestamp}.json"
    with open(raw_file, "w", encoding="utf-8") as f:
        json.dump(page_data, f, default=str)

    summary_file = f"reports/ai_summary_{timestamp}.txt"
    with open(summary_file, "w", encoding="utf-8") as f:
        f.write(analyze_site(page_data))

    page_health_list = [pg.get("health_breakdown", {}) for pg in page_data]
    site_health      = compute_site_health_score(page_health_list)
    run_confidence   = compute_run_confidence(page_data, active_filters)
    site_health["confidence_score"] = run_confidence

    site_summary_file = f"reports/site_health_{timestamp}.json"
    with open(site_summary_file, "w", encoding="utf-8") as f:
        json.dump(site_health, f)

    total  = len(page_data)
    passed = sum(1 for pg in page_data if pg["result"] == "pass")
    logger.info(f"Scan complete → {total} pages, {passed} passed, confidence={run_confidence}")

    return {
        "total":             total,
        "passed":            passed,
        "failed":            total - passed,
        "report_file":       report_file,
        "summary_file":      summary_file,
        "raw_file":          raw_file,
        "site_summary_file": site_summary_file,
        "scanned_pages":     total,
        "site_health":       site_health,
        "confidence_score":  run_confidence,
        "active_filters":    active_filters,
    }