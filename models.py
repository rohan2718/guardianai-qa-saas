"""
models.py — GuardianAI
Production schema with full admin infrastructure, audit logging, and password reset support.

MIGRATION NOTES — run these SQL commands on existing databases before deploying:

  -- 1. New user fields
  ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255);
  ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE NOT NULL;
  ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE NOT NULL;
  ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
  ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ;
  CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users(email) WHERE email IS NOT NULL;

  -- 2. Audit log table (create_all handles if fresh DB)
  -- 3. Password reset table (create_all handles if fresh DB)

  -- 4. Promote your first admin:
  UPDATE users SET is_admin = TRUE WHERE username = 'YOUR_USERNAME_HERE';
"""

from datetime import datetime, UTC

from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from sqlalchemy.dialects.postgresql import JSONB

db = SQLAlchemy()


# ── User ──────────────────────────────────────────────────────────────────────

class User(UserMixin, db.Model):
    __tablename__ = "users"
    __table_args__ = (
        db.Index("ix_users_is_admin",  "is_admin"),
        db.Index("ix_users_is_active", "is_active"),
        db.Index("ix_users_created_at","created_at"),
    )

    id                 = db.Column(db.Integer, primary_key=True)
    username           = db.Column(db.String(150), unique=True, nullable=False)
    email              = db.Column(db.String(255), unique=True, nullable=True)
    password           = db.Column(db.String(255), nullable=False)
    otp_secret         = db.Column(db.String(32))
    is_2fa_enabled     = db.Column(db.Boolean, default=False, nullable=False)
    plan               = db.Column(db.String(50), default="free", nullable=False)
    scan_limit         = db.Column(db.Integer, default=5)
    page_limit_default = db.Column(db.Integer, default=50)

    # Admin + lifecycle fields
    is_admin      = db.Column(db.Boolean, default=False, nullable=False)
    is_active     = db.Column(db.Boolean, default=True,  nullable=False)
    created_at    = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(UTC))
    last_login_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # Relationships
    test_runs      = db.relationship("TestRun",            backref="owner",    lazy="dynamic")
    audit_logs     = db.relationship("AuditLog",           backref="user",     lazy="dynamic")
    reset_tokens   = db.relationship("PasswordResetToken", backref="user",     lazy="dynamic")

    def is_active_account(self) -> bool:
        """Flask-Login compatible active check."""
        return self.is_active

    # Override Flask-Login's is_active property
    @property
    def is_active(self):
        return self._is_active

    @is_active.setter
    def is_active(self, value):
        self._is_active = value

    # Map column to _is_active
    _is_active = db.Column("is_active", db.Boolean, default=True, nullable=False)


# ── TestRun ───────────────────────────────────────────────────────────────────

class TestRun(db.Model):
    __tablename__ = "test_runs"
    __table_args__ = (
        db.Index("ix_testrun_user_id",    "user_id"),
        db.Index("ix_testrun_started_at", "started_at"),
        db.Index("ix_testrun_status",     "status"),
    )

    id          = db.Column(db.Integer, primary_key=True)
    target_url  = db.Column(db.String(500))
    started_at  = db.Column(db.DateTime(timezone=True))
    finished_at = db.Column(db.DateTime(timezone=True))
    status      = db.Column(db.String(50))  # queued | running | completed | failed

    # Page counts
    total_tests      = db.Column(db.Integer, default=0)
    passed           = db.Column(db.Integer, default=0)
    failed           = db.Column(db.Integer, default=0)
    scanned_pages    = db.Column(db.Integer, default=0)
    discovered_pages = db.Column(db.Integer, default=0)

    # Real-time progress
    progress         = db.Column(db.Integer, default=0)
    avg_scan_time_ms = db.Column(db.Float,   nullable=True)
    eta_seconds      = db.Column(db.Float,   nullable=True)

    # Scan filters — native JSONB
    scan_filters = db.Column(JSONB, nullable=True)

    # File paths
    report_file       = db.Column(db.String(255))
    summary_file      = db.Column(db.String(255))
    raw_file          = db.Column(db.String(255))
    site_summary_file = db.Column(db.String(255))

    # Site-level aggregate scores
    site_health_score = db.Column(db.Float,       nullable=True)
    risk_category     = db.Column(db.String(50),  nullable=True)
    confidence_score  = db.Column(db.Float,       nullable=True)

    # Component averages
    avg_performance_score   = db.Column(db.Float, nullable=True)
    avg_accessibility_score = db.Column(db.Float, nullable=True)
    avg_security_score      = db.Column(db.Float, nullable=True)
    avg_functional_score    = db.Column(db.Float, nullable=True)
    avg_ui_form_score       = db.Column(db.Float, nullable=True)

    # Aggregate issue counts
    total_accessibility_issues = db.Column(db.Integer, nullable=True)
    total_broken_links         = db.Column(db.Integer, nullable=True)
    total_js_errors            = db.Column(db.Integer, nullable=True)
    slow_pages_count           = db.Column(db.Integer, nullable=True)

    # Page risk distribution
    excellent_pages       = db.Column(db.Integer, default=0)
    good_pages            = db.Column(db.Integer, default=0)
    needs_attention_pages = db.Column(db.Integer, default=0)
    critical_pages        = db.Column(db.Integer, default=0)

    # AI narrative fields
    ai_summary      = db.Column(db.Text, nullable=True)
    ai_summary_html = db.Column(db.Text, nullable=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)


# ── PageResult ────────────────────────────────────────────────────────────────

class PageResult(db.Model):
    __tablename__ = "page_results"
    __table_args__ = (
        db.Index("ix_pageresult_run_id",          "run_id"),
        db.Index("ix_pageresult_failure_pattern", "failure_pattern_id"),
        db.Index("ix_pageresult_run_risk",        "run_id", "risk_category"),
    )

    id         = db.Column(db.Integer, primary_key=True)
    run_id     = db.Column(db.Integer, db.ForeignKey("test_runs.id"), nullable=False)
    url        = db.Column(db.String(1000))
    title      = db.Column(db.String(500))
    scanned_at = db.Column(db.DateTime(timezone=True))
    status     = db.Column(db.Integer)

    # Component scores
    health_score        = db.Column(db.Float, nullable=True)
    risk_category       = db.Column(db.String(50), nullable=True)
    performance_score   = db.Column(db.Float, nullable=True)
    accessibility_score = db.Column(db.Float, nullable=True)
    security_score      = db.Column(db.Float, nullable=True)
    functional_score    = db.Column(db.Float, nullable=True)
    ui_form_score       = db.Column(db.Float, nullable=True)

    # Confidence
    confidence_score = db.Column(db.Float,   nullable=True)
    checks_executed  = db.Column(db.Integer, nullable=True)
    checks_null      = db.Column(db.Integer, nullable=True)

    # AI learning fields
    failure_pattern_id      = db.Column(db.String(64),  nullable=True)
    root_cause_tag          = db.Column(db.String(200), nullable=True)
    self_healing_suggestion = db.Column(db.Text,        nullable=True)
    similar_issue_ref       = db.Column(db.Integer,     nullable=True)

    # Metrics
    load_time            = db.Column(db.Float,   nullable=True)
    fcp_ms               = db.Column(db.Float,   nullable=True)
    lcp_ms               = db.Column(db.Float,   nullable=True)
    ttfb_ms              = db.Column(db.Float,   nullable=True)
    accessibility_issues = db.Column(db.Integer, nullable=True)
    broken_links_count   = db.Column(db.Integer, nullable=True)
    js_errors_count      = db.Column(db.Integer, nullable=True)
    is_https             = db.Column(db.Boolean, nullable=True)
    screenshot_path      = db.Column(db.String(500), nullable=True)

    # UI summary — compact JSONB
    ui_summary = db.Column(JSONB, nullable=True)


# ── AuditLog ──────────────────────────────────────────────────────────────────

class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    __table_args__ = (
        db.Index("ix_auditlog_user_id",   "user_id"),
        db.Index("ix_auditlog_action",    "action"),
        db.Index("ix_auditlog_created_at","created_at"),
    )

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    action     = db.Column(db.String(100), nullable=False)
    extra_data = db.Column(JSONB, nullable=True)
    ip_address = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(UTC))


# ── PasswordResetToken ────────────────────────────────────────────────────────

class PasswordResetToken(db.Model):
    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        db.Index("ix_prt_token",      "token"),
        db.Index("ix_prt_user_id",    "user_id"),
        db.Index("ix_prt_expires_at", "expires_at"),
    )

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    token      = db.Column(db.String(128), unique=True, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(UTC))
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    used       = db.Column(db.Boolean, default=False, nullable=False)
