"""
Scoring Engine — GuardianAI
Computes weighted composite health scores for pages and entire site.
Weights: Performance 30%, Accessibility 25%, Security 20%, Functional 15%, UI/Form 10%
"""

from typing import Optional

WEIGHTS = {
    "performance": 0.30,
    "accessibility": 0.25,
    "security": 0.20,
    "functional": 0.15,
    "ui_form": 0.10,
}

RISK_THRESHOLDS = {
    "Excellent": 90,
    "Good": 75,
    "Needs Attention": 50,
    "Critical": 0,
}


def compute_functional_score(page_data: dict) -> dict:
    """
    Computes functional health from broken links, JS errors, network failures,
    redirect chains, and API errors found during crawl.
    """
    status = page_data.get("status", 200)
    errors = page_data.get("errors") or []
    broken_links = page_data.get("broken_links") or []
    redirect_chain = page_data.get("redirect_chain_length", 0) or 0
    js_errors = page_data.get("js_errors") or []

    score = 100.0
    breakdown = {}

    # HTTP status
    if status == 404:
        score -= 80
        breakdown["http_status"] = {"value": status, "deduction": 80}
    elif status == 500:
        score -= 100
        breakdown["http_status"] = {"value": status, "deduction": 100}
    elif status and status >= 400:
        score -= 50
        breakdown["http_status"] = {"value": status, "deduction": 50}
    elif status == 200:
        breakdown["http_status"] = {"value": status, "deduction": 0}

    # Broken links on page
    broken = len(broken_links)
    if broken > 0:
        b_deduct = min(30.0, broken * 5.0)
        score -= b_deduct
        breakdown["broken_links"] = {"count": broken, "deduction": round(b_deduct, 1)}
    else:
        breakdown["broken_links"] = {"count": 0, "deduction": 0}

    # JS console errors
    js_err_count = len(js_errors)
    if js_err_count > 0:
        j_deduct = min(20.0, js_err_count * 3.0)
        score -= j_deduct
        breakdown["js_errors"] = {"count": js_err_count, "deduction": round(j_deduct, 1)}
    else:
        breakdown["js_errors"] = {"count": 0, "deduction": 0}

    # Network failures (failed requests)
    net_fail = len(errors)
    if net_fail > 0:
        n_deduct = min(15.0, net_fail * 2.0)
        score -= n_deduct
        breakdown["network_failures"] = {"count": net_fail, "deduction": round(n_deduct, 1)}

    # Redirect chains
    if redirect_chain > 2:
        r_deduct = min(10.0, (redirect_chain - 2) * 3.0)
        score -= r_deduct
        breakdown["redirect_chain"] = {"length": redirect_chain, "deduction": round(r_deduct, 1)}

    score = max(0.0, min(100.0, score))
    return {
        "score": round(score, 1),
        "breakdown": breakdown
    }


def compute_ui_form_score(page_data: dict) -> dict:
    """
    Scores UI element integrity and form health from crawl data.
    """
    forms = page_data.get("forms") or []
    ui_elements = page_data.get("ui_elements") or []
    score = 100.0
    breakdown = {}

    total_form_issues = 0
    total_form_health = 0
    form_count = len(forms)

    for form in forms:
        fh = form.get("form_health_score")
        fi = form.get("form_issue_count", 0) or 0
        total_form_issues += fi
        if fh is not None:
            total_form_health += fh

    if form_count > 0:
        avg_form_health = total_form_health / form_count
        # Each 10 pts below 100 in form health deducts ~5 pts from overall
        form_deduct = max(0.0, (100 - avg_form_health) * 0.5)
        score -= form_deduct
        breakdown["form_health"] = {
            "avg_form_score": round(avg_form_health, 1),
            "total_issues": total_form_issues,
            "deduction": round(form_deduct, 1)
        }
    else:
        breakdown["form_health"] = {"avg_form_score": None, "total_issues": 0, "deduction": 0}

    # Check for invisible but non-hidden interactive elements (UI integrity)
    invisible_interactive = sum(
        1 for el in ui_elements
        if el.get("visible") is False and el.get("enabled") is True
    )
    if invisible_interactive > 3:
        ui_deduct = min(10.0, invisible_interactive * 1.5)
        score -= ui_deduct
        breakdown["invisible_interactive"] = {
            "count": invisible_interactive, "deduction": round(ui_deduct, 1)
        }

    score = max(0.0, min(100.0, score))
    return {
        "score": round(score, 1),
        "breakdown": breakdown
    }


def compute_page_health_score(
    performance_score: Optional[float],
    accessibility_score: Optional[float],
    security_score: Optional[float],
    functional_score: Optional[float],
    ui_form_score: Optional[float]
) -> dict:
    """
    Weighted composite health score for a single page.
    If a component score is None, its weight is redistributed to available scores.
    """
    scores = {
        "performance": performance_score,
        "accessibility": accessibility_score,
        "security": security_score,
        "functional": functional_score,
        "ui_form": ui_form_score,
    }

    available = {k: v for k, v in scores.items() if v is not None}

    if not available:
        return {
            "health_score": None,
            "risk_category": None,
            "component_scores": scores,
            "weights_used": {}
        }

    # Redistribute weight from missing scores
    total_available_weight = sum(WEIGHTS[k] for k in available)
    adjusted_weights = {
        k: WEIGHTS[k] / total_available_weight
        for k in available
    }

    weighted_sum = sum(available[k] * adjusted_weights[k] for k in available)
    health_score = round(min(100.0, max(0.0, weighted_sum)), 1)
    risk_category = _risk_category(health_score)

    return {
        "health_score": health_score,
        "risk_category": risk_category,
        "component_scores": {
            "performance": performance_score,
            "accessibility": accessibility_score,
            "security": security_score,
            "functional": functional_score,
            "ui_form": ui_form_score,
        },
        "weights_used": {k: round(adjusted_weights[k], 3) for k in adjusted_weights}
    }


def compute_site_health_score(page_scores: list) -> dict:
    """
    Aggregates all page health scores into a single site-level score.
    Uses average of valid page scores only.
    Also computes per-component averages and site-wide stats.
    """
    valid_scores = [p["health_score"] for p in page_scores if p.get("health_score") is not None]

    if not valid_scores:
        return {
            "site_health_score": None,
            "risk_category": None,
            "page_count": len(page_scores),
            "scored_pages": 0,
            "component_averages": {}
        }

    site_score = round(sum(valid_scores) / len(valid_scores), 1)
    risk_category = _risk_category(site_score)

    # Component averages
    component_keys = ["performance", "accessibility", "security", "functional", "ui_form"]
    component_averages = {}
    for key in component_keys:
        vals = [
            p["component_scores"][key]
            for p in page_scores
            if p.get("component_scores", {}).get(key) is not None
        ]
        component_averages[key] = round(sum(vals) / len(vals), 1) if vals else None

    # Distribution
    distribution = {
        "Excellent": sum(1 for s in valid_scores if s >= 90),
        "Good": sum(1 for s in valid_scores if 75 <= s < 90),
        "Needs Attention": sum(1 for s in valid_scores if 50 <= s < 75),
        "Critical": sum(1 for s in valid_scores if s < 50),
    }

    return {
        "site_health_score": site_score,
        "risk_category": risk_category,
        "page_count": len(page_scores),
        "scored_pages": len(valid_scores),
        "component_averages": component_averages,
        "score_distribution": distribution,
        "min_page_score": min(valid_scores),
        "max_page_score": max(valid_scores),
    }


def _risk_category(score: float) -> str:
    if score >= 90:
        return "Excellent"
    elif score >= 75:
        return "Good"
    elif score >= 50:
        return "Needs Attention"
    else:
        return "Critical"