"""
Crawler — GuardianAI Production
Full-spectrum QA crawler integrating all engine modules.
Extended: scan filters, ETA calculation, confidence scoring, AI learning fields.
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

# Module-level globals reset per run
visited = set()
page_data = []
MAX_PAGES = None

# Valid filter keys
VALID_FILTERS = {
    "ui_elements", "form_validation", "functional",
    "accessibility", "performance", "security"
}


# ── URL Utilities ──────────────────────────────────────────────────────────────

def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    parsed = parsed._replace(fragment="")
    return urlunparse(parsed).rstrip("/")


def same_domain(base: str, url: str) -> bool:
    return urlparse(base).netloc == urlparse(url).netloc


# ── DOM Intelligence Layer ─────────────────────────────────────────────────────

async def capture_dom_elements(page) -> dict:
    """
    Deep DOM inspection: UI elements, forms, pagination, dropdowns,
    accordion, tabs, modals, breadcrumbs, sidebar, navigation.
    """
    try:
        return await page.evaluate("""() => {
        const ui_elements = [];
        const elements = document.querySelectorAll('button, a, input, select, textarea');
        elements.forEach(el => {
            const rect = el.getBoundingClientRect();
            ui_elements.push({
                tag: el.tagName.toLowerCase(),
                type: el.type || 'clickable',
                text: (el.innerText || el.value || '').substring(0, 100),
                id: el.id || '',
                visible: rect.width > 0 && rect.height > 0 &&
                    window.getComputedStyle(el).visibility !== 'hidden' &&
                    window.getComputedStyle(el).display !== 'none',
                enabled: !el.disabled,
                required: el.required || false,
                position: { x: Math.round(rect.x), y: Math.round(rect.y) },
                attributes: {
                    placeholder: el.placeholder || '',
                    name: el.name || '',
                    class: (el.className || '').substring(0, 100),
                    aria_label: el.getAttribute('aria-label') || ''
                }
            });
        });

        const forms = [];
        const formSignatures = new Set();
        document.querySelectorAll('form').forEach(form => {
            const style = window.getComputedStyle(form);
            const rect = form.getBoundingClientRect();
            if (style.display === 'none' || style.visibility === 'hidden' ||
                rect.width === 0 || rect.height === 0) return;
            const inputs = Array.from(form.querySelectorAll('input, select, textarea'));
            const visibleFields = inputs.filter(i => {
                const fs = window.getComputedStyle(i);
                const fr = i.getBoundingClientRect();
                return i.type !== 'hidden' && fs.display !== 'none' &&
                    fs.visibility !== 'hidden' && fr.width > 0 && fr.height > 0;
            });
            if (visibleFields.length === 0) return;
            const signature = visibleFields
                .map(i => i.tagName + '-' + (i.name || i.type || 'unknown'))
                .sort().join('|');
            if (!formSignatures.has(signature)) {
                formSignatures.add(signature);
                forms.push({
                    action: form.action || '',
                    method: (form.method || 'GET').toUpperCase(),
                    fields: visibleFields.map(i => ({
                        tag: i.tagName.toLowerCase(),
                        type: i.type || '',
                        name: i.name || '',
                        required: i.required || false,
                        placeholder: i.placeholder || ''
                    })),
                    fields_count: visibleFields.length
                });
            }
        });

        const dropdowns = [];
        document.querySelectorAll('select').forEach(el => {
            dropdowns.push({
                type: 'native', trigger_tag: 'select',
                name: el.name || '', options_count: el.options.length,
                visible: el.offsetParent !== null,
                accessibility: (el.labels && el.labels.length) ? 'label-linked' : 'no-label'
            });
        });
        document.querySelectorAll('[aria-expanded]').forEach(trigger => {
            const tag = trigger.tagName.toLowerCase();
            if (!['button', 'a'].includes(tag)) return;
            const controls = trigger.getAttribute('aria-controls');
            let container = controls ? document.getElementById(controls) : null;
            dropdowns.push({
                type: 'structured', trigger_tag: tag,
                expanded: trigger.getAttribute('aria-expanded') === 'true',
                has_container: !!container,
                container_role: container ? (container.getAttribute('role') || '') : '',
                visible: trigger.offsetParent !== null
            });
        });

        const pagination = [];
        document.querySelectorAll('nav, ul, div').forEach(container => {
            const text = container.innerText || '';
            if (text.match(/\\b1\\b.*\\b2\\b/) ||
                text.toLowerCase().includes('next') ||
                text.toLowerCase().includes('prev')) {
                const links = Array.from(container.querySelectorAll('a'))
                    .map(a => ({ text: a.innerText.trim(), href: a.href }))
                    .filter(a => a.text.length > 0);
                if (links.length > 1) {
                    pagination.push({ type: 'numbered', links: links.slice(0, 20) });
                }
            }
        });

        const nav_menus = [];
        document.querySelectorAll('nav, [role="navigation"], header').forEach(nav => {
            const links = Array.from(nav.querySelectorAll('a'))
                .map(a => ({ text: a.innerText.trim(), href: a.href }))
                .filter(a => a.text.length > 0 && a.text.length < 80);
            if (links.length > 0) {
                nav_menus.push({
                    tag: nav.tagName.toLowerCase(),
                    link_count: links.length,
                    links: links.slice(0, 20)
                });
            }
        });

        const tabs = [];
        document.querySelectorAll('[role="tablist"]').forEach(tablist => {
            const tab_items = Array.from(tablist.querySelectorAll('[role="tab"]'));
            tabs.push({
                tab_count: tab_items.length,
                active_tab: (tab_items.find(t => t.getAttribute('aria-selected') === 'true')
                    ? (tab_items.find(t => t.getAttribute('aria-selected') === 'true').textContent || '')
                    : '').trim().substring(0, 60),
                items: tab_items.map(t => t.textContent.trim().substring(0, 60))
            });
        });

        const modals = [];
        document.querySelectorAll('[role="dialog"], .modal, [aria-modal="true"]').forEach(modal => {
            const style = window.getComputedStyle(modal);
            modals.push({
                visible: style.display !== 'none' && style.visibility !== 'hidden',
                has_close: !!modal.querySelector('[aria-label*="close"], [class*="close"], button'),
                has_aria_modal: modal.getAttribute('aria-modal') === 'true',
                has_aria_labelledby: !!modal.getAttribute('aria-labelledby')
            });
        });

        const accordions = [];
        document.querySelectorAll('[data-toggle="collapse"], details, [aria-expanded]').forEach(el => {
            if (el.tagName === 'DETAILS' || el.getAttribute('data-toggle') === 'collapse') {
                accordions.push({
                    tag: el.tagName.toLowerCase(),
                    open: el.open || el.getAttribute('aria-expanded') === 'true',
                    has_summary: !!el.querySelector('summary')
                });
            }
        });

        const breadcrumbs_el = document.querySelector(
            '[aria-label="breadcrumb"], nav.breadcrumb, ol.breadcrumb, .breadcrumbs'
        );
        const breadcrumbs = breadcrumbs_el
            ? { found: true, item_count: breadcrumbs_el.querySelectorAll('li, a').length }
            : { found: false, item_count: 0 };

        const sidebar_el = document.querySelector('aside, [role="complementary"], .sidebar, #sidebar');
        const sidebar = sidebar_el
            ? { found: true, visible: window.getComputedStyle(sidebar_el).display !== 'none' }
            : { found: false, visible: false };

        const ui_summary = {
            buttons: document.querySelectorAll('button, [role="button"]').length,
            links: document.querySelectorAll('a[href]').length,
            inputs: document.querySelectorAll('input:not([type="hidden"])').length,
            selects: document.querySelectorAll('select').length,
            textareas: document.querySelectorAll('textarea').length,
            images: document.querySelectorAll('img').length,
            videos: document.querySelectorAll('video').length,
            iframes: document.querySelectorAll('iframe').length,
            nav_menus: document.querySelectorAll('nav, [role="navigation"]').length,
            modals: document.querySelectorAll('[role="dialog"], [aria-modal="true"]').length,
            tab_lists: document.querySelectorAll('[role="tablist"]').length,
            accordions: document.querySelectorAll('details').length,
        };

        return {
            ui_elements: ui_elements.slice(0, 200), ui_summary,
            forms, dropdowns, pagination, nav_menus, tabs, modals,
            accordions, breadcrumbs, sidebar
        };
    }""")
    except Exception as e:
        logger.error(f"DOM capture failed: {e}")
        return {
            "ui_elements": [], "ui_summary": {}, "forms": [], "dropdowns": [],
            "pagination": [], "nav_menus": [], "tabs": [], "modals": [],
            "accordions": [], "breadcrumbs": {"found": False}, "sidebar": {"found": False},
            "_dom_error": str(e)
        }


# ── Filter-aware engine runners ─────────────────────────────────────────────

def _filter_active(active_filters: list, key: str) -> bool:
    """Returns True if the filter key is selected (or no filters = run all)."""
    if not active_filters:
        return True
    return key in active_filters


# ── ETA state tracking ────────────────────────────────────────────────────────

class ETATracker:
    def __init__(self):
        self.page_times = []   # list of seconds per page

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


# ── Main Crawl Loop ────────────────────────────────────────────────────────────

async def crawl_site(
    context,
    base_url: str,
    run_id: int,
    active_filters: list = None,
    update_fn=None
):
    global visited, page_data, MAX_PAGES
    queue = [normalize_url(base_url)]
    eta_tracker = ETATracker()

    while queue:
        if MAX_PAGES and len(page_data) >= MAX_PAGES:
            break

        current_url = queue.pop(0)
        if current_url in visited:
            continue
        if not same_domain(base_url, current_url):
            continue

        visited.add(current_url)
        page_start = time.time()
        discovered_count = len(visited) + len(queue)

        logger.info(f"Scanning [{len(page_data)+1}/{MAX_PAGES or '?'}]: {current_url}")

        page = await context.new_page()
        try:
            failed_requests = []
            js_errors = []
            page.on("requestfailed", lambda req: failed_requests.append(req.url))
            page.on("pageerror", lambda err: js_errors.append(str(err)))

            error_responses = []
            redirect_count = [0]

            def handle_response(response):
                if 300 <= response.status < 400:
                    redirect_count[0] += 1
                if response.status >= 400:
                    ct = response.headers.get("content-type", "")
                    if "text/html" in ct:
                        error_responses.append({"url": response.url, "status": response.status})

            page.on("response", handle_response)

            try:
                response = await page.goto(current_url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                try:
                    response = await page.goto(current_url, wait_until="load", timeout=30000)
                except Exception:
                    response = await page.goto(current_url, wait_until="commit", timeout=15000)

            await page.wait_for_timeout(2000)

            # ── Engine execution based on active filters ──
            dom_data = {}
            perf_raw = {}
            a11y_raw = {}
            security_raw = {}

            # Always collect DOM elements (needed for scoring)
            if _filter_active(active_filters, "ui_elements") or \
               _filter_active(active_filters, "form_validation") or \
               not active_filters:
                dom_data = await capture_dom_elements(page)

            # Performance engine
            if _filter_active(active_filters, "performance"):
                perf_raw = await capture_performance_metrics(page)

            # Accessibility engine
            if _filter_active(active_filters, "accessibility"):
                a11y_raw = await capture_accessibility_data(page)

            # Security engine
            if _filter_active(active_filters, "security"):
                security_raw = await capture_security_data(page, response, current_url)

            title = await page.title()
            status = response.status if response else 0

            screenshot_path = f"screenshots/page_{int(time.time()*1000)}.png"
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

            # ── Compute Scores ──
            perf_score_data = compute_performance_score(perf_raw) if perf_raw else {"score": None, "grade": None, "slow_indicators": []}
            a11y_score_data = compute_accessibility_score(a11y_raw) if a11y_raw else {"score": None, "risk_level": None, "wcag_violations": []}
            sec_score_data = compute_security_score(security_raw) if security_raw else {"score": None, "risk_level": None}
            analyzed_forms = analyze_all_forms(dom_data.get("forms") or []) if dom_data else []

            page_object = {
                "url": current_url,
                "title": title,
                "timestamp": datetime.now(UTC).isoformat(),
                "status": status,
                "result": "pass" if status == 200 else "fail",

                # DOM & UI
                "ui_elements": dom_data.get("ui_elements", []),
                "ui_summary": dom_data.get("ui_summary", {}),
                "forms": analyzed_forms,
                "dropdowns": dom_data.get("dropdowns", []),
                "pagination": dom_data.get("pagination", []),
                "nav_menus": dom_data.get("nav_menus", []),
                "tabs": dom_data.get("tabs", []),
                "modals": dom_data.get("modals", []),
                "accordions": dom_data.get("accordions", []),
                "breadcrumbs": dom_data.get("breadcrumbs", {}),
                "sidebar": dom_data.get("sidebar", {}),

                # Functional
                "broken_links": list({(e["url"], e["status"]) for e in error_responses}),
                "errors": failed_requests[:20],
                "js_errors": js_errors[:20],
                "redirect_chain_length": redirect_count[0],

                # Performance (raw + scored)
                "performance_metrics": perf_raw,
                "performance_score": perf_score_data.get("score"),
                "performance_grade": perf_score_data.get("grade"),
                "load_time": round((perf_raw.get("load_event_end_ms") or 0) / 1000.0, 2) if perf_raw else None,
                "fcp_ms": perf_raw.get("fcp_ms") if perf_raw else None,
                "lcp_ms": perf_raw.get("lcp_ms") if perf_raw else None,
                "ttfb_ms": perf_raw.get("ttfb_ms") if perf_raw else None,
                "slow_indicators": perf_score_data.get("slow_indicators", []),

                # Accessibility
                "accessibility_data": a11y_raw,
                "accessibility_score": a11y_score_data.get("score"),
                "accessibility_risk": a11y_score_data.get("risk_level"),
                "accessibility_issues": (a11y_raw or {}).get("total_issues", 0) if a11y_raw else None,
                "wcag_violations": a11y_score_data.get("wcag_violations", []),

                # Security
                "security_data": security_raw,
                "security_score": sec_score_data.get("score"),
                "security_risk": sec_score_data.get("risk_level"),
                "is_https": security_raw.get("is_https") if security_raw else None,

                # Screenshot & meta
                "screenshot": screenshot_path,
                "connected_pages": internal_links[:50],
                "viewport": "1280x800",

                # Filters applied
                "active_filters": active_filters or list(VALID_FILTERS),
            }

            # Functional + UI/Form scoring
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

            # Composite health score (only from enabled engines)
            health_data = compute_page_health_score(
                performance_score=page_object["performance_score"],
                accessibility_score=page_object["accessibility_score"],
                security_score=page_object["security_score"],
                functional_score=page_object["functional_score"],
                ui_form_score=page_object["ui_form_score"],
            )
            page_object["health_score"] = health_data.get("health_score")
            page_object["risk_category"] = health_data.get("risk_category")
            page_object["health_breakdown"] = health_data

            # ── Confidence + AI learning fields ──
            enrich_page_with_ai_fields(page_object, active_filters)

            page_data.append(page_object)

            # ── ETA update ──
            elapsed = time.time() - page_start
            eta_tracker.record_page(elapsed)
            remaining = len(queue)
            if MAX_PAGES:
                remaining = max(0, MAX_PAGES - len(page_data))
            avg_ms = round((eta_tracker.avg_time() or 0) * 1000, 1)
            eta_s = eta_tracker.eta_seconds(remaining)

            if update_fn:
                try:
                    update_fn(
                        scanned=len(page_data),
                        total=MAX_PAGES or (len(page_data) + len(queue)),
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
    run_id: int,
    start_url: str,
    user_id: int,
    page_limit=None,
    update_fn=None,
    active_filters: list = None
):
    global MAX_PAGES, visited, page_data

    MAX_PAGES = int(page_limit) if page_limit and str(page_limit).isdigit() else None
    visited = set()
    page_data = []

    # Normalize + validate filters
    if active_filters:
        active_filters = [f for f in active_filters if f in VALID_FILTERS]
    if not active_filters:
        active_filters = None  # None = run all

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="GuardianAI/4.0 QA Bot"
        )
        await crawl_site(context, start_url, run_id, active_filters=active_filters, update_fn=update_fn)
        await browser.close()

    # ── Build report DataFrame ──
    rows = []
    for pg in page_data:
        perf_m = pg.get("performance_metrics") or {}
        a11y_d = pg.get("accessibility_data") or {}
        sec_d = pg.get("security_data") or {}
        ui_s = pg.get("ui_summary") or {}

        rows.append({
            "URL": pg["url"],
            "Title": pg["title"],
            "Status": pg["status"],
            "Result": pg["result"],
            "Load Time (s)": pg.get("load_time"),
            "FCP (ms)": pg.get("fcp_ms"),
            "LCP (ms)": pg.get("lcp_ms"),
            "TTFB (ms)": pg.get("ttfb_ms"),
            "Performance Score": pg.get("performance_score"),
            "Accessibility Score": pg.get("accessibility_score"),
            "Accessibility Issues": pg.get("accessibility_issues"),
            "Accessibility Risk": pg.get("accessibility_risk"),
            "Security Score": pg.get("security_score"),
            "Security Risk": pg.get("security_risk"),
            "Is HTTPS": pg.get("is_https"),
            "Functional Score": pg.get("functional_score"),
            "UI/Form Score": pg.get("ui_form_score"),
            "Health Score": pg.get("health_score"),
            "Risk Category": pg.get("risk_category"),
            "Confidence Score": pg.get("confidence_score"),
            "Failure Pattern ID": pg.get("failure_pattern_id"),
            "Root Cause Tag": pg.get("root_cause_tag"),
            "Broken Links": len(pg.get("broken_links") or []),
            "JS Errors": len(pg.get("js_errors") or []),
            "Redirect Chain": pg.get("redirect_chain_length", 0),
            "Forms Count": len(pg.get("forms") or []),
            "Buttons": ui_s.get("buttons", 0),
            "Links": ui_s.get("links", 0),
            "Images": ui_s.get("images", 0),
            "Elements Found": len(pg.get("ui_elements") or []),
            "Screenshot": pg.get("screenshot"),
        })

    df = pd.DataFrame(rows)
    timestamp = int(time.time())
    report_file = f"reports/qa_report_{timestamp}.xlsx"
    df.to_excel(report_file, index=False)

    raw_file = f"raw/qa_raw_{timestamp}.json"
    with open(raw_file, "w", encoding="utf-8") as f:
        json.dump(page_data, f, default=str)

    summary_file = f"reports/ai_summary_{timestamp}.txt"
    with open(summary_file, "w", encoding="utf-8") as f:
        f.write(analyze_site(page_data))

    page_health_list = [pg.get("health_breakdown", {}) for pg in page_data]
    site_health = compute_site_health_score(page_health_list)

    # Run-level confidence score
    run_confidence = compute_run_confidence(page_data, active_filters)
    site_health["confidence_score"] = run_confidence

    site_summary_file = f"reports/site_health_{timestamp}.json"
    with open(site_summary_file, "w", encoding="utf-8") as f:
        json.dump(site_health, f)

    total = len(page_data)
    passed = sum(1 for pg in page_data if pg["result"] == "pass")

    logger.info(f"Scan complete → {total} pages, {passed} passed, confidence={run_confidence}")

    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "report_file": report_file,
        "summary_file": summary_file,
        "raw_file": raw_file,
        "site_summary_file": site_summary_file,
        "scanned_pages": len(page_data),
        "site_health": site_health,
        "confidence_score": run_confidence,
        "active_filters": active_filters,
    }