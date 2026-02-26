"""
ai_analyzer.py — GuardianAI Production Refactor
Redesigned analyze_site():
  - Suggestions ranked by severity frequency across ALL pages
  - Top 3 root causes surfaced specifically
  - Generic suggestions suppressed unless issue frequency > threshold
  - AI output is site-specific (uses actual issue counts + patterns)
  - Adaptive: if site is clean, summary reflects that instead of padding
"""

import json
import os
import logging
from collections import Counter

logger = logging.getLogger(__name__)

COHERE_API_KEY = os.environ.get("COHERE_API_KEY", "")

try:
    import cohere
    co = cohere.Client(COHERE_API_KEY) if COHERE_API_KEY else None
except ImportError:
    co = None

# ── Thresholds ────────────────────────────────────────────────────────────────
# Generic suggestions are only included if issue count exceeds these per-site
SUGGESTION_THRESHOLDS = {
    "broken_nav_links":       1,   # any = actionable
    "js_errors":              3,   # <3 total = don't flag
    "accessibility_issues":   5,   # <5 = minor
    "slow_pages":             1,   # any = actionable
    "security_low_score":     1,   # any page <75 = actionable
    "failed_pages":           1,   # any non-200 = critical
    "missing_https":          1,   # any = critical
}


# ── Issue Aggregator ──────────────────────────────────────────────────────────

def aggregate_issues(pages: list) -> dict:
    """
    Scans all pages and returns frequency-ranked issue map.
    Returns structured dict for both AI prompt and deterministic fallback.
    """
    total  = len(pages)
    failed = sum(1 for p in pages if (p.get("status") or 200) != 200)
    slow   = sum(1 for p in pages if (p.get("load_time") or 0) > 3)

    # Broken nav links only (not assets/3p)
    broken_nav = sum(
        len(p.get("broken_navigation_links") or p.get("broken_links") or [])
        for p in pages
    )

    a11y_total = sum(p.get("accessibility_issues") or 0 for p in pages)
    js_total   = sum(len(p.get("js_errors") or []) for p in pages)
    no_https   = sum(1 for p in pages if p.get("is_https") is False)

    sec_low    = sum(1 for p in pages if (p.get("security_score") or 100) < 75)
    sec_crit   = sum(1 for p in pages if (p.get("security_score") or 100) < 50)

    # Root cause tag frequency
    root_tags  = Counter()
    for p in pages:
        tag = p.get("root_cause_tag") or ""
        for t in tag.split("+"):
            if t:
                root_tags[t] += 1

    # Health score stats
    health_scores = [p.get("health_score") for p in pages if p.get("health_score") is not None]
    avg_health = round(sum(health_scores) / len(health_scores), 1) if health_scores else None
    min_health = round(min(health_scores), 1) if health_scores else None

    # Worst pages
    worst_pages = sorted(
        [p for p in pages if p.get("health_score") is not None],
        key=lambda p: p["health_score"]
    )[:3]
    worst_urls = [p.get("url", "") for p in worst_pages]

    # Performance
    lcp_bad  = sum(1 for p in pages if (p.get("lcp_ms") or 0) > 4000)
    ttfb_bad = sum(1 for p in pages if (p.get("ttfb_ms") or 0) > 800)

    # Build ranked issue list (sorted by severity impact)
    ranked_issues = []

    if failed > 0:
        ranked_issues.append({
            "severity": "critical",
            "category": "http_errors",
            "count": failed,
            "pages_affected": failed,
            "label": f"{failed} page(s) returning non-200 HTTP status",
            "action": f"Audit server routing — {failed} URL(s) returning errors. Fix or redirect immediately."
        })

    if no_https > 0:
        ranked_issues.append({
            "severity": "critical",
            "category": "https",
            "count": no_https,
            "pages_affected": no_https,
            "label": f"{no_https} page(s) served over HTTP (no TLS)",
            "action": "Force HTTPS redirect and update all internal links to https://."
        })

    if sec_crit > 0:
        ranked_issues.append({
            "severity": "critical",
            "category": "security_headers",
            "count": sec_crit,
            "pages_affected": sec_crit,
            "label": f"{sec_crit} page(s) with critical security score",
            "action": "Add missing security headers: Content-Security-Policy, X-Frame-Options, Strict-Transport-Security."
        })

    if broken_nav > SUGGESTION_THRESHOLDS["broken_nav_links"] - 1:
        ranked_issues.append({
            "severity": "high",
            "category": "broken_nav_links",
            "count": broken_nav,
            "pages_affected": None,
            "label": f"{broken_nav} broken internal navigation link(s)",
            "action": "Run link audit — fix or redirect broken anchor hrefs (assets/3rd-party excluded)."
        })

    if slow > SUGGESTION_THRESHOLDS["slow_pages"] - 1:
        ranked_issues.append({
            "severity": "high",
            "category": "performance",
            "count": slow,
            "pages_affected": slow,
            "label": f"{slow} slow page(s) with load time > 3s",
            "action": f"{'Optimize LCP — ' + str(lcp_bad) + ' pages exceed 4s LCP. ' if lcp_bad else ''}{'Reduce TTFB on ' + str(ttfb_bad) + ' pages. ' if ttfb_bad else ''}Compress images and defer render-blocking JS."
        })

    if a11y_total >= SUGGESTION_THRESHOLDS["accessibility_issues"]:
        top_a11y_tag = root_tags.most_common(1)
        tag_hint = f" — top cause: {top_a11y_tag[0][0]}" if top_a11y_tag else ""
        ranked_issues.append({
            "severity": "medium",
            "category": "accessibility",
            "count": a11y_total,
            "pages_affected": None,
            "label": f"{a11y_total} accessibility issue(s) across site{tag_hint}",
            "action": "Fix missing alt attributes, unlabeled inputs, and ARIA roles. Target WCAG 2.1 AA compliance."
        })

    if js_total >= SUGGESTION_THRESHOLDS["js_errors"]:
        ranked_issues.append({
            "severity": "medium",
            "category": "js_errors",
            "count": js_total,
            "pages_affected": None,
            "label": f"{js_total} JavaScript console error(s) detected",
            "action": "Review browser console logs. JS errors indicate broken components or missing dependencies."
        })

    if sec_low > 0 and sec_crit == 0:
        ranked_issues.append({
            "severity": "medium",
            "category": "security_warnings",
            "count": sec_low,
            "pages_affected": sec_low,
            "label": f"{sec_low} page(s) with security score below 75",
            "action": "Add Permissions-Policy, Referrer-Policy and review CSP implementation."
        })

    # Sort by severity rank
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    ranked_issues.sort(key=lambda x: severity_order.get(x["severity"], 99))

    return {
        "total":           total,
        "failed":          failed,
        "slow":            slow,
        "broken_nav":      broken_nav,
        "a11y_total":      a11y_total,
        "js_total":        js_total,
        "no_https":        no_https,
        "sec_low":         sec_low,
        "sec_crit":        sec_crit,
        "lcp_bad":         lcp_bad,
        "ttfb_bad":        ttfb_bad,
        "avg_health":      avg_health,
        "min_health":      min_health,
        "worst_urls":      worst_urls,
        "root_tags":       dict(root_tags.most_common(5)),
        "ranked_issues":   ranked_issues,
    }


# ── Health Label ──────────────────────────────────────────────────────────────

def _health_label(score):
    if score is None:    return "Unknown — insufficient data"
    if score >= 90:      return "Excellent"
    if score >= 75:      return "Good"
    if score >= 50:      return "Needs Attention"
    return "Critical"


# ── Deterministic Fallback ────────────────────────────────────────────────────

def basic_summary(pages: list) -> str:
    if not pages:
        return "No pages were crawled."

    agg = aggregate_issues(pages)
    top3 = agg["ranked_issues"][:3]

    perf_scores = [p.get("performance_score") for p in pages if p.get("performance_score") is not None]
    a11y_scores = [p.get("accessibility_score") for p in pages if p.get("accessibility_score") is not None]
    sec_scores  = [p.get("security_score") for p in pages if p.get("security_score") is not None]
    avg_perf = round(sum(perf_scores)/len(perf_scores), 1) if perf_scores else None
    avg_a11y = round(sum(a11y_scores)/len(a11y_scores), 1) if a11y_scores else None
    avg_sec  = round(sum(sec_scores)/len(sec_scores), 1) if sec_scores else None

    actions = []
    for i, issue in enumerate(top3, 1):
        actions.append(f"{i}. {issue['action']}")
    if not actions:
        actions = ["1. Site appears healthy — continue monitoring and run scheduled scans."]

    root_tag_str = ""
    if agg["root_tags"]:
        top_tags = list(agg["root_tags"].keys())[:3]
        root_tag_str = f"\n- Top root causes: {', '.join(top_tags)}"

    return f"""## Overall Health
{_health_label(agg['avg_health'])} — avg health score {agg['avg_health']}/100 across {agg['total']} pages

## Key Findings
- Total pages scanned: {agg['total']}
- Failed pages (non-200): {agg['failed']}
- Slow pages (>3s): {agg['slow']}
- Accessibility issues: {agg['a11y_total']}
- Broken navigation links: {agg['broken_nav']}
- JS console errors: {agg['js_total']}
- Pages missing HTTPS: {agg['no_https']}{root_tag_str}

## Scores
- Site Health: {agg['avg_health'] if agg['avg_health'] is not None else 'N/A'}/100 (worst page: {agg['min_health']}/100)
- Performance: {avg_perf if avg_perf is not None else 'N/A'}/100
- Accessibility: {avg_a11y if avg_a11y is not None else 'N/A'}/100
- Security: {avg_sec if avg_sec is not None else 'N/A'}/100

## Top Priority Actions
{chr(10).join(actions)}""".strip()


# ── AI-Powered Summary ────────────────────────────────────────────────────────

def analyze_site(pages: list) -> str:
    if not pages:
        return "No pages crawled."

    if not co:
        logger.info("Cohere not available — using basic summary")
        return basic_summary(pages)

    agg = aggregate_issues(pages)

    # Only include issues that exceed thresholds — no padding
    active_issues = [
        f"- [{i['severity'].upper()}] {i['label']}"
        for i in agg["ranked_issues"]
    ]
    issues_block = "\n".join(active_issues) if active_issues else "- No significant issues detected"

    top_actions = [
        f"{idx+1}. {i['action']}"
        for idx, i in enumerate(agg["ranked_issues"][:3])
    ]
    if not top_actions:
        top_actions = ["1. Site appears healthy — schedule regular scans and monitor for regressions."]
    actions_block = "\n".join(top_actions)

    root_tags_str = ", ".join(list(agg["root_tags"].keys())[:3]) or "none identified"

    prompt = f"""You are a senior QA consultant. Write a site-specific executive summary based ONLY on the data below.

SITE DATA:
- Pages scanned: {agg['total']}
- Avg health score: {agg['avg_health']}/100 (worst: {agg['min_health']}/100)
- Failed pages: {agg['failed']}
- Slow pages (>3s load): {agg['slow']}
- LCP > 4s: {agg['lcp_bad']}, TTFB > 800ms: {agg['ttfb_bad']}
- Broken internal navigation links: {agg['broken_nav']} (assets/3rd-party excluded)
- JS errors: {agg['js_total']}
- Accessibility issues: {agg['a11y_total']}
- Pages without HTTPS: {agg['no_https']}
- Pages with security score <75: {agg['sec_low']}
- Top root cause tags: {root_tags_str}

RANKED ISSUES (by severity):
{issues_block}

WRITE:
## Overall Health
<Excellent/Good/Needs Attention/Critical> — <one sentence specific to THIS site's data>

## Key Findings
<bullet list of the most impactful metrics — only include items with non-zero counts>

## Top 3 Priority Actions
{actions_block}

RULES:
- Be specific to the numbers above — never generic
- Do NOT mention CSP or aria-label unless the data shows them as issues
- If no issues exist in a category, skip that category entirely
- Max 180 words
- No preamble, no sign-off
"""

    try:
        response = co.chat(
            model="command-a-03-2025",
            message=prompt,
            temperature=0.15,
            max_tokens=500,
        )
        return response.text.strip()
    except Exception as e:
        logger.error(f"Cohere AI failed: {e}")
        return basic_summary(pages)