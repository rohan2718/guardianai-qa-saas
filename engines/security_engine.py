"""
Security Engine - GuardianAI
Performs real security checks via Playwright response headers + DOM inspection.
No fabricated scores -- all findings from live page data.

IMPORTANT: JS passed to page.evaluate() must NOT use backtick template literals.
They conflict with Python triple-quoted strings. Use string concatenation instead.
"""

import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


async def capture_security_data(page, response, url: str) -> dict:
    findings = []
    headers = {}
    passed_checks = []

    try:
        if response:
            headers = dict(response.headers) if response.headers else {}
    except Exception as e:
        logger.warning(f"Could not read response headers: {e}")

    # 1. HTTPS CHECK
    parsed = urlparse(url)
    is_https = parsed.scheme == "https"
    if not is_https:
        findings.append({
            "category": "https",
            "severity": "critical",
            "detail": "Page served over HTTP, not HTTPS",
            "recommendation": "Enforce HTTPS sitewide with 301 redirect"
        })
    else:
        passed_checks.append("HTTPS enforced")

    # 2. HSTS HEADER
    hsts = headers.get("strict-transport-security", "")
    if is_https and not hsts:
        findings.append({
            "category": "hsts",
            "severity": "high",
            "detail": "Strict-Transport-Security header missing",
            "recommendation": "Add HSTS header: max-age=31536000; includeSubDomains"
        })
    elif hsts:
        passed_checks.append("HSTS header present")

    # 3. CONTENT SECURITY POLICY
    csp = headers.get("content-security-policy", "") or headers.get("x-content-security-policy", "")
    if not csp:
        findings.append({
            "category": "csp",
            "severity": "high",
            "detail": "Content-Security-Policy header not set",
            "recommendation": "Implement a CSP to prevent XSS and data injection"
        })
    else:
        passed_checks.append("CSP header present")
        if "unsafe-inline" in csp:
            findings.append({
                "category": "csp",
                "severity": "medium",
                "detail": "CSP allows unsafe-inline - weakens XSS protection",
                "recommendation": "Remove unsafe-inline; use nonces or hashes instead"
            })
        if "unsafe-eval" in csp:
            findings.append({
                "category": "csp",
                "severity": "medium",
                "detail": "CSP allows unsafe-eval",
                "recommendation": "Remove unsafe-eval from CSP directives"
            })

    # 4. X-FRAME-OPTIONS
    xfo = headers.get("x-frame-options", "")
    csp_frame = "frame-ancestors" in csp if csp else False
    if not xfo and not csp_frame:
        findings.append({
            "category": "clickjacking",
            "severity": "medium",
            "detail": "X-Frame-Options header missing (clickjacking risk)",
            "recommendation": "Set X-Frame-Options: DENY or use CSP frame-ancestors"
        })
    else:
        passed_checks.append("Clickjacking protection present")

    # 5. X-CONTENT-TYPE-OPTIONS
    xcto = headers.get("x-content-type-options", "")
    if not xcto:
        findings.append({
            "category": "mime_sniffing",
            "severity": "low",
            "detail": "X-Content-Type-Options header missing",
            "recommendation": "Set X-Content-Type-Options: nosniff"
        })
    else:
        passed_checks.append("MIME sniffing protection present")

    # 6. REFERRER POLICY
    rp = headers.get("referrer-policy", "")
    if not rp:
        findings.append({
            "category": "referrer_policy",
            "severity": "low",
            "detail": "Referrer-Policy header not set",
            "recommendation": "Set Referrer-Policy: strict-origin-when-cross-origin"
        })
    else:
        passed_checks.append("Referrer-Policy set")

    # 7. DOM-BASED CHECKS
    # IMPORTANT: No backtick template literals inside page.evaluate() strings.
    # They break Python's string parser. Use string concatenation (x + y) only.
    dom_js = """
() => {
    var issues = [];

    if (location.protocol === 'https:') {
        document.querySelectorAll('img, script, link, iframe').forEach(function(el) {
            var src = el.src || el.href || '';
            if (src.indexOf('http://') === 0) {
                issues.push({
                    category: 'mixed_content',
                    severity: 'high',
                    detail: 'Mixed content: HTTP resource on HTTPS page',
                    element: src.substring(0, 100)
                });
            }
        });
    }

    var inlineScripts = document.querySelectorAll('script:not([src])');
    var xss_patterns = 0;
    inlineScripts.forEach(function(script) {
        var code = script.textContent || '';
        if (
            code.indexOf('document.write(') !== -1 ||
            code.indexOf('innerHTML =') !== -1 ||
            code.indexOf('eval(') !== -1
        ) {
            xss_patterns++;
        }
    });
    if (xss_patterns > 0) {
        issues.push({
            category: 'xss_risk',
            severity: 'medium',
            detail: xss_patterns + ' inline script(s) with potentially dangerous patterns',
            element: xss_patterns + ' scripts'
        });
    }

    var passwordInputs = document.querySelectorAll(
        'input[type="password"]'
    );
    passwordInputs.forEach(function(input) {
        var ac = input.getAttribute('autocomplete');
        if (ac === null || ac === 'on') {
            issues.push({
                category: 'autocomplete',
                severity: 'low',
                detail: 'Password field missing autocomplete=off or new-password',
                element: input.name || 'password'
            });
        }
    });

    var forms = document.querySelectorAll('form');
    var unprotected_forms = 0;
    forms.forEach(function(form) {
        var method = (form.method || 'get').toLowerCase();
        if (method === 'post') {
            var csrfField = form.querySelector(
                'input[name*="csrf"], input[name*="token"], input[name*="_token"]'
            );
            if (!csrfField) {
                unprotected_forms++;
            }
        }
    });
    if (unprotected_forms > 0) {
        issues.push({
            category: 'csrf',
            severity: 'high',
            detail: unprotected_forms + ' POST form(s) with no apparent CSRF token',
            element: unprotected_forms + ' forms'
        });
    }

    var metas = document.querySelectorAll('meta[name="generator"]');
    metas.forEach(function(meta) {
        var content = meta.getAttribute('content') || '';
        if (content) {
            issues.push({
                category: 'version_disclosure',
                severity: 'low',
                detail: 'Server/CMS version disclosed: ' + content.substring(0, 80),
                element: 'meta generator'
            });
        }
    });

    return issues;
}
"""

    try:
        dom_findings = await page.evaluate(dom_js)
        findings.extend(dom_findings)
    except Exception as e:
        logger.warning(f"DOM security scan failed: {e}")

    # 8. PERMISSIONS POLICY
    permissions_policy = headers.get("permissions-policy", "") or headers.get("feature-policy", "")
    if not permissions_policy:
        findings.append({
            "category": "permissions_policy",
            "severity": "low",
            "detail": "Permissions-Policy header not set",
            "recommendation": "Restrict browser features with Permissions-Policy header"
        })

    severity_counts = {
        "critical": sum(1 for f in findings if f.get("severity") == "critical"),
        "high":     sum(1 for f in findings if f.get("severity") == "high"),
        "medium":   sum(1 for f in findings if f.get("severity") == "medium"),
        "low":      sum(1 for f in findings if f.get("severity") == "low"),
    }

    return {
        "is_https": is_https,
        "headers_analyzed": list(headers.keys()),
        "findings": findings,
        "passed_checks": passed_checks,
        "total_issues": len(findings),
        "severity_counts": severity_counts
    }


def compute_security_score(security_data: dict) -> dict:
    if not security_data:
        return {"score": None, "risk_level": None, "breakdown": {}}

    score = 100.0
    breakdown = {}
    severity = security_data.get("severity_counts") or {}

    critical = severity.get("critical", 0)
    c_deduct = min(50.0, critical * 25.0)
    score -= c_deduct
    breakdown["critical"] = {"count": critical, "deduction": round(c_deduct, 1)}

    high = severity.get("high", 0)
    h_deduct = min(40.0, high * 12.0)
    score -= h_deduct
    breakdown["high"] = {"count": high, "deduction": round(h_deduct, 1)}

    medium = severity.get("medium", 0)
    m_deduct = min(25.0, medium * 5.0)
    score -= m_deduct
    breakdown["medium"] = {"count": medium, "deduction": round(m_deduct, 1)}

    low = severity.get("low", 0)
    l_deduct = min(15.0, low * 2.0)
    score -= l_deduct
    breakdown["low"] = {"count": low, "deduction": round(l_deduct, 1)}

    score = max(0.0, min(100.0, score))

    if score >= 85:
        risk_level = "Low"
    elif score >= 65:
        risk_level = "Medium"
    elif score >= 40:
        risk_level = "High"
    else:
        risk_level = "Critical"

    return {
        "score": round(score, 1),
        "risk_level": risk_level,
        "breakdown": breakdown
    }