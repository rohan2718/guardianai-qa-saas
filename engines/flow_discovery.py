"""
engines/flow_discovery.py — GuardianAI Autonomous QA
Flow Discovery Engine: builds user journey maps from crawler page_data.

Consumes existing page_object dicts (url, forms, connected_pages, nav_menus,
form_purpose, ui_elements) — zero new crawl infrastructure needed.

Output: list[FlowDefinition] — structured user flows ready for test generation.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ── Data Structures ────────────────────────────────────────────────────────────

@dataclass
class FlowStep:
    step_number: int
    page_url: str
    page_title: str
    action: str                    # "navigate" | "fill_form" | "click" | "submit"
    action_detail: str             # Human-readable description
    element_selector: Optional[str] = None
    form_purpose: Optional[str] = None
    expected_outcome: Optional[str] = None


@dataclass
class FlowDefinition:
    flow_id: str                   # e.g. "flow_login_001"
    flow_name: str                 # e.g. "User Login"
    flow_type: str                 # login|registration|checkout|navigation|search|contact|generic
    priority: str                  # critical|high|medium|low
    steps: list[FlowStep] = field(default_factory=list)
    entry_url: str = ""
    exit_url: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "flow_id":    self.flow_id,
            "flow_name":  self.flow_name,
            "flow_type":  self.flow_type,
            "priority":   self.priority,
            "entry_url":  self.entry_url,
            "exit_url":   self.exit_url,
            "description": self.description,
            "tags":       self.tags,
            "steps": [
                {
                    "step_number":      s.step_number,
                    "page_url":         s.page_url,
                    "page_title":       s.page_title,
                    "action":           s.action,
                    "action_detail":    s.action_detail,
                    "element_selector": s.element_selector,
                    "form_purpose":     s.form_purpose,
                    "expected_outcome": s.expected_outcome,
                }
                for s in self.steps
            ],
        }


# ── URL Classification ─────────────────────────────────────────────────────────

# Slug patterns → (flow_type, priority, label)
_URL_PATTERNS = [
    (r"/(login|signin|log-in|sign-in)",         "login",        "critical", "Login"),
    (r"/(logout|signout|log-out|sign-out)",      "logout",       "high",     "Logout"),
    (r"/(register|signup|sign-up|create.account)","registration","critical", "Registration"),
    (r"/(checkout|payment|pay|order)",           "checkout",     "critical", "Checkout"),
    (r"/(cart|basket|bag)",                      "cart",         "high",     "Cart"),
    (r"/(product|item|shop|store|catalogue)",    "shop",         "high",     "Product Browse"),
    (r"/(search|find|query|results)",            "search",       "medium",   "Search"),
    (r"/(contact|support|help|feedback)",        "contact",      "medium",   "Contact"),
    (r"/(profile|account|settings|preferences)", "profile",      "medium",   "Profile"),
    (r"/(dashboard|home|overview|summary)",      "dashboard",    "high",     "Dashboard"),
    (r"/(password.reset|forgot.password)",       "password_reset","high",    "Password Reset"),
    (r"/(subscribe|newsletter|signup)",          "newsletter",   "low",      "Newsletter"),
]

def _classify_url(url: str) -> tuple[str, str, str]:
    """Returns (flow_type, priority, label) for a URL path."""
    path = urlparse(url).path.lower()
    for pattern, flow_type, priority, label in _URL_PATTERNS:
        if re.search(pattern, path):
            return flow_type, priority, label
    return "navigation", "low", "Page Visit"


def _is_root(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.path in ("", "/", "/index", "/home")


# ── Page Index Builder ─────────────────────────────────────────────────────────

def _build_page_index(page_data: list[dict]) -> dict[str, dict]:
    """url → page_object. Normalises trailing slashes."""
    return {p["url"].rstrip("/"): p for p in page_data if p.get("url")}


# ── Flow Builders ──────────────────────────────────────────────────────────────

def _make_step(n: int, page: dict, action: str, detail: str,
               selector: str = None, outcome: str = None,
               form_purpose: str = None) -> FlowStep:
    return FlowStep(
        step_number=n,
        page_url=page["url"],
        page_title=page.get("title") or urlparse(page["url"]).path or "/",
        action=action,
        action_detail=detail,
        element_selector=selector,
        expected_outcome=outcome,
        form_purpose=form_purpose,
    )


def _build_form_flow(page: dict, idx: dict, flow_counter: list) -> Optional[FlowDefinition]:
    """
    Creates a flow for any page that has a recognisable form.
    Works with form_purpose already detected by the crawler's JS eval.
    """
    forms = page.get("forms") or []
    if not forms:
        return None

    # Pick the most interesting form (prefer Login/Checkout over generic)
    purpose_rank = {
        "Login": 0, "Checkout": 1, "Registration": 2, "Search": 3,
        "Contact": 4, "Newsletter": 5, "Feedback": 6,
    }
    forms_with_purpose = [f for f in forms if f.get("form_purpose")]
    if not forms_with_purpose:
        # Fall back to any form with a submit button
        forms_with_purpose = [f for f in forms if f.get("has_submit")]
    if not forms_with_purpose:
        return None

    target_form = min(
        forms_with_purpose,
        key=lambda f: purpose_rank.get(f.get("form_purpose") or "", 99)
    )

    purpose = target_form.get("form_purpose") or "Form"
    flow_type_map = {
        "Login": "login", "Registration": "registration",
        "Checkout": "checkout", "Search": "search",
        "Contact": "contact", "Newsletter": "newsletter",
        "Feedback": "contact",
    }
    flow_type = flow_type_map.get(purpose, "generic_form")
    priority_map = {
        "login": "critical", "registration": "critical",
        "checkout": "critical", "search": "medium",
        "contact": "medium", "newsletter": "low",
        "generic_form": "medium",
    }

    flow_counter[0] += 1
    fid = f"flow_{flow_type}_{flow_counter[0]:03d}"

    steps: list[FlowStep] = []
    n = 1

    # Step 1: Navigate to homepage (if there is one and this isn't root)
    root_page = next((p for u, p in idx.items() if _is_root(u)), None)
    if root_page and root_page["url"] != page["url"]:
        steps.append(_make_step(n, root_page, "navigate",
                                "Open the homepage",
                                outcome="Homepage loads successfully"))
        n += 1

    # Step 2: Navigate to form page
    steps.append(_make_step(n, page, "navigate",
                            f"Navigate to {purpose.lower()} page",
                            outcome=f"{purpose} page loads, form is visible"))
    n += 1

    # Steps 3+: Fill each visible field
    for f in (target_form.get("fields") or []):
        if f.get("type") in ("submit", "button", "reset", "hidden", "image"):
            continue
        if f.get("readonly") or f.get("disabled"):
            continue

        label = f.get("display_name") or f.get("placeholder") or f.get("name") or f.get("type") or "field"
        ftype = f.get("type") or "text"

        if ftype == "email":
            test_val = "testuser@example.com"
        elif ftype == "password":
            test_val = "TestPassword123!"
        elif ftype == "tel":
            test_val = "+1 555-0100"
        elif ftype in ("number", "range"):
            test_val = "42"
        elif ftype == "date":
            test_val = "2025-01-15"
        elif ftype == "select":
            opts = f.get("options") or []
            test_val = opts[0]["value"] if opts else "option_1"
        elif ftype == "checkbox":
            test_val = "check"
        else:
            test_val = f"Test {label}"

        selector = None
        if f.get("id"):
            selector = f'#{f["id"]}'
        elif f.get("name"):
            selector = f'[name="{f["name"]}"]'

        steps.append(_make_step(
            n, page, "fill_form",
            f"Enter {label}: '{test_val}'",
            selector=selector,
            outcome=f"Field accepts input",
            form_purpose=purpose,
        ))
        n += 1

    # Final step: submit
    submit_label = target_form.get("submit_label") or "Submit"
    expected_after_submit = {
        "Login":        "User is redirected to dashboard or protected page",
        "Registration": "Account is created; redirect to dashboard or confirmation",
        "Checkout":     "Order is confirmed; confirmation page or receipt is shown",
        "Search":       "Search results page loads with matching results",
        "Contact":      "Success message is displayed; form resets or redirects",
        "Newsletter":   "Subscription confirmation is shown",
    }.get(purpose, "Form submits without errors; success state is visible")

    steps.append(_make_step(
    n, page, "submit",
    f"Click '{submit_label}' button",
    outcome=expected_after_submit,
    form_purpose=purpose,
    ))

    return FlowDefinition(
        flow_id=fid,
        flow_name=f"{purpose} Flow",
        flow_type=flow_type,
        priority=priority_map.get(flow_type, "medium"),
        steps=steps,
        entry_url=root_page["url"] if root_page else page["url"],
        exit_url=page["url"],
        description=f"Tests the {purpose.lower()} form on {page['url']}",
        tags=[purpose.lower(), "form"],
    )


def _build_navigation_flow(start_page: dict, page_chain: list[dict],
                           flow_counter: list) -> FlowDefinition:
    """Builds a navigation flow through a sequence of linked pages."""
    flow_counter[0] += 1
    fid = f"flow_navigation_{flow_counter[0]:03d}"

    steps: list[FlowStep] = []
    for i, page in enumerate(page_chain, 1):
        if i == 1:
            detail = f"Open {page.get('title') or urlparse(page['url']).path}"
            action = "navigate"
        else:
            prev_url = page_chain[i - 2]["url"]
            detail = f"Follow link to {page.get('title') or urlparse(page['url']).path}"
            action = "click"

        steps.append(_make_step(
            i, page, action, detail,
            outcome=f"Page loads with status 200, content is visible",
        ))

    last = page_chain[-1]
    ft, priority, label = _classify_url(last["url"])
    name = " → ".join(
        (p.get("title") or urlparse(p["url"]).path or "/")[:25]
        for p in page_chain
    )

    return FlowDefinition(
        flow_id=fid,
        flow_name=f"Navigation: {name}",
        flow_type=ft,
        priority=priority,
        steps=steps,
        entry_url=page_chain[0]["url"],
        exit_url=last["url"],
        description=f"Tests navigation through {len(page_chain)} pages",
        tags=["navigation"],
    )


# ── Main Entry Point ───────────────────────────────────────────────────────────

def discover_flows(page_data: list[dict]) -> list[FlowDefinition]:
    """
    Main entry point. Accepts the page_data list produced by crawler.py
    and returns a list of FlowDefinition objects.

    Integration: call from tasks.py after crawler completes, before persist.
    """
    if not page_data:
        return []

    idx = _build_page_index(page_data)
    flows: list[FlowDefinition] = []
    flow_counter = [0]   # mutable counter shared across builders

    seen_form_types: set[str] = set()

    # ── 1. Form-based flows (highest value — login, checkout, registration) ──
    # Sort pages so critical forms (login, checkout) are discovered first
    priority_order = {
        "Login": 0, "Checkout": 1, "Registration": 2,
        "Search": 3, "Contact": 4,
    }
    pages_with_forms = sorted(
        [p for p in page_data if p.get("forms")],
        key=lambda p: min(
            (priority_order.get(f.get("form_purpose") or "", 99)
             for f in (p.get("forms") or [])),
            default=99
        )
    )

    for page in pages_with_forms:
        flow = _build_form_flow(page, idx, flow_counter)
        if flow and flow.flow_type not in seen_form_types:
            flows.append(flow)
            seen_form_types.add(flow.flow_type)

    # ── 2. Navigation flows through connected pages ──────────────────────────
    # Build simple 2–4 page chains from connected_pages graph
    root_pages = [p for p in page_data if _is_root(p["url"])]
    if not root_pages:
        root_pages = page_data[:1]  # fall back to first page

    visited_chains: set[tuple] = set()

    for root in root_pages[:2]:
        connected = root.get("connected_pages") or []
        for linked_url in connected[:8]:   # cap fan-out
            linked_url_norm = linked_url.rstrip("/")
            linked_page = idx.get(linked_url_norm)
            if not linked_page:
                continue

            chain_key = (root["url"], linked_url_norm)
            if chain_key in visited_chains:
                continue
            visited_chains.add(chain_key)

            chain = [root, linked_page]

            # Try to extend to depth 3 (e.g. Home → Products → Product Detail)
            linked_connected = linked_page.get("connected_pages") or []
            for deep_url in linked_connected[:4]:
                deep_norm = deep_url.rstrip("/")
                deep_page = idx.get(deep_norm)
                if deep_page and deep_norm not in (root["url"].rstrip("/"), linked_url_norm):
                    chain.append(deep_page)
                    break

            if len(chain) >= 2:
                flows.append(_build_navigation_flow(root, chain, flow_counter))

        # Cap total navigation flows
        if len([f for f in flows if f.flow_type == "navigation"]) >= 5:
            break

    # ── 3. De-duplicate and sort by priority ─────────────────────────────────
    priority_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    flows.sort(key=lambda f: priority_rank.get(f.priority, 9))

    logger.info(f"[flow_discovery] Discovered {len(flows)} flows from {len(page_data)} pages")
    return flows


def discover_flows_as_dicts(page_data: list[dict]) -> list[dict]:
    """Convenience wrapper returning plain dicts for JSON serialisation."""
    return [f.to_dict() for f in discover_flows(page_data)]