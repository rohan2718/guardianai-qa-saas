"""
models.py — GuardianAI
Extended schema: confidence scores, AI learning fields, scan filters (JSONB), ETA tracking.

MIGRATION NOTE — existing deployments with a TEXT scan_filters column:
  Run once against your database before deploying this version:

    ALTER TABLE test_runs
      ALTER COLUMN scan_filters TYPE JSONB
      USING scan_filters::JSONB;

  If the column is empty/null on all rows you can also simply:
    ALTER TABLE test_runs DROP COLUMN scan_filters;
    ALTER TABLE test_runs ADD COLUMN scan_filters JSONB;
"""

from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from sqlalchemy.dialects.postgresql import JSONB

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(150), unique=True, nullable=False)
    password      = db.Column(db.String(255), nullable=False)
    otp_secret    = db.Column(db.String(32))
    is_2fa_enabled = db.Column(db.Boolean, default=False)
    plan          = db.Column(db.String(50), default="free")
    scan_limit    = db.Column(db.Integer, default=5)
    page_limit_default = db.Column(db.Integer, default=50)

    test_runs = db.relationship("TestRun", backref="owner", lazy=True)


class TestRun(db.Model):
    __tablename__ = "test_runs"
    __table_args__ = (
        db.Index("ix_testrun_user_id",    "user_id"),
        db.Index("ix_testrun_started_at", "started_at"),
        db.Index("ix_testrun_status",     "status"),
    )

    id         = db.Column(db.Integer, primary_key=True)
    target_url = db.Column(db.String(500))
    started_at = db.Column(db.DateTime(timezone=True))
    finished_at = db.Column(db.DateTime(timezone=True))
    status     = db.Column(db.String(50))  # queued | running | completed | failed

    # ── Page counts ──
    total_tests      = db.Column(db.Integer, default=0)
    passed           = db.Column(db.Integer, default=0)
    failed           = db.Column(db.Integer, default=0)
    scanned_pages    = db.Column(db.Integer, default=0)
    discovered_pages = db.Column(db.Integer, default=0)

    # ── Real-time progress ──
    progress          = db.Column(db.Integer, default=0)         # 0–100
    avg_scan_time_ms  = db.Column(db.Float,   nullable=True)
    eta_seconds       = db.Column(db.Float,   nullable=True)

    # ── Scan filters — stored as native JSONB (was TEXT) ──
    # Contains a JSON array of filter key strings, e.g. ["performance","security"]
    scan_filters = db.Column(JSONB, nullable=True)

    # ── File paths ──
    report_file      = db.Column(db.String(255))
    summary_file     = db.Column(db.String(255))
    raw_file         = db.Column(db.String(255))
    site_summary_file = db.Column(db.String(255))

    # ── Site-level aggregate scores ──
    site_health_score = db.Column(db.Float,  nullable=True)
    risk_category     = db.Column(db.String(50), nullable=True)
    confidence_score  = db.Column(db.Float,  nullable=True)

    # ── Component averages ──
    avg_performance_score   = db.Column(db.Float, nullable=True)
    avg_accessibility_score = db.Column(db.Float, nullable=True)
    avg_security_score      = db.Column(db.Float, nullable=True)
    avg_functional_score    = db.Column(db.Float, nullable=True)
    avg_ui_form_score       = db.Column(db.Float, nullable=True)

    # ── Aggregate issue counts ──
    total_accessibility_issues = db.Column(db.Integer, nullable=True)
    total_broken_links         = db.Column(db.Integer, nullable=True)
    total_js_errors            = db.Column(db.Integer, nullable=True)
    slow_pages_count           = db.Column(db.Integer, nullable=True)

    # ── Page risk distribution ──
    excellent_pages       = db.Column(db.Integer, default=0)
    good_pages            = db.Column(db.Integer, default=0)
    needs_attention_pages = db.Column(db.Integer, default=0)
    critical_pages        = db.Column(db.Integer, default=0)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)


class PageResult(db.Model):
    """
    Per-page detailed scan result.
    Primary source for paginated /api/run/<id>/pages/paginated endpoint.
    Raw JSON file is used for export and detail views only.
    """
    __tablename__ = "page_results"
    __table_args__ = (
        db.Index("ix_pageresult_run_id",           "run_id"),
        db.Index("ix_pageresult_failure_pattern",  "failure_pattern_id"),
        db.Index("ix_pageresult_run_risk",         "run_id", "risk_category"),
    )

    id         = db.Column(db.Integer, primary_key=True)
    run_id     = db.Column(db.Integer, db.ForeignKey("test_runs.id"), nullable=False)
    url        = db.Column(db.String(1000))
    title      = db.Column(db.String(500))
    scanned_at = db.Column(db.DateTime(timezone=True))
    status     = db.Column(db.Integer)

    # ── Component scores ──
    health_score        = db.Column(db.Float, nullable=True)
    risk_category       = db.Column(db.String(50), nullable=True)
    performance_score   = db.Column(db.Float, nullable=True)
    accessibility_score = db.Column(db.Float, nullable=True)
    security_score      = db.Column(db.Float, nullable=True)
    functional_score    = db.Column(db.Float, nullable=True)
    ui_form_score       = db.Column(db.Float, nullable=True)

    # ── Confidence ──
    confidence_score = db.Column(db.Float,   nullable=True)
    checks_executed  = db.Column(db.Integer, nullable=True)
    checks_null      = db.Column(db.Integer, nullable=True)

    # ── AI learning fields ──
    failure_pattern_id       = db.Column(db.String(64),  nullable=True)
    root_cause_tag           = db.Column(db.String(200), nullable=True)
    self_healing_suggestion  = db.Column(db.Text,        nullable=True)
    similar_issue_ref        = db.Column(db.Integer,     nullable=True)

    # ── Metrics ──
    load_time            = db.Column(db.Float,   nullable=True)
    fcp_ms               = db.Column(db.Float,   nullable=True)
    lcp_ms               = db.Column(db.Float,   nullable=True)
    ttfb_ms              = db.Column(db.Float,   nullable=True)
    accessibility_issues = db.Column(db.Integer, nullable=True)
    broken_links_count   = db.Column(db.Integer, nullable=True)
    js_errors_count      = db.Column(db.Integer, nullable=True)
    is_https             = db.Column(db.Boolean, nullable=True)
    screenshot_path      = db.Column(db.String(500), nullable=True)

    # ── UI summary (compact) — stored as JSONB ──
    ui_summary = db.Column(JSONB, nullable=True)