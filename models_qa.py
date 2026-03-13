"""
models_qa.py — GuardianAI Autonomous QA
SQLAlchemy models for the QA layer: flows, test cases, results, bugs, regression.

Import and register with the existing db instance from models.py.
Add to models.py: from models_qa import QAFlow, QATestCase, QATestResult, BugReport, RegressionReport
"""

from datetime import datetime, UTC
from models import db     # shared SQLAlchemy instance from existing models.py


class QAFlow(db.Model):
    """A discovered user journey flow."""
    __tablename__ = "qa_flows"

    id          = db.Column(db.Integer, primary_key=True)
    run_id      = db.Column(db.Integer, db.ForeignKey("test_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    flow_id     = db.Column(db.String(64), nullable=False)
    flow_name   = db.Column(db.Text, nullable=False)
    flow_type   = db.Column(db.String(30))
    priority    = db.Column(db.String(10))
    entry_url   = db.Column(db.Text)
    exit_url    = db.Column(db.Text)
    description = db.Column(db.Text)
    tags        = db.Column(db.JSON, default=list)
    steps       = db.Column(db.JSON, default=list)
    created_at  = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(UTC))

    test_cases  = db.relationship("QATestCase", backref="flow", lazy="dynamic",
                                   foreign_keys="QATestCase.qa_flow_db_id",
                                   cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id":          self.id,
            "run_id":      self.run_id,
            "flow_id":     self.flow_id,
            "flow_name":   self.flow_name,
            "flow_type":   self.flow_type,
            "priority":    self.priority,
            "entry_url":   self.entry_url,
            "exit_url":    self.exit_url,
            "description": self.description,
            "tags":        self.tags or [],
            "steps":       self.steps or [],
        }


class QATestCase(db.Model):
    """A generated test case derived from a flow."""
    __tablename__ = "qa_test_cases"

    id                 = db.Column(db.Integer, primary_key=True)
    run_id             = db.Column(db.Integer, db.ForeignKey("test_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    flow_id            = db.Column(db.String(64))
    qa_flow_db_id      = db.Column(db.Integer, db.ForeignKey("qa_flows.id", ondelete="SET NULL"), nullable=True)
    tc_id              = db.Column(db.String(30), nullable=False)
    scenario           = db.Column(db.Text, nullable=False)
    description        = db.Column(db.Text)
    preconditions      = db.Column(db.JSON, default=list)
    steps              = db.Column(db.JSON, default=list)
    expected_result    = db.Column(db.Text)
    actual_result      = db.Column(db.Text)
    status             = db.Column(db.String(10), default="pending", index=True)
    severity           = db.Column(db.String(10), default="medium", index=True)
    tags               = db.Column(db.JSON, default=list)
    playwright_snippet = db.Column(db.Text)
    created_at         = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(UTC))

    result = db.relationship("QATestResult", backref="test_case", lazy="dynamic",
                              foreign_keys="QATestResult.test_case_id",
                              cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id":               self.id,
            "run_id":           self.run_id,
            "tc_id":            self.tc_id,
            "flow_id":          self.flow_id,
            "scenario":         self.scenario,
            "description":      self.description,
            "preconditions":    self.preconditions or [],
            "steps":            self.steps or [],
            "expected_result":  self.expected_result,
            "actual_result":    self.actual_result,
            "status":           self.status,
            "severity":         self.severity,
            "tags":             self.tags or [],
            "playwright_snippet": self.playwright_snippet,
        }


class QATestResult(db.Model):
    """Execution result for a test case."""
    __tablename__ = "qa_test_results"

    id              = db.Column(db.Integer, primary_key=True)
    run_id          = db.Column(db.Integer, db.ForeignKey("test_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    test_case_id    = db.Column(db.Integer, db.ForeignKey("qa_test_cases.id", ondelete="CASCADE"), nullable=True)
    tc_id           = db.Column(db.String(30), nullable=False)
    flow_id         = db.Column(db.String(64))
    scenario        = db.Column(db.Text)
    status          = db.Column(db.String(10), index=True)
    actual_result   = db.Column(db.Text)
    failure_step    = db.Column(db.Integer)
    failure_reason  = db.Column(db.Text)
    duration_ms     = db.Column(db.Float)
    screenshot_path = db.Column(db.Text)
    step_results    = db.Column(db.JSON, default=list)
    created_at      = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(UTC))

    def to_dict(self):
        return {
            "id":             self.id,
            "tc_id":          self.tc_id,
            "flow_id":        self.flow_id,
            "scenario":       self.scenario,
            "status":         self.status,
            "actual_result":  self.actual_result,
            "failure_step":   self.failure_step,
            "failure_reason": self.failure_reason,
            "duration_ms":    self.duration_ms,
            "screenshot_path": self.screenshot_path,
            "step_results":   self.step_results or [],
        }


class BugReport(db.Model):
    """Structured QA bug report — from passive scan or active test failure."""
    __tablename__ = "bug_reports"

    id                 = db.Column(db.Integer, primary_key=True)
    run_id             = db.Column(db.Integer, db.ForeignKey("test_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    page_result_id     = db.Column(db.Integer, db.ForeignKey("page_results.id", ondelete="SET NULL"), nullable=True)
    test_case_id       = db.Column(db.Integer, db.ForeignKey("qa_test_cases.id", ondelete="SET NULL"), nullable=True)
    tc_id              = db.Column(db.String(30))
    flow_id            = db.Column(db.String(64))
    bug_title          = db.Column(db.Text, nullable=False)
    page_url           = db.Column(db.Text)
    bug_type           = db.Column(db.String(30), index=True)
    severity           = db.Column(db.String(10), nullable=False, index=True)
    component          = db.Column(db.Text)
    description        = db.Column(db.Text)
    impact             = db.Column(db.Text)
    steps_to_reproduce = db.Column(db.JSON, default=list)
    expected_result    = db.Column(db.Text)
    actual_result      = db.Column(db.Text)
    suggested_fix      = db.Column(db.Text)
    screenshot_path    = db.Column(db.Text)
    source             = db.Column(db.String(20), default="scan")
    is_resolved        = db.Column(db.Boolean, default=False)
    playwright_snippet = db.Column(db.Text)
    created_at         = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(UTC))

    def to_dict(self):
        return {
            "id":                 self.id,
            "run_id":             self.run_id,
            "tc_id":              self.tc_id,
            "flow_id":            self.flow_id,
            "bug_title":          self.bug_title,
            "page_url":           self.page_url,
            "bug_type":           self.bug_type,
            "severity":           self.severity,
            "component":          self.component,
            "description":        self.description,
            "impact":             self.impact,
            "steps_to_reproduce": self.steps_to_reproduce or [],
            "expected_result":    self.expected_result,
            "actual_result":      self.actual_result,
            "suggested_fix":      self.suggested_fix,
            "screenshot_path":    self.screenshot_path,
            "source":             self.source,
            "is_resolved":        self.is_resolved,
            "playwright_snippet": self.playwright_snippet,
        }


class RegressionReport(db.Model):
    """Scan comparison / regression analysis result."""
    __tablename__ = "regression_reports"

    id              = db.Column(db.Integer, primary_key=True)
    run_id_before   = db.Column(db.Integer, db.ForeignKey("test_runs.id", ondelete="SET NULL"), nullable=True)
    run_id_after    = db.Column(db.Integer, db.ForeignKey("test_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    health_before   = db.Column(db.Float)
    health_after    = db.Column(db.Float)
    health_delta    = db.Column(db.Float)
    bugs_before     = db.Column(db.Integer, default=0)
    bugs_after      = db.Column(db.Integer, default=0)
    bugs_new        = db.Column(db.Integer, default=0)
    bugs_resolved   = db.Column(db.Integer, default=0)
    bugs_persisting = db.Column(db.Integer, default=0)
    verdict         = db.Column(db.String(20))
    summary         = db.Column(db.Text)
    report_data     = db.Column(db.JSON)
    created_at      = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(UTC))

    def to_dict(self):
        return self.report_data or {}