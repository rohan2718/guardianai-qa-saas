"""
Performance Engine — GuardianAI
Captures real browser performance metrics from Playwright pages.
All values sourced from browser Performance API. Nothing is estimated.
"""

import logging

logger = logging.getLogger(__name__)


async def capture_performance_metrics(page) -> dict:
    try:
        raw = await page.evaluate("""() => {
            const nav = performance.getEntriesByType('navigation')[0] || null;
            const paintEntries = performance.getEntriesByType('paint') || [];

            let fcp = null;
            paintEntries.forEach(entry => {
                if (entry.name === 'first-contentful-paint') fcp = entry.startTime;
            });

            const lcpEntries = performance.getEntriesByType('largest-contentful-paint');
            const lcp = (lcpEntries && lcpEntries.length > 0)
                ? lcpEntries[lcpEntries.length - 1].startTime
                : null;

            const mem = (typeof performance !== 'undefined' && performance.memory)
                ? {
                    used_js_heap: performance.memory.usedJSHeapSize,
                    total_js_heap: performance.memory.totalJSHeapSize,
                    heap_limit: performance.memory.jsHeapSizeLimit
                }
                : null;

            const ttfb              = nav ? (nav.responseStart - nav.requestStart) : null;
            const dom_interactive   = nav ? nav.domInteractive : null;
            const dom_complete      = nav ? nav.domComplete : null;
            const load_event_end    = nav ? nav.loadEventEnd : null;
            const dom_content_loaded = nav
                ? (nav.domContentLoadedEventEnd - nav.domContentLoadedEventStart)
                : null;

            const resources      = performance.getEntriesByType('resource') || [];
            const js_count       = resources.filter(r => r.initiatorType === 'script').length;
            const css_count      = resources.filter(r => r.initiatorType === 'link' || r.name.endsWith('.css')).length;
            const img_count      = resources.filter(r => r.initiatorType === 'img').length;
            const total_transfer = resources.reduce((s, r) => s + (r.transferSize || 0), 0);

            const blocking_scripts = document.querySelectorAll(
                'head script:not([async]):not([defer]):not([type="module"])'
            ).length;
            const blocking_css = document.querySelectorAll('head link[rel="stylesheet"]').length;

            return {
                ttfb_ms:               ttfb !== null ? Math.max(0, ttfb) : null,
                dom_interactive_ms:    dom_interactive !== null ? Math.max(0, dom_interactive) : null,
                dom_complete_ms:       dom_complete !== null ? Math.max(0, dom_complete) : null,
                load_event_end_ms:     load_event_end !== null ? Math.max(0, load_event_end) : null,
                dom_content_loaded_ms: dom_content_loaded !== null ? Math.max(0, dom_content_loaded) : null,
                fcp_ms:  fcp !== null ? Math.max(0, fcp) : null,
                lcp_ms:  lcp !== null ? Math.max(0, lcp) : null,
                memory:  mem,
                resources: {
                    total:               resources.length,
                    js_count:            js_count,
                    css_count:           css_count,
                    img_count:           img_count,
                    total_transfer_bytes: total_transfer
                },
                render_blocking: {
                    scripts:    blocking_scripts,
                    stylesheets: blocking_css
                }
            };
        }""")
        return raw
    except Exception as e:
        logger.error(f"Performance capture failed: {e}")
        return {
            "ttfb_ms": None, "dom_interactive_ms": None, "dom_complete_ms": None,
            "load_event_end_ms": None, "dom_content_loaded_ms": None,
            "fcp_ms": None, "lcp_ms": None,
            "memory": None, "resources": None, "render_blocking": None,
            "_error": str(e)
        }


def compute_performance_score(metrics: dict) -> dict:
    if not metrics or metrics.get("_error"):
        return {"score": None, "grade": None, "breakdown": {}, "slow_indicators": []}

    slow_indicators = []
    score = 100.0
    breakdown = {}

    # TTFB: <200ms good, <500ms ok, >500ms bad
    ttfb = metrics.get("ttfb_ms")
    if ttfb is not None:
        if ttfb > 500:
            deduct = min(25, (ttfb - 500) / 100)
            score -= deduct
            breakdown["ttfb"] = {"value_ms": ttfb, "rating": "slow", "deduction": round(deduct, 1)}
            slow_indicators.append(f"High TTFB: {ttfb:.0f}ms")
        elif ttfb > 200:
            deduct = (ttfb - 200) / 150
            score -= deduct
            breakdown["ttfb"] = {"value_ms": ttfb, "rating": "moderate", "deduction": round(deduct, 1)}
        else:
            breakdown["ttfb"] = {"value_ms": ttfb, "rating": "good", "deduction": 0}

    # FCP: <1800ms good, <3000ms ok, >3000ms bad
    fcp = metrics.get("fcp_ms")
    if fcp is not None:
        if fcp > 3000:
            deduct = min(30, (fcp - 3000) / 500)
            score -= deduct
            breakdown["fcp"] = {"value_ms": fcp, "rating": "slow", "deduction": round(deduct, 1)}
            slow_indicators.append(f"Slow FCP: {fcp/1000:.1f}s")
        elif fcp > 1800:
            deduct = (fcp - 1800) / 400
            score -= deduct
            breakdown["fcp"] = {"value_ms": fcp, "rating": "moderate", "deduction": round(deduct, 1)}
        else:
            breakdown["fcp"] = {"value_ms": fcp, "rating": "good", "deduction": 0}

    # LCP: <2500ms good, <4000ms ok, >4000ms bad
    lcp = metrics.get("lcp_ms")
    if lcp is not None:
        if lcp > 4000:
            deduct = min(30, (lcp - 4000) / 500)
            score -= deduct
            breakdown["lcp"] = {"value_ms": lcp, "rating": "slow", "deduction": round(deduct, 1)}
            slow_indicators.append(f"Slow LCP: {lcp/1000:.1f}s")
        elif lcp > 2500:
            deduct = (lcp - 2500) / 500
            score -= deduct
            breakdown["lcp"] = {"value_ms": lcp, "rating": "moderate", "deduction": round(deduct, 1)}
        else:
            breakdown["lcp"] = {"value_ms": lcp, "rating": "good", "deduction": 0}

    load = metrics.get("load_event_end_ms")
    if load is not None:
        if load > 5000:
            deduct = min(20, (load - 5000) / 1000)
            score -= deduct
            breakdown["load_time"] = {"value_ms": load, "rating": "slow", "deduction": round(deduct, 1)}
            slow_indicators.append(f"High total load: {load/1000:.1f}s")
        else:
            breakdown["load_time"] = {"value_ms": load, "rating": "acceptable", "deduction": 0}

    rb = metrics.get("render_blocking") or {}
    blocking_total = (rb.get("scripts", 0) or 0) + (rb.get("stylesheets", 0) or 0)
    if blocking_total > 5:
        deduct = min(10, blocking_total - 5)
        score -= deduct
        breakdown["render_blocking"] = {"count": blocking_total, "deduction": round(deduct, 1)}
        slow_indicators.append(f"{blocking_total} render-blocking resources")
    else:
        breakdown["render_blocking"] = {"count": blocking_total, "deduction": 0}

    score = max(0.0, min(100.0, score))

    return {
        "score": round(score, 1),
        "grade": _grade(score),
        "breakdown": breakdown,
        "slow_indicators": slow_indicators
    }


def _grade(score: float) -> str:
    if score >= 90:   return "Excellent"
    elif score >= 75: return "Good"
    elif score >= 50: return "Needs Attention"
    else:             return "Critical"
