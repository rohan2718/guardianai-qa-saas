"""
crawler.py — GuardianAI Production Refactor
Changes v2 (QA Intelligence upgrade):
  - nav_menus now captures full link text, href, aria-label per item
  - sidebar_links captured with text + href
  - JS errors capture message + stack trace + source location
  - console errors also captured (type="error")
  - Broken links split into: broken_navigation_links, failed_assets, third_party_failures
  - Navigation link validation via context.request.get() (no new pages)
  - functional_score uses ONLY broken_navigation_links
  - Memory-safe: page.close() always in finally block
  - Rate limiting: configurable delay between pages
  - Crawl anomaly detection
  - Improved timeout handling with tiered fallback
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


# crawler.py  — add near the top, after imports

import socket
import urllib.request
import urllib.error
from urllib.parse import urlparse

# ── Pre-flight reachability check ─────────────────────────────────────────────

class TargetUnreachableError(Exception):
    """Raised when the scan target cannot be contacted before Playwright starts."""
    pass


def _tcp_reachable(url: str, timeout: float = 5.0) -> tuple[bool, str]:
    """
    Attempt a raw TCP connection to host:port derived from the URL.
    Returns (reachable: bool, reason: str).
    """
    parsed = urlparse(url)
    host   = parsed.hostname
    port   = parsed.port or (443 if parsed.scheme == "https" else 80)

    if not host:
        return False, "URL has no resolvable hostname"

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        if result == 0:
            return True, "TCP connection succeeded"
        return False, f"TCP connection refused (errno {result})"
    except socket.gaierror as e:
        return False, f"DNS resolution failed: {e}"
    except socket.timeout:
        return False, f"TCP connection timed out after {timeout}s"
    except OSError as e:
        return False, f"Network error: {e}"


def _http_reachable(url: str, timeout: float = 8.0) -> tuple[bool, str]:
    """
    Fallback: attempt an HTTP HEAD request.
    Useful when a server accepts TCP but the TCP check gives a false negative
    (e.g., behind a transparent proxy).
    """
    try:
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", "GuardianAI-Preflight/1.0")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, f"HTTP HEAD {resp.status}"
    except urllib.error.HTTPError as e:
        # 4xx/5xx means server is up, just unhappy — still reachable
        return True, f"HTTP HEAD {e.code} (server is up)"
    except urllib.error.URLError as e:
        return False, f"HTTP HEAD failed: {e.reason}"
    except Exception as e:
        return False, f"HTTP HEAD error: {e}"


def preflight_check(url: str) -> None:
    """
    Raises TargetUnreachableError if the target cannot be contacted.
    Call this before launching Playwright.
    """
    tcp_ok, tcp_reason = _tcp_reachable(url)
    if tcp_ok:
        logger.info(f"[preflight] {url} → reachable ({tcp_reason})")
        return

    logger.warning(f"[preflight] TCP failed: {tcp_reason} — trying HTTP HEAD")
    http_ok, http_reason = _http_reachable(url)
    if http_ok:
        logger.info(f"[preflight] {url} → reachable via HTTP ({http_reason})")
        return

    raise TargetUnreachableError(
        f"Target unreachable. TCP: {tcp_reason} | HTTP: {http_reason}"
    )
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
    return base_netloc != url_netloc


def _filter_active(active_filters, name: str) -> bool:
    if not active_filters:
        return True
    return name in active_filters


# ── ETA Tracker ────────────────────────────────────────────────────────────────

class ETATracker:
    def __init__(self):
        self._times: list[float] = []

    def record(self, elapsed_ms: float):
        self._times.append(elapsed_ms)
        if len(self._times) > 10:
            self._times.pop(0)

    def avg_ms(self) -> float:
        return sum(self._times) / len(self._times) if self._times else 0.0

    def eta(self, remaining: int) -> float:
        return (self.avg_ms() * remaining) / 1000.0


# ── Link Classification ────────────────────────────────────────────────────────

async def classify_links(page, base_url: str, context) -> dict:
    """
    Validates internal navigation links.
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
            if "timeout" in err_str or "err_" in err_str or "net::" in err_str:
                broken_navigation_links.append({
                    "url": href,
                    "status": None,
                    "error": str(e)[:120],
                })

    return {
        "broken_navigation_links": broken_navigation_links,
        "failed_assets": failed_assets,
        "third_party_failures": third_party_failures,
        "internal_links": list(dict.fromkeys(internal_links)),
    }


# ── DOM Intelligence Layer ─────────────────────────────────────────────────────

async def capture_dom_elements(page) -> dict:
    try:
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        data = await page.evaluate("""() => {
            // ── Visibility helpers ──────────────────────────────────────────

            function isElementVisible(el) {
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 && rect.height === 0) return false;
                const s = window.getComputedStyle(el);
                return s.display !== 'none' && s.visibility !== 'hidden' &&
                       s.visibility !== 'collapse' && parseFloat(s.opacity) !== 0;
            }

            function isInsideHiddenContainer(el) {
                let node = el.parentElement;
                while (node && node !== document.body) {
                    const s = window.getComputedStyle(node);
                    if (s.display === 'none' || s.visibility === 'hidden' ||
                        s.visibility === 'collapse' || parseFloat(s.opacity) === 0)
                        return true;
                    node = node.parentElement;
                }
                return false;
            }

            function isCookieBannerElement(el) {
                const COOKIE_PATTERNS = [
                    /cookie/i, /consent/i, /gdpr/i, /ccpa/i,
                    /onetrust/i, /cookiebot/i, /cookiepro/i,
                    /privacy-notice/i, /cookie-notice/i,
                    /cookie-banner/i, /cookie-bar/i,
                ];
                const haystack = [
                    el.id || '', el.className || '',
                    el.getAttribute('data-testid') || '',
                    el.getAttribute('aria-label') || '',
                ].join(' ');
                return COOKIE_PATTERNS.some(p => p.test(haystack));
            }

            function isAccessibilityHelper(el) {
                const SR_CLASSES = [
                    'sr-only', 'visually-hidden', 'screen-reader-only',
                    'a11y-hidden', 'visually_hidden', 'offscreen', 'clip', 'sr_only',
                ];
                const cls = (el.className || '').toString().toLowerCase();
                return SR_CLASSES.some(c => cls.includes(c));
            }

            function shouldIncludeElement(el) {
                if (!isElementVisible(el)) return false;
                if (isInsideHiddenContainer(el)) return false;
                if (isCookieBannerElement(el)) return false;
                if (isAccessibilityHelper(el)) return false;
                return true;
            }

            // ── UI Elements ────────────────────────────────────────────────
            const INTERACTIVE_SELECTORS = [
                'a[href]', 'button', 'input:not([type="hidden"])',
                'select', 'textarea', '[role="button"]', '[role="link"]',
                '[role="menuitem"]', '[role="tab"]',
            ];
            const ui_elements = [];
            const seen_els = new Set();
            for (const sel of INTERACTIVE_SELECTORS) {
                for (const el of document.querySelectorAll(sel)) {
                    if (seen_els.has(el) || !shouldIncludeElement(el)) continue;
                    seen_els.add(el);
                    const tag  = el.tagName.toLowerCase();
                    const role = el.getAttribute('role') || tag;
                    const text = (el.textContent || '').trim().substring(0, 80);
                    const aria = el.getAttribute('aria-label') || '';
                    const href = el.getAttribute('href') || '';
                    const id   = el.id || '';
                    ui_elements.push({ tag, role, text, aria_label: aria, href, id });
                }
            }

            const visibleLinks   = [...document.querySelectorAll('a[href]')].filter(el => shouldIncludeElement(el)).length;
            const visibleButtons = [...document.querySelectorAll('button, [role="button"]')].filter(el => shouldIncludeElement(el)).length;
            const visibleInputs  = [...document.querySelectorAll('input:not([type="hidden"]), select, textarea')].filter(el => shouldIncludeElement(el)).length;
            const visibleImages  = [...document.querySelectorAll('img')].filter(el => isElementVisible(el) && !isInsideHiddenContainer(el)).length;
            const totalVisible   = ui_elements.length;

            // ── Forms ──────────────────────────────────────────────────────
            const forms = [...document.querySelectorAll('form')]
                .map(f => {
                    const allInputs = [...f.querySelectorAll('input, select, textarea, button')];
                    const fields = allInputs
                        .filter(i => i.type !== 'hidden')
                        .map(i => {
                            const tag       = i.tagName.toLowerCase();
                            const inputType = i.getAttribute('type') ||
                                (tag === 'select' ? 'select' : tag === 'textarea' ? 'textarea' : 'text');
                            const labelEl   = i.id ? document.querySelector(`label[for="${i.id}"]`) : null;
                            const labelText = labelEl
                                ? labelEl.innerText.trim().substring(0, 60)
                                : (i.getAttribute('placeholder') || i.getAttribute('aria-label') || null);
                            return {
                                tag, type: inputType,
                                name:         i.getAttribute('name') || null,
                                id:           i.id || null,
                                required:     i.required || i.getAttribute('aria-required') === 'true',
                                placeholder:  i.getAttribute('placeholder') || null,
                                display_name: labelText,
                                maxlength:    i.getAttribute('maxlength') || null,
                                pattern:      i.getAttribute('pattern') || null,
                                autocomplete: i.getAttribute('autocomplete') || null,
                                readonly:     i.readOnly || false,
                                disabled:     i.disabled || false,
                                options: tag === 'select'
                                    ? [...i.querySelectorAll('option')]
                                        .filter(o => o.value)
                                        .slice(0, 10)
                                        .map(o => ({ value: o.value, text: o.innerText.trim().substring(0, 40) }))
                                    : null,
                            };
                        });
                    const visibleFields = fields.filter(f => !['submit','button','reset','image'].includes(f.type));
                    const submitBtn = fields.find(f => ['submit','button'].includes(f.type) || f.tag === 'button');
                    let actionUrl = f.action || null;
                    if (actionUrl) {
                        try { actionUrl = new URL(actionUrl).href; } catch(e) {}
                    }
                    return {
                        id:           f.id || null,
                        action:       actionUrl,
                        method:       (f.getAttribute('method') || 'get').toUpperCase(),
                        enctype:      f.getAttribute('enctype') || null,
                        name:         f.getAttribute('name') || null,
                        fields,
                        fields_count: visibleFields.length,
                        has_submit:   !!submitBtn,
                        submit_label: submitBtn ? (submitBtn.display_name || submitBtn.placeholder || 'Submit') : null,
                        form_purpose: (() => {
                            const names = fields.map(f => (f.name || '').toLowerCase()).join(' ');
                            if (/login|signin|password|username/.test(names)) return 'Login';
                            if (/register|signup|create.*account/.test(names)) return 'Registration';
                            if (/search|query|q\\b/.test(names)) return 'Search';
                            if (/contact|message|enqui/.test(names)) return 'Contact';
                            if (/subscribe|newsletter|email/.test(names)) return 'Newsletter';
                            if (/checkout|payment|card|billing/.test(names)) return 'Checkout';
                            if (/comment|reply|feedback/.test(names)) return 'Feedback';
                            return null;
                        })(),
                    };
                })
                .filter(f => f.fields_count > 0 || f.has_submit);

            // ── NAV MENUS — enriched with actual link text + href ──────────
            // KEY FIX: Previously only captured {id, items_count}.
            // Now captures the actual link labels used in navigation so that
            // flow_discovery can build "Click 'Country Master'" steps instead
            // of "Follow link to ATIRA".
            const navSources = [
                ...document.querySelectorAll('nav, [role="navigation"]'),
                ...document.querySelectorAll('[class*="sidebar"], [class*="side-nav"], [class*="sidenav"]'),
                ...document.querySelectorAll('.app-aside-wrapper, .menu-wrapper, .left-menu, .main-menu'),
            ];
            // Deduplicate elements
            const navUnique = [...new Set(navSources)];
            const nav_menus = navUnique
                .map(nav => {
                    const links = [...nav.querySelectorAll('a[href]')]
                        .filter(a => {
                            const s = window.getComputedStyle(a);
                            return s.display !== 'none' && s.visibility !== 'hidden';
                        })
                        .map(a => ({
                            text:       (a.textContent || '').trim().replace(/\s+/g, ' ').substring(0, 80),
                            href:       a.href || '',
                            aria_label: a.getAttribute('aria-label') || null,
                            title:      a.getAttribute('title') || null,
                            role:       a.getAttribute('role') || null,
                        }))
                        .filter(l => (l.text && l.text.length > 0) || l.aria_label);
                    return {
                        id:         nav.id || null,
                        aria_label: nav.getAttribute('aria-label') || null,
                        items:      links.length,
                        links:      links.slice(0, 40),
                    };
                })
                .filter(nav => nav.items > 0);

            // ── SIDEBAR — enriched with link text ─────────────────────────
            const sidebar_el = document.querySelector('aside, [role="complementary"], .sidebar, .side-nav, .sidenav, #sidebar, [class*="sidebar"], .app-aside-wrapper, .menu-wrapper');
            // Collect sidebar links from sidebar element AND .menu-item divs
            const sidebarLinkEls = [
                ...(sidebar_el ? sidebar_el.querySelectorAll('a[href]') : []),
                ...document.querySelectorAll('.menu-item a[href], [class*="menu-item"] a[href]'),
            ];
            const sidebarLinksSeen = new Set();
            const sidebar_links = sidebarLinkEls
                    .filter(a => {
                        const s = window.getComputedStyle(a);
                        return s.display !== 'none' && a.textContent.trim().length > 0;
                    })
                    .map(a => ({
                        text: (a.textContent || '').trim().replace(/\s+/g, ' ').substring(0, 80),
                        href: a.href || '',
                        aria_label: a.getAttribute('aria-label') || null,
                    }))
                    .filter(l => {
                        if (!l.text.length || !l.href || sidebarLinksSeen.has(l.href)) return false;
                        sidebarLinksSeen.add(l.href);
                        return true;
                    })
                    .slice(0, 60);

            const dropdowns  = [...document.querySelectorAll('select, [role="listbox"], .dropdown')]
                .map(d => ({ id: d.id || null }));
            const tabs       = [...document.querySelectorAll('[role="tab"], .tab')]
                .map(t => ({ text: (t.innerText || '').substring(0, 60) }));
            const modals     = [...document.querySelectorAll('[role="dialog"], .modal, [aria-modal="true"]')]
                .map(m => ({ id: m.id || null }));
            const accordions = [...document.querySelectorAll('details, [role="region"]')]
                .map(a => ({ id: a.id || null }));
            const pagination = [...document.querySelectorAll('[aria-label*="paginat"], .pagination, [class*="paginat"]')]
                .map(p => ({ id: p.id || null }));

            const breadcrumbs_el = document.querySelector(
                '[aria-label*="breadcrumb"], .breadcrumb, nav[aria-label]'
            );
            // Capture actual breadcrumb text items too
            const breadcrumb_items = breadcrumbs_el
                ? [...breadcrumbs_el.querySelectorAll('a, li, span')]
                    .map(el => (el.textContent || '').trim())
                    .filter(t => t.length > 0 && t.length < 60)
                    .slice(0, 8)
                : [];

            return {
                ui_elements,
                ui_summary: {
                    images:        visibleImages,
                    buttons:       visibleButtons,
                    links:         visibleLinks,
                    inputs:        visibleInputs,
                    total_visible: totalVisible,
                },
                forms,
                nav_menus,
                sidebar_links,
                dropdowns,
                tabs,
                modals,
                accordions,
                pagination,
                breadcrumbs: {
                    found: !!breadcrumbs_el,
                    items: breadcrumb_items,
                },
                sidebar: { found: !!sidebar_el },
            };
        }""")

        return data or {}

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
        return self.consecutive_err >= self.threshold * 2


# ── Auth ────────────────────────────────────────────────────────────────────────

def _read_auth_config() -> dict | None:
    """
    Reads login config from environment variables.
    Returns None if CRAWLER_USERNAME is not set (no auth needed).
 
    Standard .env variables:
        CRAWLER_LOGIN_URL=http://example.com/login
        CRAWLER_USERNAME_FIELD=#txtUserName
        CRAWLER_PASSWORD_FIELD=#txtPwd
        CRAWLER_SUBMIT=button:has-text('Sign In')
        CRAWLER_USERNAME=admin
        CRAWLER_PASSWORD=password
        CRAWLER_SUCCESS_URL=/dashboard
        CRAWLER_SKIP_URLS=/logout,/signout
 
    NEW — Extra fields (for multi-field login forms):
        CRAWLER_EXTRA_FIELDS=company_code,unique_code
        CRAWLER_FIELD_company_code_SELECTOR=#txtCompanyCode
        CRAWLER_FIELD_company_code_VALUE=VC00008
        CRAWLER_FIELD_unique_code_SELECTOR=#txtUniqueCode
        CRAWLER_FIELD_unique_code_VALUE=SBCI
 
    Backward compatible: if CRAWLER_EXTRA_FIELDS is not set,
    extra_fields returns [] and _do_login behaves exactly as before.
    """
    # Re-read .env at call time — ensures RQ worker threads always pick up auth vars
    try:
        from dotenv import load_dotenv as _load_dotenv
        from pathlib import Path as _Path
        _load_dotenv(dotenv_path=_Path(__file__).resolve().parent / ".env", override=True)
    except Exception:
        pass
 
    username = os.environ.get("CRAWLER_USERNAME", "").strip()
    if not username:
        logger.debug("[auth] CRAWLER_USERNAME not set — skipping auth")
        return None
 
    # Parse extra pre-auth fields (e.g. company code, unique code)
    extra_fields_raw = os.environ.get("CRAWLER_EXTRA_FIELDS", "").strip()
    extra_fields = []
    if extra_fields_raw:
        for field_name in [f.strip() for f in extra_fields_raw.split(",") if f.strip()]:
            selector = os.environ.get(f"CRAWLER_FIELD_{field_name}_SELECTOR", "").strip()
            value    = os.environ.get(f"CRAWLER_FIELD_{field_name}_VALUE",    "").strip()
            if selector and value:
                extra_fields.append({"name": field_name, "selector": selector, "value": value})
            else:
                logger.warning(
                    f"[auth] CRAWLER_EXTRA_FIELDS: '{field_name}' is missing "
                    f"SELECTOR or VALUE — skipping"
                )
 
    return {
        "login_url":       os.environ.get("CRAWLER_LOGIN_URL", "").strip(),
        "username_field":  os.environ.get("CRAWLER_USERNAME_FIELD", "#txtUserName").strip(),
        "password_field":  os.environ.get("CRAWLER_PASSWORD_FIELD", "#txtPwd").strip(),
        "submit_selector": os.environ.get("CRAWLER_SUBMIT", "button:has-text('Sign In')").strip(),
        "username":        username,
        "password":        os.environ.get("CRAWLER_PASSWORD", "").strip(),
        "success_url":     os.environ.get("CRAWLER_SUCCESS_URL", "").strip(),
        "skip_urls":       [p.strip() for p in os.environ.get("CRAWLER_SKIP_URLS", "").split(",") if p.strip()],
        "extra_fields":    extra_fields,   # NEW — empty list if not configured
    }
 
 
# ── THIS FUNCTION MUST STAY HERE — DO NOT MOVE IT ─────────────────────────────
def _is_skip_url(url: str, auth_cfg: dict | None) -> bool:
    path = urlparse(url).path.lower()
    if "logout" in path or "signout" in path:
        return True
    if auth_cfg:
        for skip_path in auth_cfg.get("skip_urls", []):
            if skip_path.lower() in path:
                return True
    return False
 
 
async def _do_login(context, auth: dict) -> bool:
    """
    Authenticates into the target site using the configured credentials.
    Supports N extra fields before the standard username + password.
    Extra fields are filled in the order declared in CRAWLER_EXTRA_FIELDS.
    """
    page = await context.new_page()
    try:
        await page.goto(auth["login_url"], wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(1000)
 
        # ── Fill extra fields first (e.g. company code, unique code) ──────────
        # This loop is a no-op when extra_fields is [] (standard 2-field login).
        for extra in auth.get("extra_fields", []):
            try:
                await page.wait_for_selector(extra["selector"], timeout=5000)
                await page.fill(extra["selector"], extra["value"])
                await page.wait_for_timeout(300)
                logger.debug(
                    f"[auth] Filled extra field '{extra['name']}' → {extra['selector']}"
                )
            except Exception as e:
                logger.warning(
                    f"[auth] Could not fill extra field '{extra['name']}' "
                    f"({extra['selector']}): {e}"
                )
 
        # ── Fill standard username + password ─────────────────────────────────
        await page.fill(auth["username_field"], auth["username"])
        await page.fill(auth["password_field"], auth["password"])
        await page.click(auth["submit_selector"])
        await page.wait_for_load_state("networkidle", timeout=20000)
        await page.wait_for_timeout(2000)
 
        current_url = page.url
        success_url = auth.get("success_url", "")
 
        if success_url and success_url in current_url:
            cookies = await context.cookies()
            logger.info(f"[auth] ✓ Login succeeded — {len(cookies)} cookies captured")
            return True
        elif auth["login_url"].rstrip("/") == current_url.rstrip("/"):
            logger.warning("[auth] ✗ Login failed — still on login page after submit")
            return False
        else:
            cookies = await context.cookies()
            logger.info(
                f"[auth] ✓ Redirected to {current_url} — {len(cookies)} cookies captured"
            )
            return True
 
    except Exception as e:
        logger.error(f"[auth] Login error: {e}", exc_info=True)
        return False
    finally:
        await page.close()

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
    queue       = [normalize_url(base_url)]
    eta_tracker = ETATracker()
    anomaly_det = CrawlAnomalyDetector()
    auth        = _read_auth_config()

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
        if _is_skip_url(current_url, auth):
            logger.info(f"[skip] {current_url}")
            continue

        visited.add(current_url)
        t_start = time.time()

        page = await context.new_page()

        # ── JS error collection — structured with stack traces ────────────────
        js_errors: list[dict] = []

        def _capture_js_error(err):
            """Capture pageerror with message and stack."""
            js_errors.append({
                "message": str(err),
                "stack":   getattr(err, "stack", None) or str(err),
                "source":  "pageerror",
            })

        def _capture_console_error(msg):
            """Capture console.error() calls."""
            if msg.type == "error":
                location = None
                try:
                    loc = msg.location
                    if loc:
                        location = f"{loc.get('url','?')}:{loc.get('lineNumber','?')}"
                except Exception:
                    pass
                js_errors.append({
                    "message":  msg.text,
                    "stack":    None,
                    "source":   "console.error",
                    "location": location,
                })

        page.on("pageerror", _capture_js_error)
        page.on("console", _capture_console_error)

        # ── Failed asset tracking ──────────────────────────────────────────────
        failed_assets_live:       list[dict] = []
        third_party_failures_live: list[dict] = []

        def _on_response_failed(req):
            url = req.url
            if is_asset_url(url):
                if is_third_party(base_url, url):
                    third_party_failures_live.append({"url": url, "resource_type": req.resource_type})
                else:
                    failed_assets_live.append({"url": url, "resource_type": req.resource_type})

        page.on("requestfailed", _on_response_failed)

        redirect_count = [0]

        try:
            response = None
            load_ms  = None

            try:
                response = await page.goto(
                    current_url,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                load_ms = (time.time() - t_start) * 1000
            except Exception as nav_err:
                logger.warning(f"domcontentloaded failed for {current_url}: {nav_err}")
                try:
                    response = await page.goto(current_url, wait_until="load", timeout=20000)
                    load_ms = (time.time() - t_start) * 1000
                except Exception as nav_err2:
                    logger.warning(f"load failed for {current_url}: {nav_err2}")
                    try:
                        response = await page.goto(current_url, wait_until="commit", timeout=15000)
                        load_ms = (time.time() - t_start) * 1000
                    except Exception as nav_err3:
                        logger.error(f"All navigation strategies failed for {current_url}: {nav_err3}")
                        anomaly_det.record_failure(current_url, str(nav_err3))
                        continue

            status = response.status if response else None

            # Track redirects
            if response and response.url != current_url:
                redirect_count[0] += 1

            # ── Engine execution gated by filters ──────────────────────────
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

            # ── Screenshot ─────────────────────────────────────────────────
            screenshot_path = f"screenshots/run_{run_id}_{int(time.time()*1000)}.png"
            try:
                # Wait for page to visually settle before capturing
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass

                # ── Guard: if page redirected to login, re-authenticate ──────
                # This happens when the session expires mid-crawl for some pages.
                # Re-login and navigate back to the real page before screenshotting.
                page_url_now = page.url.lower()
                auth_cfg = _read_auth_config()
                login_indicators = ["login", "signin", "sign-in", "account/login"]
                if auth_cfg and any(ind in page_url_now for ind in login_indicators):
                    logger.warning(f"[screenshot] Session lost on {current_url} — re-authenticating")
                    try:
                        re_login_ok = await _do_login(context, auth_cfg)
                        if re_login_ok:
                            await page.goto(current_url, wait_until="domcontentloaded", timeout=20000)
                            await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception as re_auth_err:
                        logger.warning(f"[screenshot] Re-auth failed: {re_auth_err}")

                # Hide cookie banners / overlays that block the real UI
                await page.evaluate("""() => {
                    const selectors = [
                        '[class*="cookie"]', '[id*="cookie"]',
                        '[class*="consent"]', '[id*="consent"]',
                        '.modal-backdrop', '.overlay',
                    ];
                    for (const sel of selectors) {
                        document.querySelectorAll(sel)
                            .forEach(el => el.style.display = 'none');
                    }
                }""")
                await page.wait_for_timeout(2000)
                await page.screenshot(path=screenshot_path, full_page=True, timeout=15000)
            except Exception:
                screenshot_path = None

            # ── Link classification ────────────────────────────────────────
            link_data = await classify_links(page, base_url, context)
            link_data["failed_assets"]        = failed_assets_live
            link_data["third_party_failures"] = third_party_failures_live
            internal_links                    = link_data["internal_links"]

            # ── Scoring ────────────────────────────────────────────────────
            perf_score_data  = compute_performance_score(perf_raw)  if perf_raw  else {}
            a11y_score_data  = compute_accessibility_score(a11y_raw) if a11y_raw  else {}
            sec_score_data   = compute_security_score(security_raw)  if security_raw else {}

            analyzed_forms   = analyze_all_forms(dom_data.get("forms", []))

            func_input = {
                "broken_navigation_links": link_data["broken_navigation_links"],
                "js_errors":               js_errors,
                "status":                  status,
            }
            func_score_data = compute_functional_score(func_input)

            ui_form_input = {
                "forms":       analyzed_forms,
                "ui_elements": dom_data.get("ui_elements", []),
                "ui_summary":  dom_data.get("ui_summary", {}),
            }
            ui_form_score_data = compute_ui_form_score(ui_form_input)

            page_title = await page.title()

            page_obj = {
                "url":   current_url,
                "title": page_title,
                "status": status,
                "result": "pass" if (status or 200) < 400 else "fail",

                # UI / DOM
                "ui_elements": dom_data.get("ui_elements", []),
                "ui_summary":  dom_data.get("ui_summary", {}),
                "forms":       analyzed_forms,
                "dropdowns":   dom_data.get("dropdowns", []),
                "pagination":  dom_data.get("pagination", []),

                # ── ENRICHED: nav_menus now has full link objects ──────────
                "nav_menus":    dom_data.get("nav_menus", []),
                # ── NEW: sidebar_links for flow discovery ──────────────────
                "sidebar_links": dom_data.get("sidebar_links", []),

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

                # Functional — broken links separated
                "broken_navigation_links": link_data["broken_navigation_links"],
                "failed_assets":           link_data["failed_assets"],
                "third_party_failures":    link_data["third_party_failures"],
                "broken_links":            link_data["broken_navigation_links"],  # legacy alias

                # ── ENRICHED: JS errors now structured dicts ──────────────
                "js_errors":               js_errors,

                "failed_requests":         [],
                "redirect_chain_length":   redirect_count[0],

                # Screenshot
                "screenshot": screenshot_path,
            }

            # ── Compute health score ───────────────────────────────────────
            health_data = compute_page_health_score(
                performance_score   = perf_score_data.get("score"),
                accessibility_score = a11y_score_data.get("score"),
                security_score      = sec_score_data.get("score"),
                functional_score    = func_score_data.get("score"),
                ui_form_score       = ui_form_score_data.get("score"),
            )
            page_obj["health_score"]     = health_data.get("health_score")
            page_obj["health_breakdown"] = health_data.get("components", {})
            page_obj["risk_category"]    = health_data.get("risk_category")
            page_obj["functional_score"] = func_score_data.get("score")
            page_obj["ui_form_score"]    = ui_form_score_data.get("score")

            # ── AI / confidence enrichment ─────────────────────────────────
            enrich_page_with_ai_fields(page_obj, active_filters)

            page_data.append(page_obj)
            anomaly_det.record_success()

            # ── Queue new URLs discovered ──────────────────────────────────
            for link in internal_links:
                norm = normalize_url(link)
                if norm not in visited and norm not in queue:
                    if not _is_skip_url(norm, auth):
                        queue.append(norm)

            elapsed_ms = (time.time() - t_start) * 1000
            eta_tracker.record(elapsed_ms)

            if update_fn:
                try:
                    update_fn(
                        scanned=len(page_data),
                        total=max(len(page_data) + len(queue), len(page_data)),
                        discovered=len(visited) + len(queue),
                        avg_ms=eta_tracker.avg_ms(),
                        eta_seconds=eta_tracker.eta(len(queue)),
                    )
                except Exception:
                    pass

            logger.info(
                f"[{len(page_data)}] {current_url} → {status} "
                f"| health={page_obj.get('health_score')} "
                f"| js_errors={len(js_errors)} "
                f"| {elapsed_ms:.0f}ms"
            )

            # Rate limiting
            if CRAWL_DELAY_MS > 0:
                await asyncio.sleep(CRAWL_DELAY_MS / 1000.0)

        except Exception as e:
            logger.error(f"Page processing error for {current_url}: {e}", exc_info=True)
            anomaly_det.record_failure(current_url, str(e))
        finally:
            try:
                await page.close()
            except Exception:
                pass


# ── Report Builder ─────────────────────────────────────────────────────────────

async def build_reports(run_id: int, page_data: list, active_filters: list | None) -> dict:
    rows = []
    for pg in page_data:
        ui_s = pg.get("ui_summary") or {}
        rows.append({
            "URL":                  pg.get("url"),
            "Title":                pg.get("title"),
            "Status":               pg.get("status"),
            "Result":               pg.get("result"),
            "Health Score":         pg.get("health_score"),
            "Risk Category":        pg.get("risk_category"),
            "Confidence Score":     pg.get("confidence_score"),
            "Performance Score":    pg.get("performance_score"),
            "Accessibility Score":  pg.get("accessibility_score"),
            "Security Score":       pg.get("security_score"),
            "Functional Score":     pg.get("functional_score"),
            "UI/Form Score":        pg.get("ui_form_score"),
            "Load Time (ms)":       pg.get("load_time"),
            "FCP (ms)":             pg.get("fcp_ms"),
            "LCP (ms)":             pg.get("lcp_ms"),
            "TTFB (ms)":            pg.get("ttfb_ms"),
            "Accessibility Issues": pg.get("accessibility_issues"),
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
    run_id: int,
    url: str,
    user_id: int,
    page_limit,
    update_fn=None,
    active_filters=None,
) -> dict:
    """
    Entry point called by tasks.run_scan.
    Returns result dict — NEVER raises; unreachable targets return
    a structured failure payload so tasks.py can persist diagnostics.
    """

    # ── Pre-flight reachability check ──────────────────────────────────────
    try:
        preflight_check(url)
    except TargetUnreachableError as e:
        logger.error(f"[run {run_id}] Pre-flight failed: {e}")
        # Return a structured failure dict — tasks.py handles status
        return {
            "status": "target_unreachable",
            "error_detail": str(e),
            "pages": [],
            "site_health": {},
            "confidence": 0.0,
            "ai_summary": None,
        }

    # ── Normal Playwright crawl continues below ─────────────────────────────
    visited: set    = set()
    page_data: list = []
    resolved_filters = active_filters or list(VALID_FILTERS)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            ignore_https_errors=True,
        )

        # Attempt login if credentials are configured
        auth = _read_auth_config()
        if auth:
            login_ok = await _do_login(context, auth)
            if login_ok:
                logger.info("[auth] Session established — crawling as authenticated user")
                success_path = auth.get("success_url", "").strip()
                if success_path:
                    from urllib.parse import urlparse as _up
                    _p = _up(url)
                    url = f"{_p.scheme}://{_p.netloc}{success_path}"
                    logger.info(f"[auth] Crawl start redirected to: {url}")
            else:
                logger.warning("[auth] Login failed — crawling as unauthenticated user")

        try:
            await crawl_site(
                context=context,
                base_url=url,
                run_id=run_id,
                visited=visited,
                page_data=page_data,
                max_pages=page_limit,
                active_filters=resolved_filters,
                update_fn=update_fn,
            )
        finally:
            await browser.close()

    # Build reports and return structured result dict
    return await build_reports(run_id, page_data, resolved_filters)


def run_crawler(run_id, url, user_id, page_limit=None, update_fn=None, active_filters=None):
    """Sync entry point called by RQ worker via tasks.py."""
    return asyncio.run(
        main(
            run_id=run_id,
            url=url,
            user_id=user_id,
            page_limit=page_limit,
            update_fn=update_fn,
            active_filters=active_filters,
        )
    )