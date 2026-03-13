"""
engines/regression_engine.py — GuardianAI Autonomous QA
Regression Engine: compares two TestRun records to detect new, resolved,
and persisting issues.

No new DB tables needed — queries existing TestRun + PageResult + BugReport.
Produces a RegressionReport that is stored as JSONB on TestRun.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ── Data Structures ────────────────────────────────────────────────────────────

@dataclass
class PageDiff:
    url: str
    status: str                    # improved|degraded|new|removed|unchanged
    health_delta: float = 0.0      # positive = better
    perf_delta: float = 0.0
    a11y_delta: float = 0.0
    sec_delta: float = 0.0
    risk_before: Optional[str] = None
    risk_after: Optional[str] = None
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "url":          self.url,
            "status":       self.status,
            "health_delta": round(self.health_delta, 1),
            "perf_delta":   round(self.perf_delta, 1),
            "a11y_delta":   round(self.a11y_delta, 1),
            "sec_delta":    round(self.sec_delta, 1),
            "risk_before":  self.risk_before,
            "risk_after":   self.risk_after,
            "note":         self.note,
        }


@dataclass
class BugDiff:
    title: str
    severity: str
    bug_type: str
    page_url: str
    status: str                    # new|resolved|persisting

    def to_dict(self) -> dict:
        return {
            "title":    self.title,
            "severity": self.severity,
            "bug_type": self.bug_type,
            "page_url": self.page_url,
            "status":   self.status,
        }


@dataclass
class RegressionReport:
    run_id_before: int
    run_id_after: int
    url_before: str
    url_after: str

    # Health score comparison
    health_before: float = 0.0
    health_after: float = 0.0
    health_delta: float = 0.0

    # Bug counts
    bugs_before: int = 0
    bugs_after: int = 0
    bugs_new: int = 0
    bugs_resolved: int = 0
    bugs_persisting: int = 0

    # Test case outcomes
    tests_before_passed: int = 0
    tests_before_failed: int = 0
    tests_after_passed: int = 0
    tests_after_failed: int = 0

    # Summary
    verdict: str = "unchanged"     # improved|degraded|unchanged
    summary: str = ""

    page_diffs: list[PageDiff] = field(default_factory=list)
    bug_diffs: list[BugDiff] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id_before":       self.run_id_before,
            "run_id_after":        self.run_id_after,
            "url_before":          self.url_before,
            "url_after":           self.url_after,
            "health_before":       round(self.health_before, 1),
            "health_after":        round(self.health_after, 1),
            "health_delta":        round(self.health_delta, 1),
            "bugs_before":         self.bugs_before,
            "bugs_after":          self.bugs_after,
            "bugs_new":            self.bugs_new,
            "bugs_resolved":       self.bugs_resolved,
            "bugs_persisting":     self.bugs_persisting,
            "tests_before_passed": self.tests_before_passed,
            "tests_before_failed": self.tests_before_failed,
            "tests_after_passed":  self.tests_after_passed,
            "tests_after_failed":  self.tests_after_failed,
            "verdict":             self.verdict,
            "summary":             self.summary,
            "page_diffs":          [p.to_dict() for p in self.page_diffs],
            "bug_diffs":           [b.to_dict() for b in self.bug_diffs],
        }


# ── Bug Fingerprinting ─────────────────────────────────────────────────────────

def _bug_fingerprint(bug: dict) -> str:
    """
    Creates a stable identity key for a bug across runs.
    Uses page_url + bug_type + severity (not the title which may vary).
    """
    url = (bug.get("page_url") or "").rstrip("/")
    bug_type = bug.get("bug_type") or "unknown"
    # Use first 60 chars of title for additional specificity
    title_key = (bug.get("bug_title") or "")[:60].lower().strip()
    return f"{url}|{bug_type}|{title_key}"


# ── Page Score Comparison ──────────────────────────────────────────────────────

_RISK_RANK = {"Critical": 0, "Needs Attention": 1, "Good": 2, "Excellent": 3}


def _compare_pages(pages_before: list[dict], pages_after: list[dict]) -> list[PageDiff]:
    """Compares per-page scores between two runs."""
    map_before = {(p.get("url") or "").rstrip("/"): p for p in pages_before}
    map_after  = {(p.get("url") or "").rstrip("/"): p for p in pages_after}

    all_urls = set(map_before) | set(map_after)
    diffs: list[PageDiff] = []

    for url in sorted(all_urls):
        a = map_before.get(url)
        b = map_after.get(url)

        if a and b:
            health_delta = (b.get("health_score") or 0) - (a.get("health_score") or 0)
            perf_delta   = (b.get("performance_score") or 0) - (a.get("performance_score") or 0)
            a11y_delta   = (b.get("accessibility_score") or 0) - (a.get("accessibility_score") or 0)
            sec_delta    = (b.get("security_score") or 0) - (a.get("security_score") or 0)

            risk_before = a.get("risk_category")
            risk_after  = b.get("risk_category")

            if health_delta > 5:
                status = "improved"
                note = f"Health improved by {health_delta:+.0f} points"
            elif health_delta < -5:
                status = "degraded"
                note = f"Health degraded by {abs(health_delta):.0f} points — investigate"
            else:
                status = "unchanged"
                note = "No significant change"

            diffs.append(PageDiff(
                url=url,
                status=status,
                health_delta=health_delta,
                perf_delta=perf_delta,
                a11y_delta=a11y_delta,
                sec_delta=sec_delta,
                risk_before=risk_before,
                risk_after=risk_after,
                note=note,
            ))

        elif b and not a:
            diffs.append(PageDiff(url=url, status="new", note="New page discovered"))

        elif a and not b:
            diffs.append(PageDiff(url=url, status="removed", note="Page no longer reachable"))

    return diffs


# ── Main Regression Function ───────────────────────────────────────────────────

def generate_regression_report(
    run_before,          # TestRun ORM object
    run_after,           # TestRun ORM object
    bugs_before: list[dict],
    bugs_after: list[dict],
    pages_before: list[dict],
    pages_after: list[dict],
) -> RegressionReport:
    """
    Main entry point. Accepts two TestRun objects and their associated bugs/pages.
    Returns a RegressionReport.

    Integration: called from app.py /compare/<id_a>/<id_b> route.
    """
    report = RegressionReport(
        run_id_before=run_before.id,
        run_id_after=run_after.id,
        url_before=run_before.target_url,
        url_after=run_after.target_url,
        health_before=run_before.site_health_score or 0,
        health_after=run_after.site_health_score or 0,
    )

    report.health_delta = report.health_after - report.health_before
    report.bugs_before = len(bugs_before)
    report.bugs_after = len(bugs_after)

    # ── Bug diff ──────────────────────────────────────────────────────────────
    fp_before = {_bug_fingerprint(b): b for b in bugs_before}
    fp_after  = {_bug_fingerprint(b): b for b in bugs_after}

    fp_all = set(fp_before) | set(fp_after)
    bug_diffs: list[BugDiff] = []

    for fp in fp_all:
        b_before = fp_before.get(fp)
        b_after  = fp_after.get(fp)

        if b_before and b_after:
            status = "persisting"
            report.bugs_persisting += 1
        elif b_after and not b_before:
            status = "new"
            report.bugs_new += 1
        else:
            status = "resolved"
            report.bugs_resolved += 1

        source = b_after or b_before
        bug_diffs.append(BugDiff(
            title=source.get("bug_title", "Unknown bug"),
            severity=source.get("severity", "medium"),
            bug_type=source.get("bug_type", "unknown"),
            page_url=source.get("page_url", ""),
            status=status,
        ))

    # Sort: new first, then persisting, then resolved
    order = {"new": 0, "persisting": 1, "resolved": 2}
    bug_diffs.sort(key=lambda b: (order.get(b.status, 9), b.severity))
    report.bug_diffs = bug_diffs

    # ── Page diff ─────────────────────────────────────────────────────────────
    report.page_diffs = _compare_pages(pages_before, pages_after)

    # ── Verdict ───────────────────────────────────────────────────────────────
    if report.health_delta > 5 and report.bugs_new == 0:
        report.verdict = "improved"
    elif report.health_delta < -5 or report.bugs_new > 0:
        report.verdict = "degraded"
    else:
        report.verdict = "unchanged"

    # ── Summary ───────────────────────────────────────────────────────────────
    delta_str = f"{report.health_delta:+.0f}" if report.health_delta else "0"
    report.summary = (
        f"Site health: {report.health_before:.0f} → {report.health_after:.0f} ({delta_str} pts). "
        f"Bugs: {report.bugs_before} → {report.bugs_after} "
        f"({report.bugs_new} new, {report.bugs_resolved} resolved, {report.bugs_persisting} persisting)."
    )

    logger.info(f"[regression_engine] {report.verdict}: {report.summary}")
    return report


def compare_runs_from_db(run_id_a: int, run_id_b: int) -> Optional[dict]:
    """
    Convenience function that loads everything from DB and returns regression dict.
    Designed to be called from an app.py route with app context active.
    """
    try:
        from models import db, TestRun, PageResult, BugReport as BugReportModel

        run_a = db.session.get(TestRun, run_id_a)
        run_b = db.session.get(TestRun, run_id_b)
        if not run_a or not run_b:
            return None

        # Load bugs
        def _bugs_for_run(run_id):
            rows = BugReportModel.query.filter_by(run_id=run_id).all()
            return [{"bug_title": r.bug_title, "page_url": r.page_url,
                     "bug_type": r.bug_type, "severity": r.severity} for r in rows]

        # Load pages
        def _pages_for_run(run_id):
            rows = PageResult.query.filter_by(run_id=run_id).all()
            return [{"url": r.url, "health_score": r.health_score,
                     "performance_score": r.performance_score,
                     "accessibility_score": r.accessibility_score,
                     "security_score": r.security_score,
                     "risk_category": r.risk_category} for r in rows]

        report = generate_regression_report(
            run_before=run_a,
            run_after=run_b,
            bugs_before=_bugs_for_run(run_id_a),
            bugs_after=_bugs_for_run(run_id_b),
            pages_before=_pages_for_run(run_id_a),
            pages_after=_pages_for_run(run_id_b),
        )
        return report.to_dict()
    except Exception as e:
        logger.error(f"[regression_engine] compare_runs_from_db failed: {e}", exc_info=True)
        return None