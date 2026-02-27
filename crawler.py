"""
crawler.py — GuardianAI Production Refactor
Changes:
  - Broken links split into: broken_navigation_links, failed_assets, third_party_failures
  - Navigation link validation via context.request.get() (no new pages)
  - functional_score uses ONLY broken_navigation_links
  - Memory-safe: page.close() always in finally block
  - Rate limiting: configurable delay between pages
  - Crawl anomaly detection
  - Improved timeout handling with tiered fallback
  - Logging granularity improvements
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

VALID_FILTERS = frozenset({
    "ui_elements", "form_validation", "functional",
    "accessibility", "performance", "security",
})

# Asset extensions that should never count as broken navigation links
ASSET_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp4", ".webm", ".ogg", ".mp3", ".wav",
    ".css", ".js", ".json", ".xml", ".pdf",
    ".zip", ".gz", ".tar",
})

# Inter-page crawl delay to avoid rate-limit bans (ms)
CRAWL_DELAY_MS = int(os.environ.get("GUARDIAN_CRAWL_DELAY_MS", "500"))

# Max consecutive failures before anomaly abort
ANOMALY_FAILURE_THRESHOLD = int(os.environ.get("GUARDIAN_ANOMALY_THRESHOLD", "5"))


# ── URL Utilities ──────────────────────────────────────────────────────────────

def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    parsed = parsed._replace(fragment="")
    return urlunparse(parsed).rstrip("/")


def same_domain(base: str, url: str) -> bool:
    return urlparse(base).netloc == urlparse(url).netloc


def is_asset_url(url: str) -> bool:
    """Returns True if the URL points to a non-navigable asset."""
    path = urlparse(url).path.lower()
    _, ext = os.path.splitext(path)
    return ext in ASSET_EXTENSIONS


def is_third_party(base_url: str, url: str) -> bool:
    base_netloc = urlparse(base_url).netloc.lstrip("www.")
    url_netloc  = urlparse(url).netloc.lstrip("www.")
    return url_netloc != base_netloc and url_netloc != ""


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


# ── Broken Link Classifier ─────────────────────────────────────────────────────

async def classify_links(context, page, base_url: str) -> dict:
    """
    Classifies all <a href> links on a page into:
      - broken_navigation_links: internal anchor links returning 4xx/5xx
      - failed_assets:           images/scripts/fonts/media that failed to load
      - third_party_failures:    external resources that failed

    Uses context.request.get() for navigation links — no new pages opened.
    Asset failures come from the response listener already attached to the page.
    """
    broken_navigation_links = []
    failed_assets           = []
    third_party_failures    = []

    try:
        raw_hrefs = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => e.href).filter(h => h && !h.startsWith('mailto:') && !h.startsWith('tel:') && !h.startsWith('javascript:'))"
        )
    except Exception as e:
        logger.warning(f"classify_links: could not extract hrefs — {e}")
        return {
            "broken_navigation_links": [],
            "failed_assets": [],
            "third_party_failures": [],
            "internal_links": [],
        }

    internal_links = []
    check_targets  = []

    for href in raw_hrefs:
        norm = normalize_url(href)
        if same_domain(base_url, norm) and not is_asset_url(norm):
            internal_links.append(norm)
            check_targets.append(norm)

    # Deduplicate
    check_targets = list(dict.fromkeys(check_targets))

    # Validate navigation links via lightweight HEAD/GET (no page load)
    for href in check_targets:
        try:
            resp = await context.request.get(
                href,
                timeout=8000,
                headers={"User-Agent": "GuardianAI-LinkChecker/1.0"},
            )
            if resp.status >= 400:
                broken_navigation_links.append({
                    "url": href,
                    "status": resp.status,
                })
            try:
                await resp.dispose()
            except Exception:
                pass
        except Exception as e:
            err_str = str(e).lower()
            # Timeout or net::ERR_* = genuinely broken
            if "timeout" in err_str or "err_" in err_str or "net::" in err_str:
                broken_navigation_links.append({
                    "url": href,
                    "status": None,
                    "error": str(e)[:120],
                })
            # Otherwise (e.g. SSL mismatch on internal dev URLs) skip

    return {
        "broken_navigation_links": broken_navigation_links,
        "failed_assets": failed_assets,           # populated from response listener
        "third_party_failures": third_party_failures,  # populated from response listener
        "internal_links": list(dict.fromkeys(internal_links)),
    }


# ── DOM Intelligence Layer ─────────────────────────────────────────────────────

async def capture_dom_elements(page) -> dict:
    try:
        data = await page.evaluate("""() => {
            const ui_elements = [...document.querySelectorAll(
                'button, a[href], input, select, textarea, [role="button"], [role="link"]'
            )].slice(0, 200).map(el => ({
                tag: el.tagName.toLowerCase(),
                type: el.getAttribute('type') || null,
                id: el.id || null,
                text: (el.innerText || el.value || '').substring(0, 80),
                visible: el.offsetParent !== null,
            }));
            const forms = [...document.querySelectorAll('form')].map(f => ({
                id: f.id || null, action: f.action || null, method: f.method || 'get',
                inputs: [...f.querySelectorAll('input,select,textarea')].map(i => ({
                    type: i.type || 'text', name: i.name || null,
                    required: i.required, has_label: !!document.querySelector(`label[for="${i.id}"]`) || !!i.closest('label'),
                }))
            }));
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
                ui_elements, ui_summary: { images, buttons, links },
                forms, nav_menus, dropdowns, tabs, modals, accordions, pagination,
                breadcrumbs: { found: !!breadcrumbs_el },
                sidebar:     { found: !!sidebar_el },
            };
        }""")
        return data
    except Exception as e:
        logger.warning(f"capture_dom_elements failed: {e}")
        return {}


# ── Crawl Anomaly Detection ────────────────────────────────────────────────────

class CrawlAnomalyDetector:
    def __init__(self, threshold: int = ANOMALY_FAILURE_THRESHOLD):
        self.threshold       = threshold
        self.consecutive_err = 0
        self.anomalies       = []

    def record_success(self):
        self.consecutive_err = 0

    def record_failure(self, url: str, reason: str):
        self.consecutive_err += 1
        self.anomalies.append({"url": url, "reason": reason})
        if self.consecutive_err >= self.threshold:
            logger.warning(
                f"[ANOMALY] {self.consecutive_err} consecutive failures — "
                f"possible rate-limit or structural block. Last: {url}"
            )

    def should_abort(self) -> bool:
        return self.consecutive_err >= self.threshold * 2  # abort at 2x threshold


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
    queue        = [normalize_url(base_url)]
    eta_tracker  = ETATracker()
    anomaly_det  = CrawlAnomalyDetector()

    while queue:
        if max_pages is not None and len(page_data) >= int(max_pages):
            break

        if anomaly_det.should_abort():
            logger.error(f"[ABORT] Too many consecutive failures — stopping crawl at {len(page_data)} pages")
            break

        current_url = queue.pop(0)
        if current_url in visited:
            continue
        if not same_domain(base_url, current_url):
            continue

        visited.add(current_url)
        page_start       = time.time()
        discovered_count = len(visited) + len(queue)

        logger.info(f"[{len(page_data)+1}/{max_pages or '?'}] Scanning: {current_url}")

        page = await context.new_page()
        try:
            # ── Response listeners — classify ALL network responses ──
            failed_assets        = []
            third_party_failures = []
            js_errors            = []
            redirect_count       = [0]

            def handle_response(response):
                url  = response.url
                stat = response.status
                if 300 <= stat < 400:
                    redirect_count[0] += 1
                if stat >= 400:
                    if is_asset_url(url):
                        failed_assets.append({"url": url, "status": stat})
                    elif is_third_party(base_url, url):
                        third_party_failures.append({"url": url, "status": stat})

            def handle_request_failed(req):
                url = req.url
                if is_asset_url(url):
                    failed_assets.append({"url": url, "status": None, "error": req.failure or ""})
                elif is_third_party(base_url, url):
                    third_party_failures.append({"url": url, "status": None})
                # internal non-asset failures are caught by classify_links

            page.on("response",      handle_response)
            page.on("requestfailed", handle_request_failed)
            page.on("pageerror",     lambda err: js_errors.append(str(err)))

            # ── Navigate with tiered timeout fallback ──
            response = None
            for wait_until, timeout in [("domcontentloaded", 30000), ("load", 30000), ("commit", 15000)]:
                try:
                    response = await page.goto(current_url, wait_until=wait_until, timeout=timeout)
                    break
                except Exception as nav_err:
                    logger.debug(f"nav fallback [{wait_until}] for {current_url}: {nav_err}")
                    response = None

            if response is None:
                logger.warning(f"All navigation attempts failed for {current_url}")
                anomaly_det.record_failure(current_url, "navigation_failed")
                continue

            await page.wait_for_timeout(1500)  # reduced from 2000ms

            status = response.status if response else 0
            title  = ""
            try:
                title = await page.title()
            except Exception:
                pass

            # ── Engine execution gated by filters ──
            dom_data     = {}
            perf_raw     = {}
            a11y_raw     = {}
            security_raw = {}

            if _filter_active(active_filters, "ui_elements") or \
               _filter_active(active_filters, "form_validation") or \
               not active_filters:
                try:
                    dom_data = await capture_dom_elements(page)
                except Exception as e:
                    logger.warning(f"DOM capture failed {current_url}: {e}")

            if _filter_active(active_filters, "performance"):
                try:
                    perf_raw = await capture_performance_metrics(page)
                except Exception as e:
                    logger.warning(f"Perf capture failed {current_url}: {e}")

            if _filter_active(active_filters, "accessibility"):
                try:
                    a11y_raw = await capture_accessibility_data(page)
                except Exception as e:
                    logger.warning(f"A11y capture failed {current_url}: {e}")

            if _filter_active(active_filters, "security"):
                try:
                    security_raw = await capture_security_data(page, response, current_url)
                except Exception as e:
                    logger.warning(f"Security capture failed {current_url}: {e}")

            # ── Screenshot ──
            screenshot_path = f"screenshots/run_{run_id}_{int(time.time()*1000)}.png"
            try:
                await page.screenshot(path=screenshot_path, full_page=True, timeout=10000)
            except Exception:
                screenshot_path = None

            # ── REFACTORED: Classify broken links ──
            # NOTE: classify_links is ALWAYS called so internal_links are discovered
            # for the crawl queue regardless of active filters.
            # Broken-link *scoring* data is only used when "functional" filter is active.
            link_data = {"broken_navigation_links": [], "failed_assets": [], "third_party_failures": [], "internal_links": []}
            try:
                link_data = await classify_links(context, page, base_url)
                # Merge asset/third-party failures from response listener into link_data
                link_data["failed_assets"]        += failed_assets
                link_data["third_party_failures"] += third_party_failures
            except Exception as e:
                logger.warning(f"Link classification failed {current_url}: {e}")
                link_data["failed_assets"]        = failed_assets
                link_data["third_party_failures"] = third_party_failures

            # Gate broken-link scoring data behind the functional filter
            if not (_filter_active(active_filters, "functional") or not active_filters):
                link_data["broken_navigation_links"] = []

            internal_links = link_data["internal_links"]

            # Enqueue unvisited internal links
            for link in internal_links:
                if link not in visited and link not in queue:
                    queue.append(link)

            # ── Compute scores ──
            perf_score_data = compute_performance_score(perf_raw)  if perf_raw  else {"score": None, "grade": None, "slow_indicators": []}
            a11y_score_data = compute_accessibility_score(a11y_raw) if a11y_raw  else {"score": None, "risk_level": None, "wcag_violations": []}
            sec_score_data  = compute_security_score(security_raw)  if security_raw else {"score": None, "risk_level": None}
            analyzed_forms  = analyze_all_forms(dom_data.get("forms") or []) if dom_data else []

            load_ms = None
            try:
                load_ms = (perf_raw or {}).get("load_event_end_ms", 0) / 1000 if perf_raw else None
            except Exception:
                pass

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

                # Topology
                "connected_pages": internal_links,

                # Performance
                "performance_metrics": perf_raw,
                "performance_score":   perf_score_data.get("score"),
                "performance_grade":   perf_score_data.get("grade"),
                "load_time":           load_ms,
                "fcp_ms":  (perf_raw or {}).get("fcp_ms"),
                "lcp_ms":  (perf_raw or {}).get("lcp_ms"),
                "ttfb_ms": (perf_raw or {}).get("ttfb_ms"),

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

                # REFACTORED Functional — broken links now separated
                "broken_navigation_links": link_data["broken_navigation_links"],
                "failed_assets":           link_data["failed_assets"],
                "third_party_failures":    link_data["third_party_failures"],
                # Legacy alias kept for DB compat — points to nav links only
                "broken_links":            link_data["broken_navigation_links"],
                "js_errors":               js_errors,
                "failed_requests":         [],  # no longer used for scoring
                "redirect_chain_length":   redirect_count[0],

                # Screenshot
                "screenshot": screenshot_path,
            }

            # ── Component scores ──
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

            # Composite health
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

            enrich_page_with_ai_fields(page_object, active_filters)
            page_data.append(page_object)
            anomaly_det.record_success()

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

            logger.info(
                f"  ✓ status={status} health={page_object.get('health_score')} "
                f"nav_broken={len(link_data['broken_navigation_links'])} "
                f"asset_fail={len(link_data['failed_assets'])} "
                f"3p_fail={len(link_data['third_party_failures'])} "
                f"js_err={len(js_errors)} elapsed={round(elapsed,2)}s"
            )

        except Exception as e:
            logger.error(f"Error crawling {current_url}: {e}", exc_info=True)
            anomaly_det.record_failure(current_url, str(e)[:120])
        finally:
            # CRITICAL: always close page to prevent memory leak
            try:
                await page.close()
            except Exception:
                pass

        # Rate-limit protection
        if CRAWL_DELAY_MS > 0:
            await asyncio.sleep(CRAWL_DELAY_MS / 1000.0)


# ── Report Builder ─────────────────────────────────────────────────────────────

async def build_reports(run_id: int, page_data: list, active_filters: list | None):
    rows = []
    for pg in page_data:
        ui_s = pg.get("ui_summary") or {}
        rows.append({
            "URL":                  pg.get("url"),
            "Title":                pg.get("title"),
            "Status":               pg.get("status"),
            "Result":               pg.get("result"),
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
            # REFACTORED: separate broken link columns
            "Broken Nav Links":     len(pg.get("broken_navigation_links") or []),
            "Failed Assets":        len(pg.get("failed_assets") or []),
            "3rd Party Failures":   len(pg.get("third_party_failures") or []),
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


# ── Entry Point ────────────────────────────────────────────────────────────────

async def main(
    run_id:         int,
    start_url:      str,
    user_id:        int,
    page_limit=None,
    update_fn=None,
    active_filters: list | None = None,
):
    visited   = set()
    page_data = []

    # Always resolve to int or None — never pass a raw string into crawl_site
    try:
        max_pages = int(page_limit) if page_limit not in (None, "", "None", "null") else None
    except (TypeError, ValueError):
        max_pages = None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            ignore_https_errors=True,
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        try:
            await crawl_site(
                context=context,
                base_url=start_url,
                run_id=run_id,
                visited=visited,
                page_data=page_data,
                max_pages=max_pages,
                active_filters=active_filters,
                update_fn=update_fn,
            )
        finally:
            await context.close()
            await browser.close()

    return await build_reports(run_id, page_data, active_filters)


def run_crawler(run_id, start_url, user_id, page_limit=None, update_fn=None, active_filters=None):
    """Sync entry point called by RQ worker via tasks.py."""
    return asyncio.run(
        main(
            run_id=run_id,
            start_url=start_url,
            user_id=user_id,
            page_limit=page_limit,
            update_fn=update_fn,
            active_filters=active_filters,
        )
    )