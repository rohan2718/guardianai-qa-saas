"""
confidence_engine.py — GuardianAI Production Refactor
Redesigned compute_run_confidence() to produce meaningful variance across:
  - Small static sites    → high confidence if clean, but limited depth
  - Large dynamic sites   → penalised for crawl coverage gaps
  - Error-heavy sites     → low confidence from error variance
  - Clean enterprise site → high confidence with full coverage

Formula:
  confidence = (
      crawl_coverage_ratio * 0.30 +    # pages crawled vs discovered
      completeness_ratio   * 0.25 +    # checks with real data vs null
      error_stability      * 0.20 +    # 1 - error_variance_factor
      link_integrity       * 0.15 +    # 1 - broken_link_density
      js_cleanliness       * 0.05 +    # 1 - js_error_density
      redirect_stability   * 0.05      # 1 - redirect_instability
  ) * 100

Each factor is normalised 0-1. Score range: 0-100.
"""

import hashlib
import json
import logging
import math

logger = logging.getLogger(__name__)

# Possible score fields — used for per-page completeness
POSSIBLE_CHECKS = [
    "performance_score",
    "accessibility_score",
    "security_score",
    "functional_score",
    "ui_form_score",
    "fcp_ms",
    "lcp_ms",
    "ttfb_ms",
    "load_time",
    "accessibility_issues",
    "is_https",
]

CHECK_WEIGHTS = {
    "performance_score":    0.15,
    "accessibility_score":  0.15,
    "security_score":       0.15,
    "functional_score":     0.10,
    "ui_form_score":        0.05,
    "fcp_ms":               0.10,
    "lcp_ms":               0.10,
    "ttfb_ms":              0.08,
    "load_time":            0.07,
    "accessibility_issues": 0.03,
    "is_https":             0.02,
}


# ── Per-page confidence ────────────────────────────────────────────────────────

def compute_confidence_score(page_data: dict, active_filters: list = None) -> dict:
    if not page_data:
        return {"confidence_score": None, "checks_executed": 0, "checks_null": len(POSSIBLE_CHECKS), "completeness_ratio": 0.0}

    expected_checks = POSSIBLE_CHECKS[:]
    if active_filters:
        filter_check_map = {
            "performance":      ["performance_score", "fcp_ms", "lcp_ms", "ttfb_ms", "load_time"],
            "accessibility":    ["accessibility_score", "accessibility_issues"],
            "security":         ["security_score", "is_https"],
            "functional":       ["functional_score"],
            "ui_elements":      ["ui_form_score"],
            "form_validation":  ["ui_form_score"],
        }
        expected_checks = []
        seen = set()
        for f in active_filters:
            for c in filter_check_map.get(f, []):
                if c not in seen:
                    seen.add(c)
                    expected_checks.append(c)
        if not expected_checks:
            expected_checks = POSSIBLE_CHECKS[:]

    executed = 0
    null_count = 0
    weighted_score = 0.0
    weight_total = 0.0

    for check in expected_checks:
        val    = page_data.get(check)
        weight = CHECK_WEIGHTS.get(check, 0.05)
        weight_total += weight
        if val is not None:
            executed += 1
            weighted_score += weight
        else:
            null_count += 1

    if weight_total == 0:
        return {"confidence_score": None, "checks_executed": 0, "checks_null": len(expected_checks), "completeness_ratio": 0.0}

    completeness_ratio = weighted_score / weight_total
    confidence = round(completeness_ratio * 100, 1)

    return {
        "confidence_score":  confidence,
        "checks_executed":   executed,
        "checks_null":       null_count,
        "completeness_ratio": round(completeness_ratio, 4),
    }


# ── Run-level confidence ────────────────────────────────────────────────────────

def compute_run_confidence(pages: list, active_filters: list = None) -> float:
    """
    Redesigned run-level confidence score.
    Returns 0-100 float. Varies meaningfully across site types.

    Factors:
      1. crawl_coverage_ratio  (0-1): pages_scanned / pages_discovered
      2. completeness_ratio    (0-1): avg weighted check completeness
      3. error_stability       (0-1): inverse of error variance across pages
      4. link_integrity        (0-1): 1 - broken_nav_link_density
      5. js_cleanliness        (0-1): 1 - js_error_density
      6. redirect_stability    (0-1): 1 - redirect_instability
    """
    if not pages:
        return 0.0

    n = len(pages)

    # ── Factor 1: Crawl coverage ──────────────────────────────────────────────
    # Estimate discovered pages: visited + any connected_pages not yet in visited
    discovered_urls = set()
    for p in pages:
        discovered_urls.add(p.get("url", ""))
        for cp in (p.get("connected_pages") or []):
            discovered_urls.add(cp)
    discovered_urls.discard("")
    total_discovered = max(n, len(discovered_urls))
    crawl_coverage_ratio = min(1.0, n / total_discovered)

    # ── Factor 2: Completeness ratio ─────────────────────────────────────────
    completeness_scores = []
    for p in pages:
        result = compute_confidence_score(p, active_filters)
        cr = result.get("completeness_ratio")
        if cr is not None:
            completeness_scores.append(cr)
    completeness_ratio = (sum(completeness_scores) / len(completeness_scores)) if completeness_scores else 0.0

    # ── Factor 3: Error stability (low variance = high stability) ─────────────
    # Use health_score variance: high variance → unstable → lower confidence
    health_scores = [p.get("health_score") for p in pages if p.get("health_score") is not None]
    if len(health_scores) >= 2:
        mean_h = sum(health_scores) / len(health_scores)
        variance = sum((h - mean_h) ** 2 for h in health_scores) / len(health_scores)
        # Normalise: stddev of 30 = max expected variance → maps to 0
        stddev = math.sqrt(variance)
        error_stability = max(0.0, 1.0 - (stddev / 35.0))
    elif len(health_scores) == 1:
        # Single page — stability can't be measured, assume moderate
        error_stability = 0.70
    else:
        error_stability = 0.50  # no scores = very uncertain

    # ── Factor 4: Broken navigation link density ─────────────────────────────
    # broken_navigation_links: only internal anchor links returning 4xx/5xx
    total_nav_links  = 0
    total_broken_nav = 0
    for p in pages:
        nav_broken = p.get("broken_navigation_links") or p.get("broken_links") or []
        total_broken_nav += len(nav_broken)
        # Estimate total nav links from UI summary
        ui_s = p.get("ui_summary") or {}
        total_nav_links += ui_s.get("links", 0)

    if total_nav_links > 0:
        broken_density = total_broken_nav / total_nav_links
        link_integrity = max(0.0, 1.0 - min(1.0, broken_density * 5))  # 20% broken = 0
    else:
        link_integrity = 1.0 if total_broken_nav == 0 else 0.5

    # ── Factor 5: JS cleanliness ──────────────────────────────────────────────
    total_js_errors = sum(len(p.get("js_errors") or []) for p in pages)
    # 1 error/page = moderate concern; >3/page = severe
    js_error_per_page = total_js_errors / n
    js_cleanliness = max(0.0, 1.0 - min(1.0, js_error_per_page / 4.0))

    # ── Factor 6: Redirect stability ─────────────────────────────────────────
    # Pages with redirect_chain_length > 2 = instability signal
    unstable_redirects = sum(1 for p in pages if (p.get("redirect_chain_length") or 0) > 2)
    redirect_instability = unstable_redirects / n
    redirect_stability   = max(0.0, 1.0 - redirect_instability * 2)  # >50% with long chains = 0

    # ── Missing component scores ratio ────────────────────────────────────────
    # Pages where functional/perf/a11y score is None when filter was active
    missing_scores = 0
    for p in pages:
        if active_filters:
            if "performance"   in active_filters and p.get("performance_score")   is None: missing_scores += 1
            if "accessibility" in active_filters and p.get("accessibility_score") is None: missing_scores += 1
            if "security"      in active_filters and p.get("security_score")      is None: missing_scores += 1
        else:
            if p.get("performance_score")   is None: missing_scores += 1
            if p.get("accessibility_score") is None: missing_scores += 1
            if p.get("security_score")      is None: missing_scores += 1
    expected_scores = n * (3 if not active_filters else len([f for f in (active_filters or []) if f in ("performance", "accessibility", "security")]))
    missing_ratio = (missing_scores / expected_scores) if expected_scores > 0 else 0.0
    completeness_ratio = completeness_ratio * (1.0 - missing_ratio * 0.5)  # penalise missing

    # ── Weighted composite ────────────────────────────────────────────────────
    confidence = (
        crawl_coverage_ratio * 0.30 +
        completeness_ratio   * 0.25 +
        error_stability      * 0.20 +
        link_integrity       * 0.15 +
        js_cleanliness       * 0.05 +
        redirect_stability   * 0.05
    ) * 100

    confidence = round(max(0.0, min(100.0, confidence)), 1)

    logger.debug(
        f"[confidence] coverage={crawl_coverage_ratio:.2f} completeness={completeness_ratio:.2f} "
        f"stability={error_stability:.2f} link_integrity={link_integrity:.2f} "
        f"js_clean={js_cleanliness:.2f} redirect={redirect_stability:.2f} → {confidence}"
    )

    return confidence


def compute_confidence_explanation(pages: list, active_filters: list = None) -> dict:
    """
    Returns human-readable breakdown of what drove the confidence score.
    Used for tooltip in dashboard.
    """
    if not pages:
        return {"score": 0.0, "factors": {}}

    n = len(pages)
    discovered_urls = set()
    for p in pages:
        discovered_urls.add(p.get("url", ""))
        for cp in (p.get("connected_pages") or []):
            discovered_urls.add(cp)
    discovered_urls.discard("")
    total_discovered = max(n, len(discovered_urls))

    total_broken_nav = sum(len(p.get("broken_navigation_links") or p.get("broken_links") or []) for p in pages)
    total_js_errors  = sum(len(p.get("js_errors") or []) for p in pages)
    unstable_redir   = sum(1 for p in pages if (p.get("redirect_chain_length") or 0) > 2)

    health_scores = [p.get("health_score") for p in pages if p.get("health_score") is not None]
    health_stddev  = 0.0
    if len(health_scores) >= 2:
        mean_h = sum(health_scores) / len(health_scores)
        health_stddev = math.sqrt(sum((h - mean_h)**2 for h in health_scores) / len(health_scores))

    return {
        "score": compute_run_confidence(pages, active_filters),
        "factors": {
            "pages_scanned":        n,
            "pages_discovered":     total_discovered,
            "crawl_coverage_pct":   round(min(100, n / total_discovered * 100), 1),
            "broken_nav_links":     total_broken_nav,
            "js_errors_total":      total_js_errors,
            "unstable_redirects":   unstable_redir,
            "health_score_stddev":  round(health_stddev, 1),
        }
    }


# ── Failure Pattern & Root Cause ───────────────────────────────────────────────

def compute_failure_pattern_id(page_data: dict) -> str | None:
    try:
        flags = []
        if (page_data.get("status") or 200) >= 400:
            flags.append("http_error")
        if len(page_data.get("broken_navigation_links") or page_data.get("broken_links") or []) > 0:
            flags.append("broken_nav")
        if len(page_data.get("js_errors") or []) > 2:
            flags.append("js_errors")
        a11y = page_data.get("accessibility_data") or {}
        if (a11y.get("total_issues") or 0) > 5:
            flags.append("a11y_issues")
        sec = page_data.get("security_data") or {}
        if (sec.get("severity_counts") or {}).get("critical", 0) > 0:
            flags.append("sec_critical")
        if not flags:
            return None
        key = "|".join(sorted(flags))
        return hashlib.md5(key.encode()).hexdigest()[:8]
    except Exception as e:
        logger.warning(f"compute_failure_pattern_id failed: {e}")
        return None


def compute_root_cause_tag(page_data: dict) -> str | None:
    try:
        tags = []
        a11y = page_data.get("accessibility_data") or {}
        checks = a11y.get("checks") or {}
        if checks.get("missing_alt", 0) > 0:    tags.append("missing_alt")
        if checks.get("unlabeled_inputs", 0) > 0: tags.append("unlabeled_inputs")
        if not a11y.get("has_lang_attr"):         tags.append("no_lang_attr")
        sec = page_data.get("security_data") or {}
        findings = sec.get("findings") or []
        sec_cats = list({f.get("category") for f in findings if f.get("severity") in ("critical", "high")})
        tags.extend(sec_cats[:2])
        nav_broken = page_data.get("broken_navigation_links") or page_data.get("broken_links") or []
        if nav_broken:
            tags.append("broken_nav_links")
        perf = page_data.get("performance_metrics") or {}
        if (perf.get("lcp_ms") or 0) > 4000:
            tags.append("slow_lcp")
        if not tags:
            return None
        return "+".join(tags[:5])
    except Exception as e:
        logger.warning(f"compute_root_cause_tag failed: {e}")
        return None


def compute_self_healing_suggestion(page_data: dict) -> str | None:
    """
    Generates concrete, page-specific self-healing suggestions with real counts.
    Each page produces a unique message reflecting its actual issues.
    All deterministic — no AI call required.
    Returns None if no actionable suggestions.
    """
    try:
        suggestions = []

        a11y   = page_data.get("accessibility_data") or {}
        checks = a11y.get("checks") or {}
        issues = a11y.get("issues") or []

        missing_alt = checks.get("missing_alt", 0)
        if missing_alt > 0:
            suggestions.append(
                f"A11y: {missing_alt} image(s) missing alt text — "
                f"page.locator('img:not([alt])').count() = {missing_alt}"
            )

        unlabeled = checks.get("unlabeled_inputs", 0)
        if unlabeled > 0:
            suggestions.append(
                f"A11y: {unlabeled} unlabeled input(s) — "
                f"inject aria-label on page.locator('input:not([aria-label]):not([id])')"
            )

        contrast_issues = sum(1 for i in issues if i.get("category") == "color_contrast")
        if contrast_issues > 0:
            suggestions.append(
                f"A11y: {contrast_issues} color contrast failure(s) — "
                f"adjust foreground/background to meet WCAG AA (4.5:1 ratio)"
            )

        sec = page_data.get("security_data") or {}
        findings = sec.get("findings") or []
        sec_categories_seen = set()
        for f in findings:
            cat = f.get("category")
            sev = f.get("severity")
            if cat and sev in ("critical", "high") and cat not in sec_categories_seen:
                sec_categories_seen.add(cat)
                if cat == "csp":
                    suggestions.append(
                        "Sec: Missing Content-Security-Policy header — "
                        "add via server middleware or meta tag"
                    )
                elif cat == "csrf":
                    suggestions.append(
                        "Sec: CSRF token absent on POST form — "
                        "page.locator('form[method=POST]') → inject hidden token input"
                    )
                elif cat == "mixed_content":
                    suggestions.append(
                        "Sec: Mixed content detected — upgrade all http:// asset URLs to https://"
                    )
                elif cat == "clickjacking":
                    suggestions.append(
                        "Sec: Missing X-Frame-Options — add 'DENY' or 'SAMEORIGIN' header"
                    )
                if len(sec_categories_seen) >= 2:
                    break

        perf = page_data.get("performance_metrics") or {}
        lcp  = perf.get("lcp_ms")
        if lcp and lcp > 4000:
            suggestions.append(
                f"Perf: LCP = {lcp:.0f}ms (target <2500ms) — "
                f"defer non-critical JS, preload hero image"
            )
        elif lcp and lcp > 2500:
            suggestions.append(
                f"Perf: LCP = {lcp:.0f}ms (above target) — "
                f"compress images, enable server caching"
            )

        fcp = perf.get("fcp_ms")
        if fcp and fcp > 3000:
            suggestions.append(
                f"Perf: FCP = {fcp:.0f}ms — eliminate render-blocking resources"
            )

        broken = page_data.get("broken_links") or []
        if broken:
            sample = ", ".join(b.get("url", "?") for b in broken[:2])
            suggestions.append(
                f"Func: {len(broken)} broken navigation link(s) — e.g. {sample}"
            )

        js_errors = page_data.get("js_errors") or []
        if js_errors:
            top_err = str(js_errors[0])[:80]
            suggestions.append(
                f"Func: {len(js_errors)} JS error(s) — first: {top_err}"
            )

        if not suggestions:
            return None

        return " | ".join(suggestions[:3])

    except Exception as e:
        logger.warning(f"self_healing_suggestion failed: {e}")
        return None


def enrich_page_with_ai_fields(page_data: dict, active_filters: list = None) -> dict:
    confidence_result = compute_confidence_score(page_data, active_filters)
    page_data["confidence_score"]   = confidence_result.get("confidence_score")
    page_data["checks_executed"]    = confidence_result.get("checks_executed")
    page_data["checks_null"]        = confidence_result.get("checks_null")
    page_data["failure_pattern_id"] = compute_failure_pattern_id(page_data)
    page_data["root_cause_tag"]     = compute_root_cause_tag(page_data)
    page_data["self_healing_suggestion"] = compute_self_healing_suggestion(page_data)
    page_data["similar_issue_ref"]  = None
    page_data["ai_confidence"]      = None
    return page_data