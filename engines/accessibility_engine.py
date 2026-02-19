"""
Accessibility Engine — GuardianAI
Performs real DOM-based accessibility checks via Playwright.
No heuristics fabricated — all values sourced from live page inspection.
"""

import logging

logger = logging.getLogger(__name__)


async def capture_accessibility_data(page) -> dict:
    """
    Runs a comprehensive accessibility audit inside the browser context.
    Returns structured findings per category. Missing data = null, not estimated.
    """
    try:
        raw = await page.evaluate("""() => {
            const issues = [];
            const passed = [];

            // ── 1. IMAGES WITHOUT ALT TEXT ──
            const allImages = document.querySelectorAll('img');
            let missing_alt = 0;
            allImages.forEach(img => {
                if (!img.hasAttribute('alt')) {
                    missing_alt++;
                    issues.push({
                        category: 'missing_alt',
                        severity: 'high',
                        element: img.src ? img.src.substring(0, 120) : 'unknown',
                        message: 'Image missing alt attribute'
                    });
                } else {
                    passed.push({ category: 'alt_text', element: img.src ? img.src.substring(0, 80) : '' });
                }
            });

            // ── 2. FORM INPUTS WITHOUT LABELS ──
            const inputs = document.querySelectorAll(
                'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="reset"]), textarea, select'
            );
            let unlabeled_inputs = 0;
            inputs.forEach(input => {
                const id = input.id;
                const hasLabel = id
                    ? document.querySelector(`label[for="${id}"]`) !== null
                    : false;
                const hasAriaLabel = input.hasAttribute('aria-label') ||
                    input.hasAttribute('aria-labelledby');
                const hasTitle = input.hasAttribute('title');

                if (!hasLabel && !hasAriaLabel && !hasTitle) {
                    unlabeled_inputs++;
                    issues.push({
                        category: 'unlabeled_input',
                        severity: 'high',
                        element: input.name || input.type || input.tagName.toLowerCase(),
                        message: 'Form input has no associated label or aria-label'
                    });
                }
            });

            // ── 3. BUTTONS WITHOUT ACCESSIBLE NAMES ──
            const buttons = document.querySelectorAll('button, [role="button"]');
            let unnamed_buttons = 0;
            buttons.forEach(btn => {
                const text = (btn.textContent || '').trim();
                const ariaLabel = btn.getAttribute('aria-label') || '';
                const ariaLabelledBy = btn.getAttribute('aria-labelledby') || '';
                const title = btn.getAttribute('title') || '';

                if (!text && !ariaLabel && !ariaLabelledBy && !title) {
                    unnamed_buttons++;
                    issues.push({
                        category: 'unnamed_button',
                        severity: 'medium',
                        element: btn.className ? btn.className.substring(0, 80) : btn.tagName,
                        message: 'Button has no accessible name'
                    });
                }
            });

            // ── 4. MISSING HEADING HIERARCHY ──
            const h1s = document.querySelectorAll('h1');
            const h2s = document.querySelectorAll('h2');
            let heading_issues = 0;

            if (h1s.length === 0) {
                heading_issues++;
                issues.push({
                    category: 'heading_hierarchy',
                    severity: 'medium',
                    element: 'document',
                    message: 'Page has no H1 heading'
                });
            } else if (h1s.length > 1) {
                heading_issues++;
                issues.push({
                    category: 'heading_hierarchy',
                    severity: 'low',
                    element: 'document',
                    message: `Multiple H1 headings found (${h1s.length})`
                });
            }

            // ── 5. LINKS WITHOUT ACCESSIBLE TEXT ──
            const links = document.querySelectorAll('a[href]');
            let empty_links = 0;
            links.forEach(link => {
                const text = (link.textContent || '').trim();
                const ariaLabel = link.getAttribute('aria-label') || '';
                const title = link.getAttribute('title') || '';
                const hasImg = link.querySelector('img[alt]');

                if (!text && !ariaLabel && !title && !hasImg) {
                    empty_links++;
                    issues.push({
                        category: 'empty_link',
                        severity: 'medium',
                        element: link.href ? link.href.substring(0, 80) : 'unknown',
                        message: 'Link has no accessible text'
                    });
                }
            });

            // ── 6. FOCUSABLE ELEMENTS OUTSIDE TAB ORDER ──
            const interactiveElements = document.querySelectorAll(
                'a, button, input, select, textarea, [tabindex]'
            );
            let negative_tabindex = 0;
            interactiveElements.forEach(el => {
                const tabindex = el.getAttribute('tabindex');
                if (tabindex !== null && parseInt(tabindex) < 0) {
                    negative_tabindex++;
                }
            });
            if (negative_tabindex > 0) {
                issues.push({
                    category: 'keyboard_access',
                    severity: 'medium',
                    element: `${negative_tabindex} elements`,
                    message: `${negative_tabindex} interactive elements removed from tab order`
                });
            }

            // ── 7. MISSING LANG ATTRIBUTE ──
            const htmlEl = document.querySelector('html');
            if (!htmlEl || !htmlEl.getAttribute('lang')) {
                issues.push({
                    category: 'language',
                    severity: 'medium',
                    element: 'html',
                    message: 'Document missing lang attribute'
                });
            }

            // ── 8. SKIP NAVIGATION LINK ──
            const skipLink = document.querySelector(
                'a[href="#main"], a[href="#content"], a[href="#main-content"], .skip-link'
            );
            if (!skipLink) {
                issues.push({
                    category: 'skip_navigation',
                    severity: 'low',
                    element: 'document',
                    message: 'No skip navigation link detected'
                });
            }

            // ── 9. INTERACTIVE ELEMENTS TOO SMALL (touch target) ──
            let small_targets = 0;
            document.querySelectorAll('button, a, input, select').forEach(el => {
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    if (rect.width < 24 || rect.height < 24) {
                        small_targets++;
                    }
                }
            });
            if (small_targets > 0) {
                issues.push({
                    category: 'touch_target',
                    severity: 'low',
                    element: `${small_targets} elements`,
                    message: `${small_targets} interactive elements smaller than 24x24px`
                });
            }

            // ── 10. ARIA LANDMARK REGIONS ──
            const main = document.querySelector('main, [role="main"]');
            const nav_el = document.querySelector('nav, [role="navigation"]');
            if (!main) {
                issues.push({
                    category: 'aria_landmarks',
                    severity: 'low',
                    element: 'document',
                    message: 'No main landmark region found'
                });
            }
            if (!nav_el && links.length > 3) {
                issues.push({
                    category: 'aria_landmarks',
                    severity: 'low',
                    element: 'document',
                    message: 'Navigation links not wrapped in nav landmark'
                });
            }

            // ── SEVERITY COUNTS ──
            const high = issues.filter(i => i.severity === 'high').length;
            const medium = issues.filter(i => i.severity === 'medium').length;
            const low = issues.filter(i => i.severity === 'low').length;

            return {
                total_issues: issues.length,
                severity_counts: { high, medium, low },
                issues: issues.slice(0, 50),  // cap for storage
                checks: {
                    missing_alt,
                    unlabeled_inputs,
                    unnamed_buttons,
                    heading_issues,
                    empty_links,
                    negative_tabindex,
                    small_targets
                },
                has_skip_nav: !!skipLink,
                has_lang_attr: !!(htmlEl && htmlEl.getAttribute('lang')),
                has_main_landmark: !!main
            };
        }""")

        return raw

    except Exception as e:
        logger.error(f"Accessibility capture failed: {e}")
        return {
            "total_issues": None,
            "severity_counts": None,
            "issues": [],
            "checks": None,
            "_error": str(e)
        }


def compute_accessibility_score(a11y_data: dict) -> dict:
    """
    Computes 0–100 accessibility score. Severity-weighted deductions.
    Returns score, risk_level, and breakdown.
    """
    if not a11y_data or a11y_data.get("_error") or a11y_data.get("total_issues") is None:
        return {
            "score": None,
            "risk_level": None,
            "breakdown": {},
            "wcag_compliance_estimate": None
        }

    score = 100.0
    breakdown = {}
    severity = a11y_data.get("severity_counts") or {}

    high = severity.get("high", 0)
    medium = severity.get("medium", 0)
    low = severity.get("low", 0)

    # High severity: -8 pts each (capped at 60 pts total)
    high_deduct = min(60.0, high * 8.0)
    score -= high_deduct
    breakdown["high_severity"] = {"count": high, "deduction": round(high_deduct, 1)}

    # Medium severity: -4 pts each (capped at 30 pts total)
    med_deduct = min(30.0, medium * 4.0)
    score -= med_deduct
    breakdown["medium_severity"] = {"count": medium, "deduction": round(med_deduct, 1)}

    # Low severity: -1.5 pts each (capped at 15 pts total)
    low_deduct = min(15.0, low * 1.5)
    score -= low_deduct
    breakdown["low_severity"] = {"count": low, "deduction": round(low_deduct, 1)}

    score = max(0.0, min(100.0, score))

    if score >= 90:
        risk_level = "Low"
    elif score >= 70:
        risk_level = "Medium"
    elif score >= 50:
        risk_level = "High"
    else:
        risk_level = "Critical"

    # WCAG basic compliance estimate
    checks = a11y_data.get("checks") or {}
    wcag_failures = []
    if checks.get("missing_alt", 0) > 0:
        wcag_failures.append("WCAG 1.1.1 (Non-text Content)")
    if checks.get("unlabeled_inputs", 0) > 0:
        wcag_failures.append("WCAG 1.3.1 (Info and Relationships)")
    if not a11y_data.get("has_lang_attr"):
        wcag_failures.append("WCAG 3.1.1 (Language of Page)")
    if checks.get("unnamed_buttons", 0) > 0:
        wcag_failures.append("WCAG 4.1.2 (Name, Role, Value)")

    return {
        "score": round(score, 1),
        "risk_level": risk_level,
        "breakdown": breakdown,
        "wcag_violations": wcag_failures
    }