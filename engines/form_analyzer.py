"""
Form Validation Analyzer Engine — GuardianAI
Performs deep form health analysis from DOM data collected by the crawler.
"""

from __future__ import annotations


INTERACTIVE_INPUT_TYPES = {
    "text", "email", "number", "password", "tel", "url", "search",
    "date", "datetime-local", "time", "month", "week", "radio", "checkbox",
}


def _normalized_field_kind(field: dict) -> str:
    """Returns normalized field kind used by runner/generator and analysis checks."""
    tag = (field.get("tag") or "").lower()
    ftype = (field.get("type") or "").lower()
    role = (field.get("role") or "").lower()
    classes = " ".join(field.get("classes") or []) if isinstance(field.get("classes"), list) else str(field.get("class") or "")
    selector = (field.get("selector") or "").lower()

    if tag == "textarea":
        return "textarea"
    if tag == "select" or ftype == "select":
        return "select"
    if ftype == "radio":
        return "radio"
    if ftype == "checkbox":
        return "checkbox"
    if ftype in ("date", "datetime-local", "month", "week", "time"):
        return "date"

    custom_hints = [
        "dropdown", "select2", "react-select", "combobox", "listbox", "multiselect",
    ]
    if tag == "div" and (
        role in ("combobox", "listbox", "menu")
        or any(h in classes.lower() for h in custom_hints)
        or any(h in selector for h in custom_hints)
    ):
        return "custom_dropdown"

    if tag == "input" and (not ftype or ftype in INTERACTIVE_INPUT_TYPES):
        return ftype or "text"

    return "unknown"


def analyze_form(form_raw: dict) -> dict:
    """
    Takes raw form data from Playwright crawl and produces:
    - form_health_score (0–100)
    - form_issue_count
    - per-field issues
    """
    issues = []
    fields = form_raw.get("fields") or []
    normalized_fields = []
    for field in fields:
        kind = _normalized_field_kind(field)
        normalized_fields.append({**field, "field_kind": kind})

    fields = normalized_fields
    field_count = len(fields)

    if field_count == 0:
        return {
            **form_raw,
            "form_health_score": None,
            "form_issue_count": 0,
            "form_issues": [],
        }

    # ── 1. Submit button exists? ──
    has_submit = any(
        (f.get("type") or "").lower() in ("submit", "button")
        or (f.get("tag") or "").lower() == "button"
        or (f.get("role") or "").lower() == "button"
        for f in fields
    )
    if not has_submit:
        # Check if action implies JS submit
        action = form_raw.get("action", "")
        if not action:
            issues.append({
                "type": "missing_submit",
                "severity": "medium",
                "detail": "Form has no visible submit button"
            })

    # ── 2. Required field detection ──
    for field in fields:
        field_type = (field.get("type") or "").lower()
        field_kind = field.get("field_kind") or "unknown"
        field_name = field.get("name") or ""
        field_tag = (field.get("tag") or "input").lower()

        # Skip non-interactive
        if field_type in ("hidden", "submit", "button", "reset"):
            continue
        if field_kind == "unknown" and field_tag not in ("input", "textarea", "select", "div"):
            continue

        # Email fields — type should be 'email'
        name_lower = field_name.lower()
        if any(kw in name_lower for kw in ["email", "e-mail", "mail"]):
            if field_kind != "email":
                issues.append({
                    "type": "wrong_input_type",
                    "severity": "medium",
                    "field": field_name,
                    "detail": f"Field '{field_name}' appears to be email but uses type='{field_type}'"
                })

        # Phone fields
        if any(kw in name_lower for kw in ["phone", "tel", "mobile"]):
            if field_kind not in ("tel", "text"):
                issues.append({
                    "type": "wrong_input_type",
                    "severity": "low",
                    "field": field_name,
                    "detail": f"Field '{field_name}' appears to be phone but uses type='{field_type}'"
                })

        # Number fields
        if any(kw in name_lower for kw in ["age", "quantity", "qty", "count", "number", "amount"]):
            if field_kind not in ("number", "text"):
                issues.append({
                    "type": "wrong_input_type",
                    "severity": "low",
                    "field": field_name,
                    "detail": f"Field '{field_name}' should use type='number'"
                })

        # Placeholder-only labeling (no real label detected)
        # This is inferred by field name — we flag unnamed fields
        if not field_name and field_tag not in ("button", "select") and field_kind != "custom_dropdown":
            issues.append({
                "type": "missing_name",
                "severity": "medium",
                "field": field_name or "(unnamed)",
                "detail": "Input field has no 'name' attribute — form submission will lose this value"
            })

    # ── 3. Method check ──
    method = (form_raw.get("method") or "GET").upper()
    if method == "GET" and field_count > 2:
        # Check if any field looks like sensitive data
        sensitive_names = [(f.get("name") or "").lower() for f in fields]
        if any(kw in n for kw in ["password", "passwd", "secret", "token", "card"] for n in sensitive_names):
            issues.append({
                "type": "sensitive_get_form",
                "severity": "high",
                "detail": "Form with apparent sensitive data uses GET method — data visible in URL"
            })

    # ── 4. Score ──
    deductions = {
        "high": 20.0,
        "medium": 10.0,
        "low": 4.0
    }

    score = 100.0
    for issue in issues:
        sev = issue.get("severity", "low")
        score -= deductions.get(sev, 5.0)

    score = max(0.0, min(100.0, score))

    return {
        **form_raw,
        "fields": fields,
        "form_health_score": round(score, 1),
        "form_issue_count": len(issues),
        "form_issues": issues,
    }


def analyze_all_forms(forms_raw: list) -> list:
    """Runs analyze_form on every form found on the page."""
    return [analyze_form(form) for form in (forms_raw or [])]
