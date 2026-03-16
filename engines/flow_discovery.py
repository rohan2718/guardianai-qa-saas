"""
engines/flow_discovery.py — GuardianAI Autonomous QA  (v2)
Flow Discovery Engine: builds real user journey maps from crawler page_data.

KEY CHANGES v2:
  - _build_link_text_index() builds a {source_url: {dest_url: link_text}} map
    from the enriched nav_menus[].links and sidebar_links collected by crawler.
  - _build_navigation_flow() now uses real link text for step descriptions and
    generates working Playwright selectors (e.g. get_by_role("link", name=…)).
  - Flow names use actual link labels instead of page <title> tags, which fixes
    the "ATIRA → ATIRA → ATIRA" problem caused by identical page titles.
  - Fallback chain when link text is unavailable:
      1. Link text from nav_menus / sidebar_links index
      2. Last URL path segment, title-cased
      3. Page <title> if different from previous step's title
  - Duplicate consecutive labels are de-duplicated in flow names.
  - Breadcrumb-based flows added as a bonus source of real user paths.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
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
    flow_name: str                 # e.g. "Login Flow"
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

_URL_PATTERNS = [
    (r"/(login|signin|log-in|sign-in)",          "login",         "critical", "Login"),
    (r"/(logout|signout|log-out|sign-out)",       "logout",        "high",     "Logout"),
    (r"/(register|signup|sign-up|create.account)","registration",  "critical", "Registration"),
    (r"/(checkout|payment|pay|order)",            "checkout",      "critical", "Checkout"),
    (r"/(cart|basket|bag)",                       "cart",          "high",     "Cart"),
    (r"/(product|item|shop|store|catalogue)",     "shop",          "high",     "Product Browse"),
    (r"/(search|find|query|results)",             "search",        "medium",   "Search"),
    (r"/(contact|support|help|feedback)",         "contact",       "medium",   "Contact"),
    (r"/(profile|account|settings|preferences)",  "profile",       "medium",   "Profile"),
    (r"/(dashboard|home|overview|summary)",       "dashboard",     "high",     "Dashboard"),
    (r"/(password.reset|forgot.password)",        "password_reset","high",     "Password Reset"),
    (r"/(subscribe|newsletter|signup)",           "newsletter",    "low",      "Newsletter"),
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


def _path_label(url: str) -> str:
    """Derive a human-readable label from a URL path segment."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if not parts:
        return "Home"
    return parts[-1].replace("-", " ").replace("_", " ").title()


# ── Page Index ─────────────────────────────────────────────────────────────────

def _build_page_index(page_data: list[dict]) -> dict[str, dict]:
    """url → page_object. Normalises trailing slashes."""
    return {p["url"].rstrip("/"): p for p in page_data if p.get("url")}


# ── Link Text Index ────────────────────────────────────────────────────────────

def _build_link_text_index(page_data: list[dict]) -> dict[str, dict[str, str]]:
    """
    Builds a two-level map:
        index[source_page_url][destination_url] = "link text"

    Sources used (in order of reliability):
      1. nav_menus[].links[]  — enriched nav link objects from crawler v2
      2. sidebar_links[]      — enriched sidebar link objects from crawler v2

    This index enables flow_discovery to say:
        "Click 'Country Master'"
    instead of:
        "Follow link to ATIRA"

    Falls back gracefully when crawler v1 data (no .links array) is present.
    """
    index: dict[str, dict[str, str]] = defaultdict(dict)

    for page in page_data:
        source = page["url"].rstrip("/")

        # ── Source 1: nav_menus (each nav has a .links array in v2) ───────
        for nav in (page.get("nav_menus") or []):
            for link in (nav.get("links") or []):
                href = (link.get("href") or "").rstrip("/")
                text = (
                    link.get("text") or
                    link.get("aria_label") or
                    link.get("title") or
                    ""
                ).strip()
                if href and text:
                    # Prefer first seen (earlier nav = primary navigation)
                    if href not in index[source]:
                        index[source][href] = text

        # ── Source 2: sidebar_links ────────────────────────────────────────
        for link in (page.get("sidebar_links") or []):
            href = (link.get("href") or "").rstrip("/")
            text = (link.get("text") or link.get("aria_label") or "").strip()
            if href and text and href not in index[source]:
                index[source][href] = text

    return dict(index)


# ── Step Builder ───────────────────────────────────────────────────────────────

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


def _playwright_link_selector(link_text: str) -> str:
    """
    Generates a robust Playwright selector from link text.
    Prefers get_by_role pattern string for readable output.
    """
    # Escape any quotes in the link text
    safe = link_text.replace('"', '\\"')
    return f'a:has-text("{safe}"), [role="menuitem"]:has-text("{safe}"), [role="link"]:has-text("{safe}")'


# ── Form Flow Builder ──────────────────────────────────────────────────────────

def _build_form_flow(page: dict, idx: dict, flow_counter: list) -> Optional[FlowDefinition]:
    """
    Creates a flow for any page that has a recognisable form.
    Generates real field selectors and meaningful expected outcomes.
    """
    forms = page.get("forms") or []
    if not forms:
        return None

    purpose_rank = {
        "Login": 0, "Checkout": 1, "Registration": 2, "Search": 3,
        "Contact": 4, "Newsletter": 5, "Feedback": 6,
    }
    forms_with_purpose = [f for f in forms if f.get("form_purpose")]
    if not forms_with_purpose:
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

    root_page = next((p for u, p in idx.items() if _is_root(u)), None)
    if root_page and root_page["url"] != page["url"]:
        steps.append(_make_step(
            n, root_page, "navigate", "Open the homepage",
            outcome="Homepage loads successfully"
        ))
        n += 1

    steps.append(_make_step(
        n, page, "navigate",
        f"Navigate to {purpose.lower()} page",
        outcome=f"{purpose} page loads — form is visible"
    ))
    n += 1

    # Fill each visible field with typed test values
    for f in (target_form.get("fields") or []):
        if f.get("type") in ("submit", "button", "reset", "hidden", "image"):
            continue
        if f.get("readonly") or f.get("disabled"):
            continue

        label = (
            f.get("display_name") or
            f.get("placeholder") or
            f.get("name") or
            f.get("type") or
            "field"
        )
        ftype = f.get("type") or "text"

        # Generate realistic test values
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

        # Build a real, prioritised selector
        selector = None
        if f.get("id"):
            selector = f'#{f["id"]}'
        elif f.get("name"):
            selector = f'[name="{f["name"]}"]'
        elif label:
            safe_label = label.replace('"', '\\"')
            selector = f'input[placeholder="{safe_label}"], [aria-label="{safe_label}"]'

        steps.append(_make_step(
            n, page, "fill_form",
            f"Enter {label}: '{test_val}'",
            selector=selector,
            outcome=f"Field accepts input",
            form_purpose=purpose,
        ))
        n += 1

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
        selector=f'button[type="submit"], input[type="submit"], button:has-text("{submit_label}")',
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


# ── Navigation Flow Builder ────────────────────────────────────────────────────

def _build_navigation_flow(
    start_page: dict,
    page_chain: list[dict],
    flow_counter: list,
    link_text_index: dict = None,
) -> FlowDefinition:
    """
    Builds a navigation flow through a sequence of linked pages.

    Uses the link_text_index to get the actual text of the link that
    connects page N to page N+1. This fixes the "ATIRA → ATIRA" problem
    by using real nav labels instead of page <title> tags.

    Selector strategy:
      - If link text found in index → generate :has-text() selector
      - If no link text → use URL path label as fallback
    """
    link_text_index = link_text_index or {}

    flow_counter[0] += 1
    fid = f"flow_navigation_{flow_counter[0]:03d}"

    steps: list[FlowStep] = []
    step_labels: list[str] = []

    for i, page in enumerate(page_chain, 1):
        if i == 1:
            # First step: navigate to entry page
            page_label = page.get("title") or _path_label(page["url"])
            detail = f"Open {page_label}"
            action = "navigate"
            selector = None
            outcome = f"Page loads successfully — {page_label}"
            step_labels.append(page_label)
        else:
            prev_page = page_chain[i - 2]
            prev_url  = prev_page["url"].rstrip("/")
            curr_url  = page["url"].rstrip("/")

            # Look up the actual link text used to navigate to this page
            link_text = (link_text_index.get(prev_url) or {}).get(curr_url)

            if not link_text:
                # Fallback 1: URL path segment
                link_text = _path_label(page["url"])

            detail   = f'Click "{link_text}"'
            action   = "click"
            selector = _playwright_link_selector(link_text)
            outcome  = (
                f"Page loads — {page.get('title') or _path_label(page['url'])} "
                f"is visible"
            )
            step_labels.append(link_text)

        steps.append(_make_step(
            i, page, action, detail,
            selector=selector,
            outcome=outcome,
        ))

    # ── Build a meaningful flow name from real link labels ─────────────────
    # De-duplicate consecutive identical labels (the root cause of "ATIRA → ATIRA")
    deduped: list[str] = []
    for label in step_labels:
        if not deduped or label.lower().strip() != deduped[-1].lower().strip():
            deduped.append(label)

    # If dedup collapsed everything to 1 label, all pages have identical titles
    # → fall back to URL path segments which are always unique
    if len(deduped) < 2:
        deduped = [_path_label(p["url"]) for p in page_chain]
        # Deduplicate again with path labels
        deduped_2: list[str] = []
        for label in deduped:
            if not deduped_2 or label != deduped_2[-1]:
                deduped_2.append(label)
        deduped = deduped_2

    flow_name = " → ".join(s[:30] for s in deduped)

    # Classify the destination for type/priority
    last = page_chain[-1]
    ft, priority, _ = _classify_url(last["url"])
    # Navigation flows that don't match a special URL pattern stay as "navigation"
    if ft in ("login", "registration", "checkout"):
        flow_type = ft
    else:
        flow_type = "navigation"

    return FlowDefinition(
        flow_id=fid,
        flow_name=flow_name,
        flow_type=flow_type,
        priority=priority if flow_type != "navigation" else "medium",
        steps=steps,
        entry_url=page_chain[0]["url"],
        exit_url=last["url"],
        description=f"Tests navigation: {flow_name}",
        tags=["navigation"],
    )


# ── Breadcrumb Flow Builder ────────────────────────────────────────────────────

def _build_breadcrumb_flow(page: dict, flow_counter: list) -> Optional[FlowDefinition]:
    """
    Builds a navigation flow from breadcrumb data when available.
    Breadcrumbs are the highest-signal source of real user journeys.
    Example: Home → Products → Widget A → Specifications
    """
    breadcrumbs = page.get("breadcrumbs") or {}
    items = breadcrumbs.get("items") or []

    if len(items) < 2:
        return None

    # Remove empty/whitespace items
    items = [i.strip() for i in items if i and i.strip()]
    if len(items) < 2:
        return None

    flow_counter[0] += 1
    fid = f"flow_breadcrumb_{flow_counter[0]:03d}"

    # Build synthetic steps from breadcrumb labels
    steps: list[FlowStep] = []
    for i, label in enumerate(items, 1):
        if i == 1:
            action = "navigate"
            detail = f"Open {label}"
            selector = None
        else:
            action = "click"
            safe = label.replace('"', '\\"')
            detail = f'Click "{label}" in breadcrumb'
            selector = f'[aria-label*="breadcrumb"] a:has-text("{safe}"), .breadcrumb a:has-text("{safe}")'

        steps.append(FlowStep(
            step_number=i,
            page_url=page["url"],
            page_title=page.get("title") or label,
            action=action,
            action_detail=detail,
            element_selector=selector,
            expected_outcome=f"{label} page loads successfully",
        ))

    flow_name = " → ".join(items[:4])

    return FlowDefinition(
        flow_id=fid,
        flow_name=flow_name,
        flow_type="navigation",
        priority="medium",
        steps=steps,
        entry_url=page["url"],
        exit_url=page["url"],
        description=f"Breadcrumb path: {flow_name}",
        tags=["breadcrumb", "navigation"],
    )


# ── Main Entry Point ───────────────────────────────────────────────────────────

def discover_flows(page_data: list[dict]) -> list[FlowDefinition]:
    """
    Main entry point. Accepts the page_data list produced by crawler.py
    and returns a list of FlowDefinition objects.

    Pipeline:
      1. Form-based flows (login, checkout, registration) — highest priority
      2. Navigation flows using real link text from nav_menus / sidebar_links
      3. Breadcrumb flows — when present, highest fidelity paths
      4. De-duplicate and sort by priority

    Integration: call from tasks.py after crawler completes, before persist.
    """
    if not page_data:
        return []

    idx              = _build_page_index(page_data)
    link_text_index  = _build_link_text_index(page_data)
    flows: list[FlowDefinition] = []
    flow_counter     = [0]
    seen_form_types: set[str] = set()

    # ── 1. Form-based flows ───────────────────────────────────────────────────
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

    # ── 2. Navigation flows using link text index ──────────────────────────────
    root_pages = [p for p in page_data if _is_root(p["url"])]
    if not root_pages:
        root_pages = page_data[:1]

    visited_chains: set[tuple] = set()

    for root in root_pages[:2]:
        connected = root.get("connected_pages") or []
        for linked_url in connected[:10]:
            linked_url_norm = linked_url.rstrip("/")
            linked_page = idx.get(linked_url_norm)
            if not linked_page:
                continue

            chain_key = (root["url"].rstrip("/"), linked_url_norm)
            if chain_key in visited_chains:
                continue
            visited_chains.add(chain_key)

            chain = [root, linked_page]

            # Try to extend chain to depth 3
            linked_connected = linked_page.get("connected_pages") or []
            for deep_url in linked_connected[:5]:
                deep_norm = deep_url.rstrip("/")
                deep_page = idx.get(deep_norm)
                if (deep_page and
                    deep_norm not in (root["url"].rstrip("/"), linked_url_norm)):
                    chain.append(deep_page)
                    break

            if len(chain) >= 2:
                flows.append(_build_navigation_flow(
                    root, chain, flow_counter, link_text_index
                ))

        if len([f for f in flows if f.flow_type == "navigation"]) >= 6:
            break

    # ── 3. Breadcrumb flows ────────────────────────────────────────────────────
    breadcrumb_flows_added = 0
    for page in page_data:
        if breadcrumb_flows_added >= 3:
            break
        bc_flow = _build_breadcrumb_flow(page, flow_counter)
        if bc_flow:
            flows.append(bc_flow)
            breadcrumb_flows_added += 1

    # ── 4. Deduplicate by flow_name ────────────────────────────────────────────
    seen_names: set[str] = set()
    unique_flows: list[FlowDefinition] = []
    for f in flows:
        name_key = f.flow_name.lower().strip()
        if name_key not in seen_names:
            seen_names.add(name_key)
            unique_flows.append(f)
    flows = unique_flows

    # ── 5. Sort by priority ────────────────────────────────────────────────────
    priority_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    flows.sort(key=lambda f: priority_rank.get(f.priority, 9))

    logger.info(
        f"[flow_discovery] Discovered {len(flows)} flows from {len(page_data)} pages "
        f"(link_text_index covers {len(link_text_index)} source pages)"
    )
    return flows


def discover_flows_as_dicts(page_data: list[dict]) -> list[dict]:
    """Convenience wrapper returning plain dicts for JSON serialisation."""
    return [f.to_dict() for f in discover_flows(page_data)]