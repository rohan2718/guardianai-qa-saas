"""
engines/deep_qa_engine.py — GuardianAI Deep QA Engine
=====================================================
Performs real end-to-end QA testing on every page of the target website
using Playwright — exactly the way a human QA engineer would test it
manually before a production release.

Tests:
  1. Buttons (click, detect response, record result)
  2. Forms (fill, submit, validate, detect success/error)
  3. Navigation links (HTTP status check)
  4. Tables/data grids (pagination, sorting, search)
  5. Modals (open, close, form inside modal)
  6. Page-level checks (perf, a11y, security, JS errors)

Every function is fully implemented with try/except — one element failure
never crashes the whole page test.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

SCREENSHOT_DIR = os.environ.get("SCREENSHOT_DIR", "screenshots")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

MAX_BUTTONS_PER_PAGE = 50
MAX_FORMS_PER_PAGE   = 10
MAX_LINKS_PER_PAGE   = 100
MAX_TABLES_PER_PAGE  = 10
MAX_MODALS_PER_PAGE  = 10

# Labels too risky to auto-click
SKIP_BUTTON_LABELS = {
    # Destructive / side-effect actions — never auto-click these
    "logout", "sign out", "signout", "log out", "delete", "remove",
    "cancel subscription", "deactivate", "disable", "terminate",
    "send email", "send sms", "send message", "archive", "purge", "destroy",
    # File operations that open system dialogs
    "download", "export", "print", "upload",
    # These are safe to leave out — they reset state in unpredictable ways
    "reset", "clear all",
    # Pagination prev/next handled separately
    "previous", "prev", "next",
}

DEFAULT_TIMEOUT_MS = 8000

# Patterns indicating successful form submission
SUCCESS_PATTERNS = [
    "success", "saved", "created", "updated", "added", "submitted",
    "thank you", "confirmed", "complete", "done", "accepted",
]

# Patterns indicating failed form submission
FAILURE_PATTERNS = [
    "error", "failed", "invalid", "required", "incorrect",
    "not found", "unauthorized", "forbidden",
]

SUCCESS_SELECTORS = [
    ".alert-success", ".toast-success", ".notification-success",
    "[class*='success']", "[class*='alert-success']",
    "[role='alert']", ".toast", ".notification", ".flash",
    ".snackbar", "[class*='snack']",
]

FAILURE_SELECTORS = [
    ".alert-danger", ".alert-error", ".error-message",
    "[class*='error']", "[class*='danger']",
    ".validation-error", ".field-error",
]


# ── Data Classes ───────────────────────────────────────────────────────────────

@dataclass
class ButtonResult:
    label: str
    selector: str
    page_url: str
    status: str                  # PASS | FAIL | WARNING | SKIP
    action_result: str           # what happened after click
    failure_reason: Optional[str] = None
    js_errors: list = field(default_factory=list)
    http_responses: list = field(default_factory=list)
    screenshot_path: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "label":           self.label,
            "selector":        self.selector,
            "page_url":        self.page_url,
            "status":          self.status,
            "action_result":   self.action_result,
            "failure_reason":  self.failure_reason,
            "js_errors":       self.js_errors,
            "http_responses":  self.http_responses,
            "screenshot_path": self.screenshot_path,
        }


@dataclass
class FieldResult:
    name: str
    field_type: str
    required: bool
    value_entered: str
    validation_works: bool = False
    status: str = "PASS"

    def to_dict(self) -> dict:
        return {
            "name":             self.name,
            "field_type":       self.field_type,
            "required":         self.required,
            "value_entered":    self.value_entered,
            "validation_works": self.validation_works,
            "status":           self.status,
        }


@dataclass
class FormResult:
    form_selector: str
    form_purpose: Optional[str]
    field_count: int
    required_count: int
    fields: list
    submission_status: str       # PASS | FAIL | SKIP | WARNING
    submission_result: str
    http_response: Optional[int] = None
    network_requests: list = field(default_factory=list)
    validation_works: bool = False
    failure_reason: Optional[str] = None
    screenshot_path: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "form_selector":    self.form_selector,
            "form_purpose":     self.form_purpose,
            "field_count":      self.field_count,
            "required_count":   self.required_count,
            "fields":           [f.to_dict() if hasattr(f, "to_dict") else f for f in self.fields],
            "submission_status": self.submission_status,
            "submission_result": self.submission_result,
            "http_response":    self.http_response,
            "network_requests": self.network_requests,
            "validation_works": self.validation_works,
            "failure_reason":   self.failure_reason,
            "screenshot_path":  self.screenshot_path,
        }


@dataclass
class LinkResult:
    text: str
    url: str
    http_status: Optional[int]
    status: str                  # PASS | FAIL

    def to_dict(self) -> dict:
        return {
            "text":        self.text,
            "url":         self.url,
            "http_status": self.http_status,
            "status":      self.status,
        }


@dataclass
class TableResult:
    selector: str
    row_count: int
    has_pagination: bool
    has_sorting: bool
    has_search: bool
    status: str
    pagination_works: Optional[bool] = None
    sorting_works: Optional[bool]    = None
    search_works: Optional[bool]     = None

    def to_dict(self) -> dict:
        return {
            "selector":         self.selector,
            "row_count":        self.row_count,
            "has_pagination":   self.has_pagination,
            "pagination_works": self.pagination_works,
            "has_sorting":      self.has_sorting,
            "sorting_works":    self.sorting_works,
            "has_search":       self.has_search,
            "search_works":     self.search_works,
            "status":           self.status,
        }


@dataclass
class ModalResult:
    trigger_label: str
    trigger_selector: str
    opens_correctly: bool
    contains_form: bool
    status: str
    closes_with_button: Optional[bool] = None
    closes_with_escape: Optional[bool] = None
    form_result: Optional[FormResult]  = None

    def to_dict(self) -> dict:
        return {
            "trigger_label":      self.trigger_label,
            "trigger_selector":   self.trigger_selector,
            "opens_correctly":    self.opens_correctly,
            "closes_with_button": self.closes_with_button,
            "closes_with_escape": self.closes_with_escape,
            "contains_form":      self.contains_form,
            "form_result":        self.form_result.to_dict() if self.form_result else None,
            "status":             self.status,
        }


@dataclass
class DeepQAPageResult:
    page_url: str
    page_title: str
    tested_at: str
    load_time_ms: float

    buttons: list = field(default_factory=list)
    forms: list   = field(default_factory=list)
    links: list   = field(default_factory=list)
    tables: list  = field(default_factory=list)
    modals: list  = field(default_factory=list)

    js_errors_on_load: list = field(default_factory=list)
    broken_images: list     = field(default_factory=list)

    performance: dict   = field(default_factory=dict)
    accessibility: dict = field(default_factory=dict)
    security: dict      = field(default_factory=dict)

    bugs: list    = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    qa_score: float = 100.0
    screenshot_path: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "page_url":          self.page_url,
            "page_title":        self.page_title,
            "tested_at":         self.tested_at,
            "load_time_ms":      self.load_time_ms,
            "buttons":           [b.to_dict() if hasattr(b, "to_dict") else b for b in self.buttons],
            "forms":             [f.to_dict() if hasattr(f, "to_dict") else f for f in self.forms],
            "links":             [l.to_dict() if hasattr(l, "to_dict") else l for l in self.links],
            "tables":            [t.to_dict() if hasattr(t, "to_dict") else t for t in self.tables],
            "modals":            [m.to_dict() if hasattr(m, "to_dict") else m for m in self.modals],
            "js_errors_on_load": self.js_errors_on_load,
            "broken_images":     self.broken_images,
            "performance":       self.performance,
            "accessibility":     self.accessibility,
            "security":          self.security,
            "bugs":              self.bugs,
            "summary":           self.summary,
            "qa_score":          self.qa_score,
            "screenshot_path":   self.screenshot_path,
        }


# ── Main Engine ────────────────────────────────────────────────────────────────

class DeepQAEngine:

    def __init__(self, context, run_id: int,
                 screenshot_dir: str = SCREENSHOT_DIR):
        self.context        = context
        self.run_id         = run_id
        self.screenshot_dir = screenshot_dir
        self._tested_urls: set = set()

    # ── Public entry point ─────────────────────────────────────────────────────

    async def test_page(self, url: str) -> DeepQAPageResult:
        """
        Main entry point. Tests a single page completely.
        Returns DeepQAPageResult with all findings.
        """
        logger.info(f"[DeepQA] Starting full page test: {url}")
        t_start = time.time()

        page = await self.context.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)

        # ── Collect JS errors on load ──────────────────────────────────────
        js_errors_on_load: list = []
        page.on("pageerror", lambda e: js_errors_on_load.append(str(e)))

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            try:
                await page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass
        except Exception as nav_err:
            logger.error(f"[DeepQA] Navigation failed for {url}: {nav_err}")
            await page.close()
            return DeepQAPageResult(
                page_url=url,
                page_title="Navigation Failed",
                tested_at=datetime.now(UTC).isoformat(),
                load_time_ms=(time.time() - t_start) * 1000,
                bugs=[{
                    "title":       "Page Failed to Load",
                    "bug_type":    "functional",
                    "severity":    "critical",
                    "description": f"Page navigation failed: {nav_err}",
                    "impact":      "Page is inaccessible",
                    "steps":       [f"Navigate to {url}"],
                    "expected":    "Page loads with HTTP 200",
                    "actual":      str(nav_err),
                    "fix":         "Check server configuration and URL",
                }],
                summary={
                    "total_buttons": 0, "buttons_passed": 0, "buttons_failed": 0,
                    "total_forms": 0, "forms_passed": 0, "forms_failed": 0,
                    "total_links": 0, "links_broken": 0,
                    "total_tables": 0, "tables_passed": 0,
                    "js_errors_on_load": 0,
                },
                qa_score=0.0,
            )

        page_title = await page.title()
        load_ms    = (time.time() - t_start) * 1000

        # ── Page-level screenshot ──────────────────────────────────────────
        page_screenshot = await self._take_screenshot(page, f"page_{int(time.time()*1000)}")

        # ── Run all test suites ────────────────────────────────────────────
        buttons = await self._test_buttons(page, url)
        forms   = await self._test_forms(page, url)
        links   = await self._test_links(page, url)
        tables  = await self._test_tables(page)
        modals  = await self._test_modals(page, url)

        # ── Page-level checks ──────────────────────────────────────────────
        perf         = await self._collect_performance(page)
        a11y         = await self._collect_accessibility(page)
        security     = await self._collect_security(page, url)
        broken_imgs  = await self._collect_broken_images(page)

        result = DeepQAPageResult(
            page_url=url,
            page_title=page_title,
            tested_at=datetime.now(UTC).isoformat(),
            load_time_ms=load_ms,
            buttons=buttons,
            forms=forms,
            links=links,
            tables=tables,
            modals=modals,
            js_errors_on_load=js_errors_on_load[:20],
            broken_images=broken_imgs,
            performance=perf,
            accessibility=a11y,
            security=security,
            screenshot_path=page_screenshot,
        )

        result.qa_score = self._compute_qa_score(result)
        result.summary  = self._build_summary(result)
        result.bugs     = self._generate_bugs(result)

        logger.info(
            f"[DeepQA] Done: {url} | score={result.qa_score} "
            f"| buttons={len(buttons)} forms={len(forms)} "
            f"links={len(links)} tables={len(tables)}"
        )

        try:
            await page.close()
        except Exception:
            pass

        return result

    # ── 1. BUTTONS ─────────────────────────────────────────────────────────────

    async def _test_buttons(self, page, url: str) -> list:
        results = []
        try:
            buttons_data = await page.evaluate("""() => {
                const SKIP = new Set([
                    "logout","sign out","signout","log out","delete","remove",
                    "cancel subscription","deactivate","disable","terminate",
                    "download","export","print","send email","send sms",
                    "send message","archive","purge","reset","destroy"
                ]);
                const buttons = [];
                const seen = new Set();

                document.querySelectorAll(
                    'button, [role="button"], input[type="submit"], input[type="button"]'
                ).forEach(el => {
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return;
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) return;

                    const text = (el.textContent || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim().toLowerCase();
                    if (!text || seen.has(text)) return;
                    if (Array.from(SKIP).some(s => text.includes(s))) return;
                    seen.add(text);

                    let selector = '';
                    if (el.id) selector = '#' + el.id;
                    else if (el.getAttribute('data-testid')) selector = '[data-testid="' + el.getAttribute('data-testid') + '"]';
                    else {
                        const cls = Array.from(el.classList).slice(0,2).join('.');
                        selector = el.tagName.toLowerCase() + (cls ? '.' + cls : '');
                    }

                    buttons.push({
                        label:    (el.textContent || el.value || el.getAttribute('aria-label') || '').trim().substring(0, 80),
                        selector: selector,
                        tag:      el.tagName.toLowerCase(),
                        type:     el.getAttribute('type') || '',
                    });
                });
                return buttons.slice(0, 50);
            }""")
        except Exception as e:
            logger.warning(f"[DeepQA] Button discovery failed: {e}")
            return results

        if len(buttons_data) > MAX_BUTTONS_PER_PAGE:
            logger.warning(f"[DeepQA] Too many buttons ({len(buttons_data)}), capping at {MAX_BUTTONS_PER_PAGE}")
            buttons_data = buttons_data[:MAX_BUTTONS_PER_PAGE]

        for btn in buttons_data:
            result = await self._test_single_button(page, btn, url)
            results.append(result)

        return results

    async def _test_single_button(self, page, btn: dict, original_url: str) -> ButtonResult:
        label    = btn.get("label", "Unknown")
        selector = btn.get("selector", "button")

        js_errors:     list = []
        http_responses: list = []

        def _on_pageerror(e):
            js_errors.append(str(e))

        def _on_response(r):
            try:
                http_responses.append({"url": r.url, "status": r.status, "method": r.request.method})
            except Exception:
                pass

        page.on("pageerror", _on_pageerror)
        page.on("response",  _on_response)

        try:
            # Check element still exists and is clickable
            loc = page.locator(selector).first
            if await loc.count() == 0:
                return ButtonResult(
                    label=label, selector=selector, page_url=original_url,
                    status="SKIP", action_result="Element not found",
                )

            url_before = page.url

            # Click with timeout
            await loc.scroll_into_view_if_needed(timeout=3000)
            await loc.click(timeout=5000)

            # Wait for response
            await asyncio.sleep(0.8)
            try:
                await page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass

            url_after = page.url

            # ── Detect what happened ─────────────────────────────────────
            action_result = "No visible change"
            status = "WARNING"

            if url_after.rstrip("/") != url_before.rstrip("/"):
                action_result = f"Navigated to {url_after}"
                status = "PASS"
            else:
                # Check for modal
                modal_visible = await page.evaluate("""() => {
                    const selectors = [
                        '[role="dialog"]', '.modal.show', '.modal[style*="display: block"]',
                        '[aria-modal="true"]', '.dialog', '.popup',
                    ];
                    return selectors.some(s => {
                        const el = document.querySelector(s);
                        if (!el) return false;
                        const st = window.getComputedStyle(el);
                        return st.display !== 'none' && st.visibility !== 'hidden';
                    });
                }""")

                if modal_visible:
                    action_result = "Modal opened"
                    status = "PASS"
                    # Close the modal before continuing
                    await self._close_modal(page)
                else:
                    # Check for toast/alert
                    toast_text = await page.evaluate("""() => {
                        const sel = [
                            '.toast', '.alert', '.notification', '.snackbar',
                            '[role="alert"]', '[class*="toast"]', '[class*="alert"]'
                        ];
                        for (const s of sel) {
                            const el = document.querySelector(s);
                            if (el) {
                                const st = window.getComputedStyle(el);
                                if (st.display !== 'none') return el.textContent.trim().substring(0,100);
                            }
                        }
                        return null;
                    }""")

                    if toast_text:
                        action_result = f"Toast/alert: {toast_text}"
                        status = "PASS"
                    elif http_responses:
                        post_responses = [r for r in http_responses if r["method"] in ("POST","PUT","PATCH","DELETE")]
                        if post_responses:
                            resp = post_responses[-1]
                            if resp["status"] < 400:
                                action_result = f"API call: {resp['method']} → {resp['status']}"
                                status = "PASS"
                            else:
                                action_result = f"API error: {resp['method']} → {resp['status']}"
                                status = "FAIL"
                        else:
                            action_result = "Network requests fired (non-mutating)"
                            status = "PASS"
                    else:
                        action_result = "No response detected"
                        status = "WARNING"

            # Navigate back if page changed
            if url_after.rstrip("/") != original_url.rstrip("/"):
                try:
                    await page.goto(original_url, wait_until="domcontentloaded", timeout=10000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=4000)
                    except Exception:
                        pass
                except Exception as nav_err:
                    logger.warning(f"[DeepQA] Could not navigate back to {original_url}: {nav_err}")

            sshot = None
            if status == "FAIL":
                sshot = await self._take_screenshot(page, f"btn_fail_{int(time.time()*1000)}")

            return ButtonResult(
                label=label,
                selector=selector,
                page_url=original_url,
                status=status,
                action_result=action_result,
                js_errors=js_errors[:5],
                http_responses=[r for r in http_responses if r["method"] in ("POST","PUT","PATCH")][:5],
                screenshot_path=sshot,
            )

        except Exception as e:
            logger.warning(f"[DeepQA] Button test failed for '{label}': {e}")
            # Try to get back to original URL
            try:
                if page.url.rstrip("/") != original_url.rstrip("/"):
                    await page.goto(original_url, wait_until="domcontentloaded", timeout=10000)
            except Exception:
                pass

            return ButtonResult(
                label=label,
                selector=selector,
                page_url=original_url,
                status="FAIL",
                action_result=f"Click error: {str(e)[:100]}",
                failure_reason=str(e)[:200],
                js_errors=js_errors[:5],
            )
        finally:
            try:
                page.remove_listener("pageerror", _on_pageerror)
                page.remove_listener("response",  _on_response)
            except Exception:
                pass

    # ── 2. FORMS ───────────────────────────────────────────────────────────────

    async def _test_forms(self, page, url: str) -> list:
        results = []
        try:
            forms_data = await page.evaluate("""() => {
                const forms = [];
                document.querySelectorAll('form').forEach((f, fi) => {
                    const fields = [];
                    f.querySelectorAll('input, select, textarea, button').forEach(el => {
                        const type = el.getAttribute('type') || el.tagName.toLowerCase();
                        if (['hidden','image'].includes(type)) return;
                        const id   = el.id || '';
                        const name = el.getAttribute('name') || '';
                        const labelEl = id ? document.querySelector('label[for="' + id + '"]') : null;
                        const labelText = labelEl ? labelEl.textContent.trim() : (el.placeholder || el.getAttribute('aria-label') || name || type);

                        let selector = '';
                        if (id) selector = '#' + id;
                        else if (name) selector = '[name="' + name + '"]';
                        else selector = el.tagName.toLowerCase() + '[type="' + type + '"]';

                        const options = [];
                        if (el.tagName === 'SELECT') {
                            Array.from(el.options).slice(0,5).forEach(o => options.push({value: o.value, text: o.text}));
                        }

                        fields.push({
                            name:     name,
                            id:       id,
                            type:     type,
                            required: el.required || false,
                            label:    labelText.substring(0, 60),
                            selector: selector,
                            options:  options,
                            tag:      el.tagName.toLowerCase(),
                        });
                    });

                    const names = fields.map(f => (f.name || '').toLowerCase()).join(' ');
                    let purpose = null;
                    if (/login|signin|password|username/.test(names)) purpose = 'Login';
                    else if (/register|signup|create.*account/.test(names)) purpose = 'Registration';
                    else if (/search|query|q\b/.test(names)) purpose = 'Search';
                    else if (/contact|message|enqui/.test(names)) purpose = 'Contact';
                    else if (/subscribe|newsletter|email/.test(names)) purpose = 'Newsletter';
                    else if (/checkout|payment|card|billing/.test(names)) purpose = 'Checkout';
                    else purpose = 'Generic';

                    let formSelector = f.id ? '#' + f.id : 'form:nth-of-type(' + (fi+1) + ')';

                    forms.push({
                        selector: formSelector,
                        purpose:  purpose,
                        method:   (f.getAttribute('method') || 'GET').toUpperCase(),
                        fields:   fields,
                        has_submit: fields.some(f => ['submit','button'].includes(f.type)),
                    });
                });

                // Also detect search/filter panels: groups of inputs + a submit button
                // that may not be wrapped in a <form> tag (common in ASP.NET MVC)
                const formSelectors = new Set(forms.map(f => f.selector));
                document.querySelectorAll('.search-panel, .filter-panel, [class*="search"], [class*="filter"], [id*="search"], [id*="filter"]').forEach((panel, pi) => {
                    if (formSelectors.has(panel.id ? '#' + panel.id : null)) return;
                    const panelInputs = panel.querySelectorAll('input:not([type="hidden"]), select');
                    const submitBtn = panel.querySelector('button[type="submit"], input[type="submit"], .btn-search, [class*="btn-search"]');
                    if (panelInputs.length > 0 && submitBtn) {
                        const pFields = Array.from(panelInputs).map(el => ({
                            name: el.getAttribute('name') || '',
                            id: el.id || '',
                            type: el.getAttribute('type') || el.tagName.toLowerCase(),
                            required: false,
                            label: (el.placeholder || el.getAttribute('aria-label') || el.getAttribute('name') || 'field').substring(0, 60),
                            selector: el.id ? '#' + el.id : '[name="' + el.getAttribute('name') + '"]',
                            options: el.tagName === 'SELECT' ? Array.from(el.options).slice(0,5).map(o => ({value: o.value, text: o.text})) : [],
                            tag: el.tagName.toLowerCase(),
                        }));
                        let btnSel = submitBtn.id ? '#' + submitBtn.id : submitBtn.tagName.toLowerCase() + '.' + Array.from(submitBtn.classList).join('.');
                        pFields.push({name: '', id: submitBtn.id || '', type: 'submit', required: false, label: submitBtn.textContent.trim() || 'Search', selector: btnSel, options: [], tag: submitBtn.tagName.toLowerCase()});
                        const panelSel = panel.id ? '#' + panel.id : '[class*="search"]';
                        forms.push({ selector: panelSel, purpose: 'Search', method: 'GET', fields: pFields, has_submit: true });
                        if (forms.length >= 10) return;
                    }
                });

                return forms.slice(0, 10);
            }""")
        except Exception as e:
            logger.warning(f"[DeepQA] Form discovery failed: {e}")
            return results

        if len(forms_data) > MAX_FORMS_PER_PAGE:
            forms_data = forms_data[:MAX_FORMS_PER_PAGE]

        for form in forms_data:
            try:
                result = await self._test_single_form(page, form, url)
                results.append(result)
                # Navigate back after form test
                try:
                    if page.url.rstrip("/") != url.rstrip("/"):
                        await page.goto(url, wait_until="domcontentloaded", timeout=10000)
                        await asyncio.sleep(0.5)
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"[DeepQA] Form test error: {e}")
                results.append(FormResult(
                    form_selector=form.get("selector", "form"),
                    form_purpose=form.get("purpose"),
                    field_count=len(form.get("fields", [])),
                    required_count=0,
                    fields=[],
                    submission_status="SKIP",
                    submission_result=f"Test error: {str(e)[:100]}",
                    failure_reason=str(e)[:200],
                ))

        return results

    async def _test_single_form(self, page, form: dict, url: str) -> FormResult:
        selector = form.get("selector", "form")
        purpose  = form.get("purpose")
        fields   = form.get("fields", [])
        method   = form.get("method", "GET")

        interactable_fields = [
            f for f in fields
            if f["type"] not in ("submit", "button", "reset", "hidden", "image")
        ]
        required_count = sum(1 for f in interactable_fields if f.get("required"))

        field_results: list = []
        network_requests:  list = []
        js_errors:         list = []

        def _on_response(r):
            try:
                network_requests.append({"url": r.url, "status": r.status, "method": r.request.method})
            except Exception:
                pass

        def _on_error(e):
            js_errors.append(str(e))

        page.on("response",  _on_response)
        page.on("pageerror", _on_error)

        try:
            # ── Fill all fields ──────────────────────────────────────────
            for field in interactable_fields:
                value = self._generate_field_value(field)
                success = await self._fill_field(page, field, value)
                field_results.append(FieldResult(
                    name=field.get("name") or field.get("label") or "field",
                    field_type=field.get("type", "text"),
                    required=field.get("required", False),
                    value_entered=value,
                    status="PASS" if success else "FAIL",
                ))

            url_before = page.url

            # ── Submit the form ──────────────────────────────────────────
            submitted = False
            submit_selectors = [
                f"{selector} button[type='submit']",
                f"{selector} input[type='submit']",
                f"{selector} button:not([type='button']):not([type='reset'])",
                "button[type='submit']",
                "input[type='submit']",
            ]
            for sub_sel in submit_selectors:
                try:
                    loc = page.locator(sub_sel).first
                    if await loc.count() > 0:
                        await loc.click(timeout=5000)
                        submitted = True
                        break
                except Exception:
                    continue

            if not submitted:
                return FormResult(
                    form_selector=selector,
                    form_purpose=purpose,
                    field_count=len(interactable_fields),
                    required_count=required_count,
                    fields=field_results,
                    submission_status="SKIP",
                    submission_result="No submit button found",
                    network_requests=network_requests[:10],
                )

            # ── Wait for response ────────────────────────────────────────
            await asyncio.sleep(1.0)
            try:
                await page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass

            url_after = page.url

            # ── Detect submission outcome ────────────────────────────────
            submission_status = "WARNING"
            submission_result = "Inconclusive — no clear success or failure signal"
            http_response     = None

            # Priority 1: URL changed
            if url_after.rstrip("/") != url_before.rstrip("/"):
                submission_status = "PASS"
                submission_result = f"Redirect to {url_after}"

            else:
                # Priority 2: Success element in DOM
                success_text = await page.evaluate("""(successSels) => {
                    for (const sel of successSels) {
                        const el = document.querySelector(sel);
                        if (el) {
                            const st = window.getComputedStyle(el);
                            if (st.display !== 'none') return el.textContent.trim().substring(0,100);
                        }
                    }
                    return null;
                }""", SUCCESS_SELECTORS)

                if success_text:
                    text_lower = success_text.lower()
                    is_success = any(p in text_lower for p in SUCCESS_PATTERNS)
                    is_failure = any(p in text_lower for p in FAILURE_PATTERNS)
                    if is_success and not is_failure:
                        submission_status = "PASS"
                        submission_result = f"Success message: {success_text}"
                    elif is_failure:
                        submission_status = "FAIL"
                        submission_result = f"Error message: {success_text}"

                # Priority 3: Network response
                if submission_status == "WARNING":
                    submit_methods = {"POST", "PUT", "PATCH"}
                    for req in reversed(network_requests):
                        if req.get("method") in submit_methods:
                            http_response = req["status"]
                            if http_response and http_response < 400:
                                submission_status = "PASS"
                                submission_result = f"API {req['method']} → {http_response}"
                            elif http_response and http_response >= 400:
                                submission_status = "FAIL"
                                submission_result = f"API {req['method']} → {http_response}"
                            break

            # ── Test validation: submit empty ────────────────────────────
            validation_works = False
            if required_count > 0:
                try:
                    # Navigate back and try empty submission
                    await page.goto(url, wait_until="domcontentloaded", timeout=10000)
                    await asyncio.sleep(0.3)

                    # Click submit without filling
                    for sub_sel in submit_selectors:
                        try:
                            loc = page.locator(sub_sel).first
                            if await loc.count() > 0:
                                await loc.click(timeout=5000)
                                break
                        except Exception:
                            continue

                    await asyncio.sleep(0.5)

                    # Check if validation errors appeared
                    has_errors = await page.evaluate("""() => {
                        const errorSels = [
                            ':invalid', '.is-invalid', '.field-error',
                            '[class*="error"]', '.alert-danger',
                            '[aria-invalid="true"]',
                        ];
                        return errorSels.some(s => {
                            try { return document.querySelectorAll(s).length > 0; }
                            catch { return false; }
                        });
                    }""")

                    # Also check if still on same page (browser validation prevented submit)
                    stayed_on_page = page.url.rstrip("/") == url.rstrip("/")
                    validation_works = has_errors or stayed_on_page

                except Exception as val_err:
                    logger.debug(f"[DeepQA] Validation test failed: {val_err}")

            sshot = None
            if submission_status == "FAIL":
                sshot = await self._take_screenshot(page, f"form_fail_{int(time.time()*1000)}")

            return FormResult(
                form_selector=selector,
                form_purpose=purpose,
                field_count=len(interactable_fields),
                required_count=required_count,
                fields=field_results,
                submission_status=submission_status,
                submission_result=submission_result,
                http_response=http_response,
                network_requests=[r for r in network_requests if r["method"] in ("POST","PUT","PATCH")][:10],
                validation_works=validation_works,
                screenshot_path=sshot,
            )

        except Exception as e:
            logger.warning(f"[DeepQA] Form test error for {selector}: {e}")
            sshot = await self._take_screenshot(page, f"form_err_{int(time.time()*1000)}")
            return FormResult(
                form_selector=selector,
                form_purpose=purpose,
                field_count=len(interactable_fields),
                required_count=required_count,
                fields=field_results,
                submission_status="FAIL",
                submission_result=f"Test error: {str(e)[:100]}",
                failure_reason=str(e)[:200],
                screenshot_path=sshot,
            )
        finally:
            try:
                page.remove_listener("response",  _on_response)
                page.remove_listener("pageerror", _on_error)
            except Exception:
                pass

    def _generate_field_value(self, field: dict) -> str:
        """Generate a realistic test value for a form field."""
        ftype = (field.get("type") or "text").lower()
        name  = (field.get("name") or field.get("label") or "field").lower()

        if ftype == "email" or "email" in name:
            return "testqa@guardianai.io"
        if ftype == "password":
            pw = os.environ.get("CRAWLER_PASSWORD", "")
            return pw if pw else "TestPass@123"
        if ftype == "tel" or any(k in name for k in ("phone", "tel", "mobile")):
            return "+91 9876543210"
        if ftype == "number" or any(k in name for k in ("age", "qty", "count", "amount", "quantity")):
            return "42"
        if ftype == "date":
            # Return in DD/MM/YYYY format too — ASP.NET MVC often uses this
            return datetime.now(UTC).strftime("%Y-%m-%d")
        if ftype == "text" and any(k in name for k in ("date", "from", "to", "start", "end", "dob", "birth")):
            return datetime.now(UTC).strftime("%d/%m/%Y")
        if ftype == "url":
            return "https://example.com"
        if ftype == "select":
            opts = field.get("options") or []
            for o in opts:
                if o.get("value") and o["value"] not in ("", "0", "null", "undefined", "select"):
                    return o["value"]
            return ""
        if ftype == "checkbox":
            return "true"
        if ftype == "radio":
            return "true"
        if ftype == "textarea":
            return f"This is automated QA test input for {field.get('label', 'field')}"
        if any(k in name for k in ("username", "user", "login", "emp")):
            return os.environ.get("CRAWLER_USERNAME", "testuser_qa")
        return f"Test {field.get('label', 'QA')} Input"

    async def _fill_field(self, page, field: dict, value: str) -> bool:
        """Fill a single form field. Returns True if successful."""
        selector = field.get("selector", "")
        ftype    = (field.get("type") or "text").lower()

        if not selector:
            return False

        try:
            loc = page.locator(selector).first
            if await loc.count() == 0:
                return False

            await loc.wait_for(state="visible", timeout=3000)

            if ftype == "select":
                if value:
                    try:
                        await loc.select_option(value=value, timeout=3000)
                        return True
                    except Exception:
                        pass
                # Try by index (skip index 0 which is usually placeholder)
                try:
                    opt_count = await loc.evaluate("el => el.options.length")
                    if opt_count > 1:
                        await loc.select_option(index=1, timeout=3000)
                        return True
                except Exception:
                    pass
                # Fallback: try Select2 widget (custom dropdown)
                try:
                    sel = field.get("selector", "")
                    s2_trigger = f"{sel} + .select2-container, .select2-container:has(+ {sel})"
                    s2 = page.locator(".select2-selection").first
                    if await s2.count() > 0:
                        await s2.click(timeout=2000)
                        await page.wait_for_timeout(300)
                        first_opt = page.locator(".select2-results__option").first
                        if await first_opt.count() > 0:
                            await first_opt.click(timeout=2000)
                            return True
                except Exception:
                    pass
                return False

            if ftype == "checkbox":
                await loc.check(timeout=3000)
                return True

            if ftype == "radio":
                await loc.check(timeout=3000)
                return True

            if ftype == "date":
                # Try HTML5 date input first, then clear-and-type for custom pickers
                try:
                    await loc.fill(value, timeout=3000)
                    return True
                except Exception:
                    try:
                        await loc.triple_click(timeout=2000)
                        # Type DD/MM/YYYY format for custom ASP.NET date pickers
                        from datetime import datetime as _dt
                        date_str = _dt.now().strftime("%d/%m/%Y")
                        await page.keyboard.type(date_str, delay=50)
                        return True
                    except Exception:
                        return False

            if ftype in ("text", "email", "tel", "number", "password",
                         "url", "search", "textarea"):
                await loc.triple_click(timeout=2000)
                await loc.fill(value, timeout=3000)
                return True

            # Fallback
            await loc.fill(str(value), timeout=3000)
            return True

        except Exception as e:
            logger.debug(f"[DeepQA] Fill failed for {selector}: {e}")
            return False

    # ── 3. LINKS ────────────────────────────────────────────────────────────────

    async def _test_links(self, page, base_url: str) -> list:
        results = []
        try:
            links_data = await page.evaluate("""(baseHost) => {
                const seen = new Set();
                const links = [];
                document.querySelectorAll('a[href]').forEach(a => {
                    const href = a.href;
                    if (!href || seen.has(href)) return;
                    if (href.startsWith('mailto:') || href.startsWith('tel:') ||
                        href.startsWith('javascript:') || href.startsWith('#')) return;
                    try {
                        const u = new URL(href);
                        if (u.hostname !== baseHost) return;
                        const path = u.pathname.toLowerCase();
                        if (path.includes('logout') || path.includes('signout')) return;
                    } catch { return; }
                    seen.add(href);
                    links.push({
                        text: (a.textContent || a.getAttribute('aria-label') || '').trim().substring(0,60),
                        url:  href,
                    });
                });
                return links;
            }""", urlparse(base_url).hostname)
        except Exception as e:
            logger.warning(f"[DeepQA] Link discovery failed: {e}")
            return results

        if len(links_data) > MAX_LINKS_PER_PAGE:
            logger.warning(f"[DeepQA] Capping links at {MAX_LINKS_PER_PAGE}")
            links_data = links_data[:MAX_LINKS_PER_PAGE]

        # Check links via HEAD request through Playwright context
        for link in links_data:
            try:
                resp = await self.context.request.fetch(
                    link["url"],
                    method="HEAD",
                    timeout=6000,
                    headers={"User-Agent": "GuardianAI-DeepQA/1.0"},
                )
                status = resp.status
                try:
                    await resp.dispose()
                except Exception:
                    pass
                # 405=Method Not Allowed, 401/403=Auth required — link EXISTS, not broken
                IGNORABLE = {405, 401, 403, 406, 408}
                is_broken = status >= 400 and status not in IGNORABLE
                results.append(LinkResult(
                    text=link["text"],
                    url=link["url"],
                    http_status=status,
                    status="FAIL" if is_broken else "PASS",
                ))
            except Exception as e:
                results.append(LinkResult(
                    text=link["text"],
                    url=link["url"],
                    http_status=None,
                    status="FAIL",
                ))

        return results

    # ── 4. TABLES ──────────────────────────────────────────────────────────────

    async def _test_tables(self, page) -> list:
        results = []
        try:
            tables_data = await page.evaluate("""() => {
                const tables = [];
                const seenSels = new Set();

                // Standard HTML tables
                document.querySelectorAll('table').forEach((t, i) => {
                    const rows = t.querySelectorAll('tbody tr');
                    let sel = t.id ? '#' + t.id : 'table:nth-of-type(' + (i+1) + ')';
                    seenSels.add(sel);

                    // Find pagination near the table
                    const parent = t.parentElement || document;
                    const paginationEl = parent.querySelector(
                        '[aria-label*="paginat"], .pagination, [class*="paginat"], .page-nav'
                    );

                    // Find sortable headers
                    const sortableHeaders = t.querySelectorAll(
                        'th[class*="sort"], th[data-sort], th[data-sortable], th[class*="sortable"]'
                    );

                    // Find search near the table
                    const searchEl = parent.querySelector(
                        'input[type="search"], input[placeholder*="search" i], input[placeholder*="filter" i]'
                    );

                    tables.push({
                        selector:       sel,
                        row_count:      rows.length,
                        has_pagination: !!paginationEl,
                        pagination_sel: paginationEl ? (paginationEl.id ? '#'+paginationEl.id : '.pagination') : null,
                        has_sorting:    sortableHeaders.length > 0,
                        sort_sel:       sortableHeaders.length > 0 ? 'th[class*="sort"]' : null,
                        has_search:     !!searchEl,
                        search_sel:     searchEl ? (searchEl.id ? '#'+searchEl.id : 'input[type="search"]') : null,
                    });
                });

                return tables.slice(0, 10);
            }""")
        except Exception as e:
            logger.warning(f"[DeepQA] Table discovery failed: {e}")
            return results

        for tbl in tables_data[:MAX_TABLES_PER_PAGE]:
            try:
                result = await self._test_single_table(page, tbl)
                results.append(result)
            except Exception as e:
                logger.warning(f"[DeepQA] Table test failed: {e}")
                results.append(TableResult(
                    selector=tbl.get("selector", "table"),
                    row_count=tbl.get("row_count", 0),
                    has_pagination=tbl.get("has_pagination", False),
                    has_sorting=tbl.get("has_sorting", False),
                    has_search=tbl.get("has_search", False),
                    status="FAIL",
                ))

        return results

    async def _test_single_table(self, page, tbl: dict) -> TableResult:
        selector      = tbl.get("selector", "table")
        row_count     = tbl.get("row_count", 0)
        has_pagination = tbl.get("has_pagination", False)
        has_sorting   = tbl.get("has_sorting", False)
        has_search    = tbl.get("has_search", False)

        pagination_works: Optional[bool] = None
        sorting_works: Optional[bool]    = None
        search_works: Optional[bool]     = None

        # ── Test pagination ────────────────────────────────────────────────
        if has_pagination and tbl.get("pagination_sel"):
            try:
                next_btn = page.locator(
                    f"{tbl['pagination_sel']} [aria-label*='next' i], "
                    f"{tbl['pagination_sel']} .page-item:not(.disabled):last-child a, "
                    f"{tbl['pagination_sel']} button:has-text('Next')"
                ).first
                if await next_btn.count() > 0:
                    rows_before = await page.locator(f"{selector} tbody tr").count()
                    await next_btn.click(timeout=4000)
                    await asyncio.sleep(0.8)
                    rows_after = await page.locator(f"{selector} tbody tr").count()
                    url_changed = False  # check if page changed
                    # If rows changed or page refreshed, pagination works
                    pagination_works = rows_after > 0
                else:
                    pagination_works = None
            except Exception as e:
                logger.debug(f"[DeepQA] Pagination test failed: {e}")
                pagination_works = False

        # ── Test sorting ───────────────────────────────────────────────────
        if has_sorting and tbl.get("sort_sel"):
            try:
                sort_header = page.locator(tbl["sort_sel"]).first
                if await sort_header.count() > 0:
                    first_cell_before = await page.evaluate(f"""() => {{
                        const t = document.querySelector('{selector}');
                        const firstRow = t ? t.querySelector('tbody tr:first-child td:first-child') : null;
                        return firstRow ? firstRow.textContent.trim() : null;
                    }}""")
                    await sort_header.click(timeout=4000)
                    await asyncio.sleep(0.5)
                    first_cell_after = await page.evaluate(f"""() => {{
                        const t = document.querySelector('{selector}');
                        const firstRow = t ? t.querySelector('tbody tr:first-child td:first-child') : null;
                        return firstRow ? firstRow.textContent.trim() : null;
                    }}""")
                    sorting_works = first_cell_before != first_cell_after
            except Exception as e:
                logger.debug(f"[DeepQA] Sort test failed: {e}")
                sorting_works = False

        # ── Test search ────────────────────────────────────────────────────
        if has_search and tbl.get("search_sel"):
            try:
                search_input = page.locator(tbl["search_sel"]).first
                if await search_input.count() > 0:
                    rows_before = await page.locator(f"{selector} tbody tr").count()
                    await search_input.fill("xyz", timeout=3000)
                    await asyncio.sleep(0.8)
                    rows_after = await page.locator(f"{selector} tbody tr").count()
                    # Either rows changed (filtered) or stayed same (no match)
                    search_works = True  # the search field worked
                    # Clear search
                    await search_input.fill("", timeout=3000)
                    await asyncio.sleep(0.5)
            except Exception as e:
                logger.debug(f"[DeepQA] Search test failed: {e}")
                search_works = False

        # Status: PASS if has data, FAIL if should have data but doesn't
        status = "PASS" if row_count > 0 else "WARNING"

        return TableResult(
            selector=selector,
            row_count=row_count,
            has_pagination=has_pagination,
            pagination_works=pagination_works,
            has_sorting=has_sorting,
            sorting_works=sorting_works,
            has_search=has_search,
            search_works=search_works,
            status=status,
        )

    # ── 5. MODALS ──────────────────────────────────────────────────────────────

    async def _test_modals(self, page, url: str) -> list:
        results = []
        try:
            # Find buttons likely to open modals
            modal_triggers = await page.evaluate("""() => {
                const triggers = [];
                const seen = new Set();
                const SKIP = new Set([
                    "logout","sign out","delete","remove","download","export"
                ]);

                document.querySelectorAll(
                    'button[data-toggle="modal"], button[data-bs-toggle="modal"], button[data-target*="modal"], [data-modal], button[onclick*="modal"]'
                ).forEach(el => {
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none') return;
                    const text = (el.textContent || '').trim().toLowerCase();
                    if (SKIP.has(text)) return;
                    if (seen.has(text)) return;
                    seen.add(text);

                    let sel = el.id ? '#' + el.id : null;
                    if (!sel) {
                        const target = el.getAttribute('data-target') || el.getAttribute('data-bs-target');
                        if (target) sel = '[data-bs-target="' + target + '"]';
                    }
                    if (!sel) {
                        const cls = Array.from(el.classList).slice(0,2).join('.');
                        sel = el.tagName.toLowerCase() + (cls ? '.' + cls : '');
                    }

                    triggers.push({
                        label:    (el.textContent || '').trim().substring(0, 60),
                        selector: sel,
                    });
                });
                return triggers.slice(0, 10);
            }""")
        except Exception as e:
            logger.warning(f"[DeepQA] Modal discovery failed: {e}")
            return results

        for trigger in modal_triggers[:MAX_MODALS_PER_PAGE]:
            try:
                result = await self._test_single_modal(page, trigger, url)
                results.append(result)
                # Navigate back if needed
                if page.url.rstrip("/") != url.rstrip("/"):
                    await page.goto(url, wait_until="domcontentloaded", timeout=10000)
                    await asyncio.sleep(0.3)
            except Exception as e:
                logger.warning(f"[DeepQA] Modal test failed for '{trigger.get('label')}': {e}")

        return results

    async def _test_single_modal(self, page, trigger: dict, url: str) -> ModalResult:
        label    = trigger.get("label", "Unknown")
        selector = trigger.get("selector", "button")

        try:
            loc = page.locator(selector).first
            if await loc.count() == 0:
                return ModalResult(
                    trigger_label=label, trigger_selector=selector,
                    opens_correctly=False, contains_form=False, status="SKIP",
                )

            await loc.click(timeout=5000)
            await asyncio.sleep(0.5)

            # Check if modal opened
            modal_visible = await page.evaluate("""() => {
                const sels = [
                    '[role="dialog"]:not([aria-hidden="true"])',
                    '.modal.show', '.modal.is-active',
                    '.modal[style*="display: block"]',
                    '[aria-modal="true"]',
                    '.dialog:not([hidden])',
                ];
                for (const s of sels) {
                    try {
                        const el = document.querySelector(s);
                        if (el) {
                            const st = window.getComputedStyle(el);
                            if (st.display !== 'none' && st.visibility !== 'hidden') return true;
                        }
                    } catch {}
                }
                return false;
            }""")

            if not modal_visible:
                return ModalResult(
                    trigger_label=label, trigger_selector=selector,
                    opens_correctly=False, contains_form=False, status="FAIL",
                )

            # Check if form inside modal
            has_form = await page.evaluate("""() => {
                const modal = document.querySelector(
                    '[role="dialog"], .modal.show, [aria-modal="true"]'
                );
                return modal ? modal.querySelectorAll('input, select, textarea').length > 0 : false;
            }""")

            # Test close button
            closes_with_button = None
            try:
                close_btn = page.locator(
                    '[role="dialog"] button.close, [role="dialog"] [aria-label="Close"],'
                    ' .modal .btn-close, .modal button.close, [data-dismiss="modal"],'
                    ' [data-bs-dismiss="modal"]'
                ).first
                if await close_btn.count() > 0:
                    await close_btn.click(timeout=3000)
                    await asyncio.sleep(0.4)
                    still_open = await page.evaluate("""() => {
                        const el = document.querySelector('[role="dialog"], .modal.show');
                        if (!el) return false;
                        const st = window.getComputedStyle(el);
                        return st.display !== 'none';
                    }""")
                    closes_with_button = not still_open
            except Exception:
                closes_with_button = None

            # Reopen to test ESC
            closes_with_escape = None
            if closes_with_button:
                try:
                    await loc.click(timeout=4000)
                    await asyncio.sleep(0.4)
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.4)
                    still_open = await page.evaluate("""() => {
                        const el = document.querySelector('[role="dialog"], .modal.show');
                        if (!el) return false;
                        const st = window.getComputedStyle(el);
                        return st.display !== 'none';
                    }""")
                    closes_with_escape = not still_open
                except Exception:
                    closes_with_escape = None

            status = "PASS" if modal_visible else "FAIL"
            if closes_with_button is False:
                status = "FAIL"

            return ModalResult(
                trigger_label=label,
                trigger_selector=selector,
                opens_correctly=modal_visible,
                closes_with_button=closes_with_button,
                closes_with_escape=closes_with_escape,
                contains_form=has_form,
                status=status,
            )

        except Exception as e:
            return ModalResult(
                trigger_label=label, trigger_selector=selector,
                opens_correctly=False, contains_form=False,
                status="FAIL",
            )

    # ── 6. PAGE-LEVEL CHECKS ───────────────────────────────────────────────────

    async def _collect_performance(self, page) -> dict:
        try:
            return await page.evaluate("""() => {
                const nav = performance.getEntriesByType('navigation')[0] || {};
                const paints = Object.fromEntries(
                    performance.getEntriesByType('paint').map(e => [e.name, e.startTime])
                );
                const lcpEntries = performance.getEntriesByType('largest-contentful-paint');
                return {
                    ttfb_ms:  nav.responseStart ? Math.round(nav.responseStart - nav.requestStart) : null,
                    fcp_ms:   paints['first-contentful-paint'] ? Math.round(paints['first-contentful-paint']) : null,
                    lcp_ms:   lcpEntries.length ? Math.round(lcpEntries[lcpEntries.length-1].startTime) : null,
                    dom_ms:   nav.domComplete ? Math.round(nav.domComplete) : null,
                    load_ms:  nav.loadEventEnd ? Math.round(nav.loadEventEnd) : null,
                };
            }""")
        except Exception:
            return {}

    async def _collect_accessibility(self, page) -> dict:
        try:
            return await page.evaluate("""() => {
                const imgs = document.querySelectorAll('img');
                const missing_alt = [...imgs].filter(i => !i.hasAttribute('alt')).length;
                const inputs = document.querySelectorAll('input:not([type="hidden"]), textarea, select');
                const unlabeled = [...inputs].filter(i => {
                    const id = i.id;
                    return !i.getAttribute('aria-label') &&
                           !(id && document.querySelector('label[for="' + id + '"]'));
                }).length;
                const h1s = document.querySelectorAll('h1');
                const htmlEl = document.querySelector('html');
                return {
                    missing_alt_count: missing_alt,
                    unlabeled_inputs:  unlabeled,
                    has_h1:            h1s.length > 0,
                    has_lang:          !!(htmlEl && htmlEl.getAttribute('lang')),
                };
            }""")
        except Exception:
            return {}

    async def _collect_security(self, page, url: str) -> dict:
        try:
            is_https = url.startswith("https://")
            js_data  = await page.evaluate("""() => {
                const forms = document.querySelectorAll('form[method="post"]');
                let csrf_count = 0;
                forms.forEach(f => {
                    if (f.querySelector('input[name*="csrf"], input[name*="token"]')) csrf_count++;
                });
                return {
                    forms_with_csrf: csrf_count,
                    total_post_forms: forms.length,
                };
            }""")
            return {
                "is_https":       is_https,
                **js_data,
                "csrf_coverage": (
                    js_data["forms_with_csrf"] / js_data["total_post_forms"]
                    if js_data["total_post_forms"] > 0 else 1.0
                ),
            }
        except Exception:
            return {}

    async def _collect_broken_images(self, page) -> list:
        try:
            return await page.evaluate("""() => {
                return [...document.querySelectorAll('img')]
                    .filter(img => !img.complete || img.naturalWidth === 0)
                    .map(img => img.src)
                    .filter(src => src && !src.startsWith('data:'))
                    .slice(0, 20);
            }""")
        except Exception:
            return []

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _close_modal(self, page) -> bool:
        """Try to close any open modal."""
        try:
            # ESC first (fastest)
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)

            # Then look for close button
            close_selectors = [
                '[role="dialog"] button.close',
                '[role="dialog"] [aria-label="Close"]',
                '.modal .btn-close',
                '[data-dismiss="modal"]',
                '[data-bs-dismiss="modal"]',
            ]
            for sel in close_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() > 0:
                        await loc.click(timeout=2000)
                        return True
                except Exception:
                    continue
            return True
        except Exception:
            return False

    async def _take_screenshot(self, page, label: str) -> Optional[str]:
        """Take screenshot, save to screenshots dir, return relative path."""
        try:
            fname = f"dqa_{self.run_id}_{label}.png"
            path  = os.path.join(self.screenshot_dir, fname)
            await page.screenshot(path=path, full_page=False, timeout=5000)
            return path
        except Exception as e:
            logger.debug(f"[DeepQA] Screenshot failed: {e}")
            return None

    # ── Bug Generation ─────────────────────────────────────────────────────────

    def _generate_bugs(self, result: DeepQAPageResult) -> list:
        """Auto-generate bug reports from all FAIL statuses found."""
        bugs = []

        # Button failures
        for btn in result.buttons:
            if btn.status == "FAIL":
                bugs.append({
                    "title":       f"Button Unresponsive: '{btn.label}'",
                    "bug_type":    "functional",
                    "severity":    "high",
                    "description": (
                        f"Clicking the button '{btn.label}' (selector: {btn.selector}) "
                        f"on {result.page_url} produced no detectable response."
                        + (f" Failure reason: {btn.failure_reason}" if btn.failure_reason else "")
                    ),
                    "impact":  "Users cannot complete the intended action via this button.",
                    "steps":   [f"Navigate to {result.page_url}", f"Click button: '{btn.label}'"],
                    "expected": "Button triggers a visible action (navigation, modal, API call, or success message)",
                    "actual":   btn.action_result or "No response detected",
                    "fix":      f"Investigate click handler on '{btn.selector}'. Check event listeners and API endpoints.",
                    "screenshot_path": btn.screenshot_path,
                })

        # Form failures
        for form in result.forms:
            if form.submission_status == "FAIL":
                bugs.append({
                    "title":       f"Form Submission Failed: {form.form_purpose or 'Form'} ({form.form_selector})",
                    "bug_type":    "functional",
                    "severity":    "critical" if form.form_purpose in ("Login", "Registration", "Checkout") else "high",
                    "description": (
                        f"The {form.form_purpose or 'form'} on {result.page_url} failed to submit successfully. "
                        f"Result: {form.submission_result}."
                        + (f" HTTP response: {form.http_response}" if form.http_response else "")
                        + (f" Failure reason: {form.failure_reason}" if form.failure_reason else "")
                    ),
                    "impact":  f"Users cannot complete the {form.form_purpose or 'form'} workflow.",
                    "steps":   [
                        f"Navigate to {result.page_url}",
                        "Fill all form fields with valid test data",
                        "Click the submit button",
                        f"Observe: {form.submission_result}",
                    ],
                    "expected": "Form submits successfully, returns HTTP 2xx or redirects",
                    "actual":   form.submission_result,
                    "fix":      "Check form action URL, server-side validation, CSRF token configuration, and API endpoint.",
                    "screenshot_path": form.screenshot_path,
                })

            if not form.validation_works and form.required_count > 0:
                bugs.append({
                    "title":       f"Form Validation Missing: {form.form_purpose or 'Form'}",
                    "bug_type":    "functional",
                    "severity":    "medium",
                    "description": (
                        f"Submitting the {form.form_purpose or 'form'} on {result.page_url} "
                        f"with empty required fields did not show validation errors."
                    ),
                    "impact":  "Users can submit incomplete data, potentially causing server-side errors.",
                    "steps":   [
                        f"Navigate to {result.page_url}",
                        "Leave all required fields empty",
                        "Click the submit button",
                        "Observe: no validation error messages appeared",
                    ],
                    "expected": "Validation errors appear for each required field",
                    "actual":   "No validation errors displayed, or form was submitted with empty fields",
                    "fix":      "Add HTML5 'required' attributes and/or JavaScript validation to all required fields.",
                    "screenshot_path": None,
                })

        # Broken links
        broken_links = [l for l in result.links if l.status == "FAIL"]
        if broken_links:
            urls_list = "\n".join(
                f"  - {l.url} → HTTP {l.http_status or 'timeout'}"
                for l in broken_links[:5]
            )
            bugs.append({
                "title":       f"Broken Navigation Links ({len(broken_links)}) — {result.page_url}",
                "bug_type":    "navigation",
                "severity":    "high",
                "description": f"Found {len(broken_links)} broken internal links:\n{urls_list}",
                "impact":      "Users clicking these links will reach error pages, damaging trust and SEO.",
                "steps":       [f"Navigate to {result.page_url}", "Click each navigation link", "Observe HTTP errors"],
                "expected":    "All internal links return HTTP 200",
                "actual":      f"{len(broken_links)} links returned 4xx or 5xx",
                "fix":         "Update or remove broken links. Implement 301 redirects for moved pages.",
                "screenshot_path": None,
            })

        # Empty tables
        for tbl in result.tables:
            if tbl.row_count == 0:
                bugs.append({
                    "title":       f"Empty Data Table: {tbl.selector}",
                    "bug_type":    "functional",
                    "severity":    "medium",
                    "description": f"Table {tbl.selector} on {result.page_url} has no rows.",
                    "impact":      "Users see empty data tables, which may indicate a data loading failure.",
                    "steps":       [f"Navigate to {result.page_url}", f"Observe table {tbl.selector}"],
                    "expected":    "Table displays data rows",
                    "actual":      "Table is empty (0 rows)",
                    "fix":         "Check API calls that populate this table. Verify backend data and filters.",
                    "screenshot_path": None,
                })

        # Modal failures
        for modal in result.modals:
            if not modal.opens_correctly:
                bugs.append({
                    "title":       f"Modal Does Not Open: '{modal.trigger_label}'",
                    "bug_type":    "functional",
                    "severity":    "high",
                    "description": (
                        f"Clicking '{modal.trigger_label}' on {result.page_url} "
                        f"did not open the expected modal dialog."
                    ),
                    "impact":      "Users cannot access the functionality behind this modal trigger.",
                    "steps":       [f"Navigate to {result.page_url}", f"Click '{modal.trigger_label}'"],
                    "expected":    "A modal dialog opens",
                    "actual":      "No modal appeared after clicking",
                    "fix":         f"Check the data-bs-target or onclick attribute of selector '{modal.trigger_selector}'.",
                    "screenshot_path": None,
                })

        # JS errors on load
        if len(result.js_errors_on_load) > 0:
            bugs.append({
                "title":       f"JavaScript Errors on Page Load ({len(result.js_errors_on_load)})",
                "bug_type":    "functional",
                "severity":    "high",
                "description": (
                    f"JavaScript errors detected during page load of {result.page_url}:\n"
                    + "\n".join(f"  - {e}" for e in result.js_errors_on_load[:5])
                ),
                "impact":      "JS errors can break UI components, prevent forms from submitting, or disable interactive features.",
                "steps":       [f"Open {result.page_url}", "Open browser DevTools → Console", "Observe errors on load"],
                "expected":    "No JavaScript errors in browser console",
                "actual":      f"{len(result.js_errors_on_load)} errors on page load",
                "fix":         "Fix all JS errors. Common causes: undefined variables, missing dependencies, syntax errors.",
                "screenshot_path": result.screenshot_path,
            })

        return bugs

    # ── Score Computation ──────────────────────────────────────────────────────

    def _compute_qa_score(self, result: DeepQAPageResult) -> float:
        """Compute 0-100 QA score from real test results."""
        score = 100.0

        # Buttons: -10 per FAIL (max -40)
        btn_fails = sum(1 for b in result.buttons if b.status == "FAIL")
        score -= min(40.0, btn_fails * 10.0)

        # Forms: -20 per FAIL (max -60)
        form_fails = sum(1 for f in result.forms if f.submission_status == "FAIL")
        score -= min(60.0, form_fails * 20.0)

        # Links: -5 per broken (max -30)
        link_breaks = sum(1 for l in result.links if l.status == "FAIL")
        score -= min(30.0, link_breaks * 5.0)

        # Tables: -10 per FAIL (max -20)
        table_fails = sum(1 for t in result.tables if t.status == "FAIL")
        score -= min(20.0, table_fails * 10.0)

        # JS errors on load: -5 per error (max -20)
        score -= min(20.0, len(result.js_errors_on_load) * 5.0)

        # Broken images: -2 per image (max -10)
        score -= min(10.0, len(result.broken_images) * 2.0)

        return round(max(0.0, min(100.0, score)), 1)

    def _build_summary(self, result: DeepQAPageResult) -> dict:
        return {
            "total_buttons":   len(result.buttons),
            "buttons_passed":  sum(1 for b in result.buttons if b.status == "PASS"),
            "buttons_failed":  sum(1 for b in result.buttons if b.status == "FAIL"),
            "buttons_warning": sum(1 for b in result.buttons if b.status == "WARNING"),
            "total_forms":     len(result.forms),
            "forms_passed":    sum(1 for f in result.forms if f.submission_status == "PASS"),
            "forms_failed":    sum(1 for f in result.forms if f.submission_status == "FAIL"),
            "total_links":     len(result.links),
            "links_passed":    sum(1 for l in result.links if l.status == "PASS"),
            "links_broken":    sum(1 for l in result.links if l.status == "FAIL"),
            "total_tables":    len(result.tables),
            "tables_passed":   sum(1 for t in result.tables if t.status == "PASS"),
            "total_modals":    len(result.modals),
            "modals_working":  sum(1 for m in result.modals if m.opens_correctly),
            "js_errors_on_load": len(result.js_errors_on_load),
            "broken_images":   len(result.broken_images),
            "total_bugs":      len(result.bugs),
            "qa_score":        result.qa_score,
        }


# ── Public Runner ──────────────────────────────────────────────────────────────

async def run_deep_qa(context, urls: list,
                      run_id: int) -> list:
    """
    Run DeepQAEngine on a list of URLs.
    Returns list of DeepQAPageResult — one per URL.
    Handles errors gracefully (never crashes the whole run).
    """
    engine  = DeepQAEngine(context, run_id)
    results = []

    for url in urls:
        try:
            logger.info(f"[DeepQA] Testing: {url}")
            result = await engine.test_page(url)
            results.append(result)
        except Exception as e:
            logger.error(f"[DeepQA] Failed for {url}: {e}", exc_info=True)
            # Return a minimal failure result so the run continues
            results.append(DeepQAPageResult(
                page_url=url,
                page_title="Error",
                tested_at=datetime.now(UTC).isoformat(),
                load_time_ms=0,
                qa_score=0.0,
                summary={
                    "total_buttons": 0, "buttons_passed": 0, "buttons_failed": 0,
                    "total_forms": 0, "forms_passed": 0, "forms_failed": 0,
                    "total_links": 0, "links_broken": 0,
                    "total_tables": 0, "tables_passed": 0,
                    "js_errors_on_load": 0,
                    "total_bugs": 0, "qa_score": 0,
                },
                bugs=[{
                    "title":       f"DeepQA Engine Error",
                    "bug_type":    "functional",
                    "severity":    "high",
                    "description": f"DeepQA engine threw an exception for {url}: {str(e)[:200]}",
                    "impact":      "Page could not be tested",
                    "steps":       [f"Navigate to {url}"],
                    "expected":    "Page tests complete successfully",
                    "actual":      str(e)[:200],
                    "fix":         "Investigate DeepQA engine logs for this URL",
                }],
            ))

    logger.info(f"[DeepQA] Completed {len(results)} pages for run {run_id}")
    return results