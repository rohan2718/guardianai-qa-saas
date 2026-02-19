"""
AI Analyzer — GuardianAI
Executive summary generation via Cohere.
API key loaded from environment, never hardcoded.
Falls back to deterministic basic summary if Cohere is unavailable.
"""

import json
import os
import logging

logger = logging.getLogger(__name__)

COHERE_API_KEY = os.environ.get("COHERE_API_KEY", "")

try:
    import cohere
    co = cohere.Client(COHERE_API_KEY) if COHERE_API_KEY else None
except ImportError:
    co = None


def basic_summary(pages: list) -> str:
    """Deterministic fallback summary — no AI required."""
    total = len(pages)
    if total == 0:
        return "No pages were crawled."

    failed = sum(1 for p in pages if p.get("status") != 200)
    slow = sum(1 for p in pages if (p.get("load_time") or 0) > 3)
    a11y = sum(p.get("accessibility_issues") or 0 for p in pages)
    broken = sum(len(p.get("broken_links") or []) for p in pages)
    js_errors = sum(len(p.get("js_errors") or []) for p in pages)

    health_scores = [p.get("health_score") for p in pages if p.get("health_score") is not None]
    avg_health = round(sum(health_scores) / len(health_scores), 1) if health_scores else None
    health_str = f"{avg_health}/100" if avg_health is not None else "N/A"

    perf_scores = [p.get("performance_score") for p in pages if p.get("performance_score") is not None]
    avg_perf = round(sum(perf_scores) / len(perf_scores), 1) if perf_scores else None

    a11y_scores = [p.get("accessibility_score") for p in pages if p.get("accessibility_score") is not None]
    avg_a11y = round(sum(a11y_scores) / len(a11y_scores), 1) if a11y_scores else None

    sec_scores = [p.get("security_score") for p in pages if p.get("security_score") is not None]
    avg_sec = round(sum(sec_scores) / len(sec_scores), 1) if sec_scores else None

    return f"""## Overall Health
{_health_label(avg_health)}

## Key Findings
- Total pages scanned: {total}
- Failed pages (non-200): {failed}
- Slow pages (>3s load): {slow}
- Accessibility issues: {a11y}
- Broken links detected: {broken}
- JS console errors: {js_errors}

## Scores
- Site Health Score: {health_str}
- Performance: {avg_perf if avg_perf is not None else 'N/A'}/100
- Accessibility: {avg_a11y if avg_a11y is not None else 'N/A'}/100
- Security: {avg_sec if avg_sec is not None else 'N/A'}/100

## Top 3 Actions
1. {"Fix " + str(failed) + " failed page(s) returning non-200 status" if failed > 0 else "All pages returned HTTP 200 — maintain current infrastructure"}
2. {"Resolve " + str(a11y) + " accessibility issue(s) to improve WCAG compliance" if a11y > 0 else "Accessibility baseline is clean — continue monitoring"}
3. {"Investigate " + str(broken) + " broken link(s) found across the site" if broken > 0 else "No broken links detected — link integrity is healthy"}
""".strip()


def _health_label(score):
    if score is None:
        return "Unknown — insufficient data"
    if score >= 90:
        return "Excellent"
    if score >= 75:
        return "Good"
    if score >= 50:
        return "Needs Attention"
    return "Critical"


def analyze_site(pages: list) -> str:
    """
    Generates AI-powered executive summary.
    Uses Cohere if available, falls back to basic_summary.
    """
    if not pages:
        return "No pages crawled."

    if not co:
        logger.info("Cohere not available — using basic summary")
        return basic_summary(pages)

    # Build concise page summaries for the prompt (cap at 15 pages)
    trimmed = pages[:15]
    light_pages = [
        {
            "url": p.get("url"),
            "status": p.get("status"),
            "load_time": p.get("load_time"),
            "health_score": p.get("health_score"),
            "performance_score": p.get("performance_score"),
            "accessibility_score": p.get("accessibility_score"),
            "security_score": p.get("security_score"),
            "accessibility_issues": p.get("accessibility_issues", 0),
            "broken_links": len(p.get("broken_links") or []),
            "js_errors": len(p.get("js_errors") or []),
            "is_https": p.get("is_https"),
            "risk_category": p.get("risk_category"),
        }
        for p in trimmed
    ]

    pages_json = json.dumps(light_pages, indent=2)

    prompt = f"""
You are a senior QA consultant reviewing a website quality scan report.

Generate a SHORT executive summary (max 200 words).

STRICT FORMAT — use exactly this structure:

## Overall Health
<One line rating: Excellent / Good / Needs Attention / Critical> — <one sentence reason>

## Key Findings
- Failed pages: <count>
- Slow pages (>3s): <count>
- Accessibility issues: <total count>
- Security concerns: <count of pages with security_score < 75>
- Broken links: <total count>

## Top 3 Priority Actions
1. <specific actionable recommendation>
2. <specific actionable recommendation>
3. <specific actionable recommendation>

Rules:
- Never repeat raw data
- Be decision-ready, not descriptive
- No percentages unless meaningful
- No page names, only patterns
- Max 200 words total

Scan data:
{pages_json}
"""

    try:
        response = co.chat(
            model="command-a-03-2025",
            message=prompt,
            temperature=0.2,
            max_tokens=600
        )
        return response.text.strip()
    except Exception as e:
        logger.error(f"Cohere AI failed: {e}")
        return basic_summary(pages)