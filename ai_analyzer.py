"""
ai_analyzer.py — GuardianAI
AI executive summary powered by Groq (llama-3.3-70b-versatile).
Falls back to basic_summary() if Groq is unavailable or times out.
"""

import os
import logging
from collections import Counter

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL   = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

# Initialise Groq client
try:
    from groq import Groq
    groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
except ImportError:
    groq_client = None
    logger.warning("groq package not installed — run: pip install groq")


# ── Thresholds ────────────────────────────────────────────────────────────────

SUGGESTION_THRESHOLDS = {
    "broken_nav_links":     1,
    "js_errors":            3,
    "accessibility_issues": 5,
    "slow_pages":           1,
    "security_low_score":   1,
    "failed_pages":         1,
    "missing_https":        1,
}


# ── Issue Aggregator ──────────────────────────────────────────────────────────

def aggregate_issues(pages: list) -> dict:
    total  = len(pages)
    failed = sum(1 for p in pages if (p.get("status") or 200) != 200)
    slow   = sum(1 for p in pages if (p.get("load_time") or 0) > 3)

    broken_nav = sum(
        len(p.get("broken_navigation_links") or p.get("broken_links") or [])
        for p in pages
    )

    a11y_total = sum(p.get("accessibility_issues") or 0 for p in pages)
    js_total   = sum(len(p.get("js_errors") or []) for p in pages)
    no_https   = sum(1 for p in pages if p.get("is_https") is False)
    sec_low    = sum(1 for p in pages if (p.get("security_score") or 100) < 75)
    sec_crit   = sum(1 for p in pages if (p.get("security_score") or 100) < 50)

    root_tags = Counter()
    for p in pages:
        tag = p.get("root_cause_tag") or ""
        for t in tag.split("+"):
            if t:
                root_tags[t] += 1

    health_scores = [p.get("health_score") for p in pages if p.get("health_score") is not None]
    avg_health = round(sum(health_scores) / len(health_scores), 1) if health_scores else None
    min_health = round(min(health_scores), 1) if health_scores else None

    worst_pages = sorted(
        [p for p in pages if p.get("health_score") is not None],
        key=lambda p: p["health_score"]
    )[:3]
    worst_urls = [p.get("url", "") for p in worst_pages]

    lcp_bad  = sum(1 for p in pages if (p.get("lcp_ms") or 0) > 4000)
    ttfb_bad = sum(1 for p in pages if (p.get("ttfb_ms") or 0) > 800)

    ranked_issues = []

    if failed > 0:
        ranked_issues.append({
            "severity": "critical",
            "label": f"{failed} page(s) returning non-200 HTTP status",
            "action": f"Audit server routing — {failed} URL(s) returning errors. Fix or redirect immediately."
        })

    if no_https > 0:
        ranked_issues.append({
            "severity": "critical",
            "label": f"{no_https} page(s) served over HTTP (no TLS)",
            "action": "Force HTTPS redirect and update all internal links to https://."
        })

    if sec_crit > 0:
        ranked_issues.append({
            "severity": "critical",
            "label": f"{sec_crit} page(s) with critical security score (<50)",
            "action": "Add missing security headers: Content-Security-Policy, X-Frame-Options, Strict-Transport-Security."
        })

    if broken_nav >= SUGGESTION_THRESHOLDS["broken_nav_links"]:
        ranked_issues.append({
            "severity": "high",
            "label": f"{broken_nav} broken internal navigation link(s)",
            "action": "Run link audit — fix or redirect broken anchor hrefs (assets/3rd-party excluded)."
        })

    if slow >= SUGGESTION_THRESHOLDS["slow_pages"] or lcp_bad > 0:
        ranked_issues.append({
            "severity": "high",
            "label": f"{slow} slow page(s) (>3s load), {lcp_bad} with LCP >4s",
            "action": "Compress images and defer render-blocking JS."
        })

    if a11y_total >= SUGGESTION_THRESHOLDS["accessibility_issues"]:
        ranked_issues.append({
            "severity": "medium",
            "label": f"{a11y_total} accessibility issue(s) across site",
            "action": "Fix missing alt attributes, unlabeled inputs, and ARIA roles. Target WCAG 2.1 AA compliance."
        })

    if js_total >= SUGGESTION_THRESHOLDS["js_errors"]:
        ranked_issues.append({
            "severity": "medium",
            "label": f"{js_total} JS console error(s)",
            "action": "Audit console errors — JS errors indicate broken components or missing dependencies."
        })

    if sec_low > 0 and sec_crit == 0:
        ranked_issues.append({
            "severity": "medium",
            "label": f"{sec_low} page(s) with security score below 75",
            "action": "Add Permissions-Policy, Referrer-Policy and review CSP implementation."
        })

    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    ranked_issues.sort(key=lambda x: severity_order.get(x["severity"], 99))

    return {
        "total":         total,
        "failed":        failed,
        "slow":          slow,
        "broken_nav":    broken_nav,
        "a11y_total":    a11y_total,
        "js_total":      js_total,
        "no_https":      no_https,
        "sec_low":       sec_low,
        "sec_crit":      sec_crit,
        "lcp_bad":       lcp_bad,
        "ttfb_bad":      ttfb_bad,
        "avg_health":    avg_health,
        "min_health":    min_health,
        "worst_urls":    worst_urls,
        "root_tags":     dict(root_tags.most_common(5)),
        "ranked_issues": ranked_issues,
    }


# ── Health Label ──────────────────────────────────────────────────────────────

def _health_label(score):
    if score is None: return "Unknown"
    if score >= 90:   return "Excellent"
    if score >= 75:   return "Good"
    if score >= 50:   return "Needs Attention"
    return "Critical"


# ── Deterministic Fallback ────────────────────────────────────────────────────

def basic_summary(pages: list) -> str:
    if not pages:
        return "No pages were crawled."

    agg  = aggregate_issues(pages)
    top3 = agg["ranked_issues"][:3]

    perf_scores = [p.get("performance_score") for p in pages if p.get("performance_score") is not None]
    a11y_scores = [p.get("accessibility_score") for p in pages if p.get("accessibility_score") is not None]
    sec_scores  = [p.get("security_score")      for p in pages if p.get("security_score")      is not None]
    avg_perf = round(sum(perf_scores) / len(perf_scores), 1) if perf_scores else None
    avg_a11y = round(sum(a11y_scores) / len(a11y_scores), 1) if a11y_scores else None
    avg_sec  = round(sum(sec_scores)  / len(sec_scores),  1) if sec_scores  else None

    actions = [f"{i+1}. {issue['action']}" for i, issue in enumerate(top3)]
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


# ── Groq-Powered Summary ──────────────────────────────────────────────────────

def analyze_site(pages: list) -> str:
    if not pages:
        return "No pages crawled."

    if not groq_client:
        logger.info("Groq not available — using basic summary")
        return basic_summary(pages)

    agg = aggregate_issues(pages)

    issues_block = "\n".join(
        f"- [{i['severity'].upper()}] {i['label']}"
        for i in agg["ranked_issues"]
    ) or "- No significant issues detected"

    top_actions = "\n".join(
        f"{idx+1}. {i['action']}"
        for idx, i in enumerate(agg["ranked_issues"][:3])
    ) or "1. Site appears healthy — schedule regular scans and monitor for regressions."

    root_tags_str = ", ".join(list(agg["root_tags"].keys())[:3]) or "none identified"

    perf_scores = [p.get("performance_score") for p in pages if p.get("performance_score") is not None]
    a11y_scores = [p.get("accessibility_score") for p in pages if p.get("accessibility_score") is not None]
    sec_scores  = [p.get("security_score")      for p in pages if p.get("security_score")      is not None]
    avg_perf = round(sum(perf_scores) / len(perf_scores), 1) if perf_scores else "N/A"
    avg_a11y = round(sum(a11y_scores) / len(a11y_scores), 1) if a11y_scores else "N/A"
    avg_sec  = round(sum(sec_scores)  / len(sec_scores),  1) if sec_scores  else "N/A"

    prompt = f"""You are a senior QA consultant. Write a site-specific executive summary based ONLY on the data below.

SITE DATA:
- Pages scanned: {agg['total']}
- Avg health score: {agg['avg_health']}/100 (worst page: {agg['min_health']}/100)
- Performance score: {avg_perf}/100
- Accessibility score: {avg_a11y}/100
- Security score: {avg_sec}/100
- Failed pages: {agg['failed']}
- Slow pages (>3s load): {agg['slow']}
- LCP > 4s: {agg['lcp_bad']}, TTFB > 800ms: {agg['ttfb_bad']}
- Broken internal navigation links: {agg['broken_nav']}
- JS errors: {agg['js_total']}
- Accessibility issues: {agg['a11y_total']}
- Pages without HTTPS: {agg['no_https']}
- Pages with security score <75: {agg['sec_low']}
- Top root cause tags: {root_tags_str}

RANKED ISSUES (by severity):
{issues_block}

WRITE EXACTLY THIS FORMAT:
## Overall Health
<Excellent/Good/Needs Attention/Critical> — <one sentence specific to THIS site's data>

## Key Findings
<bullet list — only include metrics with non-zero counts>

## Scores
- Site Health: {agg['avg_health']}/100 (worst page: {agg['min_health']}/100)
- Performance: {avg_perf}/100
- Accessibility: {avg_a11y}/100
- Security: {avg_sec}/100

## Top Priority Actions
{top_actions}

RULES:
- Be specific to the numbers — never generic
- Only mention issue types that actually appear in the data
- Max 200 words total
- No preamble, no sign-off, no markdown code blocks
"""

    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.15,
            max_tokens=600,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Groq AI failed: {e} — falling back to basic summary")
        return basic_summary(pages)