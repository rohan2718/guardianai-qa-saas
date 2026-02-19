"""
GuardianAI Flask App — Production SaaS QA Platform
Extended: scan filters, ETA endpoint, confidence score, per-page AI fields API.
Improved: pagination, delete scans, clickable URLs, UX.
"""
from dotenv import load_dotenv
load_dotenv()

import base64
import json
import logging
import os
from datetime import datetime, UTC
from io import BytesIO

import markdown
import pandas as pd
import pyotp
import qrcode
import redis
from flask import (Flask, abort, redirect, render_template,
                   request, send_from_directory, session, jsonify)
from flask_login import (LoginManager, login_required,
                         login_user, logout_user, current_user)
from flask_sqlalchemy import SQLAlchemy
from rq import Queue
from sqlalchemy.engine import URL
from werkzeug.security import check_password_hash, generate_password_hash

from ai_analyzer import analyze_site
from analytics import generate_metrics
from models import db, User, TestRun, PageResult
from config import PLAN_LIMITS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── App Setup ──────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "CHANGE-THIS-IN-PRODUCTION")

SCREENSHOT_DIR = os.path.join(os.getcwd(), "screenshots")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
os.makedirs("reports", exist_ok=True)
os.makedirs("raw", exist_ok=True)

# ── Database ───────────────────────────────────────────────────────────────────

DB_URL = URL.create(
    drivername="postgresql",
    username=os.environ.get("DB_USER", "postgres"),
    password=os.environ.get("DB_PASS", ""),
    host=os.environ.get("DB_HOST", "localhost"),
    database=os.environ.get("DB_NAME", "qa_system"),
)
app.config["SQLALCHEMY_DATABASE_URI"] = DB_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

# ── Redis / RQ ─────────────────────────────────────────────────────────────────

redis_conn = redis.Redis(
    host=os.environ.get("REDIS_HOST", "localhost"),
    port=int(os.environ.get("REDIS_PORT", 6379))
)
task_queue = Queue(connection=redis_conn)

# ── Auth ───────────────────────────────────────────────────────────────────────

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ── Filter definitions (server-side source of truth) ──────────────────────────

SCAN_FILTER_DEFS = [
    {"key": "ui_elements",     "label": "UI Elements",      "group": "UI",          "desc": "Buttons, links, nav, dropdowns, modals, tabs, accordions, pagination"},
    {"key": "form_validation", "label": "Form Validation",  "group": "UI",          "desc": "Input presence, labels, required fields, submit, error messages"},
    {"key": "functional",      "label": "Functional",       "group": "QA",          "desc": "Broken links, 404s, redirect chains, JS errors, API failures"},
    {"key": "accessibility",   "label": "Accessibility",    "group": "Compliance",  "desc": "ARIA, alt text, keyboard nav, color contrast, screen reader"},
    {"key": "performance",     "label": "Performance",      "group": "Performance", "desc": "Load time, FCP, LCP, unused JS/CSS, image optimization"},
    {"key": "security",        "label": "Security",         "group": "Security",    "desc": "HTTPS, mixed content, CSP, XSS patterns, CSRF"},
]

VALID_FILTER_KEYS = {f["key"] for f in SCAN_FILTER_DEFS}

# ── Pagination defaults ────────────────────────────────────────────────────────
DEFAULT_PAGE_SIZE = 25
ALLOWED_PAGE_SIZES = [10, 25, 50]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _plan_limits(user: User) -> dict:
    return PLAN_LIMITS.get(user.plan, PLAN_LIMITS["free"])


def _scans_today(user: User) -> int:
    from datetime import date
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=UTC)
    return TestRun.query.filter(
        TestRun.user_id == user.id,
        TestRun.started_at >= today_start
    ).count()


# ── Jinja2 context processor: inject plan + user for sidebar in ALL templates ──

@app.context_processor
def inject_sidebar_globals():
    """Makes `plan` and `plan_limits` available in every rendered template."""
    from flask_login import current_user as _cu
    if _cu.is_authenticated:
        return {
            "plan":        _cu.plan,
            "plan_limits": _plan_limits(_cu),
        }
    return {"plan": "free", "plan_limits": PLAN_LIMITS.get("free", {})}


def load_history_from_db():
    runs = (TestRun.query
            .filter_by(user_id=current_user.id)
            .order_by(TestRun.id.desc())
            .limit(15)
            .all())
    return [
        {
            "id": r.id,
            "url": r.target_url,
            "status": r.status,
            "time": r.finished_at.strftime("%d %b %H:%M") if r.finished_at else "Pending",
            "health_score": r.site_health_score,
            "risk_category": r.risk_category,
            "total_pages": r.total_tests,
            "confidence_score": r.confidence_score,
        }
        for r in runs
    ]


def enrich_run_context(run: TestRun) -> dict:
    """
    Loads all data files and returns template context for a given run.

    SAFETY CONTRACT — never let None reach a template that iterates:
      • raw_data    → always list   ([] while scan running / file missing)
      • report_data → always list   ([] while scan running / file missing)
      • metrics     → always dict   ({} while scan running / file missing)

    Every file read is individually wrapped so a missing or partially-written
    file (common while a crawl is still in progress) never crashes the view.
    """
    # ── Safe defaults — templates can iterate these even if files aren't ready ─
    raw_data    = []   # type: list
    report_data = []   # type: list
    metrics     = {}   # type: dict
    ai_insight  = None
    site_health = None

    # ── Excel report (written after crawl finishes) ───────────────────────────
    if run.report_file and os.path.exists(run.report_file):
        try:
            df = pd.read_excel(run.report_file).fillna("")
            report_data = df.to_dict(orient="records") or []
            metrics     = generate_metrics(report_data) or {}
        except Exception as exc:
            logger.warning("enrich_run_context: could not load report file %s — %s",
                           run.report_file, exc)
            report_data = []
            metrics     = {}

    # ── Raw JSON per-page results (written incrementally during crawl) ─────────
    if run.raw_file and os.path.exists(run.raw_file):
        try:
            with open(run.raw_file, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            raw_data = loaded if isinstance(loaded, list) else []
        except Exception as exc:
            logger.warning("enrich_run_context: could not load raw file %s — %s",
                           run.raw_file, exc)
            raw_data = []

    # ── AI summary markdown ───────────────────────────────────────────────────
    if run.summary_file and os.path.exists(run.summary_file):
        try:
            try:
                with open(run.summary_file, "r", encoding="utf-8") as fh:
                    ai_insight = fh.read()
            except UnicodeDecodeError:
                with open(run.summary_file, "r", encoding="latin-1") as fh:
                    ai_insight = fh.read()
            if ai_insight:
                ai_insight = markdown.markdown(ai_insight)
        except Exception as exc:
            logger.warning("enrich_run_context: could not load summary file %s — %s",
                           run.summary_file, exc)
            ai_insight = None

    # ── Site-level health JSON ────────────────────────────────────────────────
    if run.site_summary_file and os.path.exists(run.site_summary_file):
        try:
            with open(run.site_summary_file, "r", encoding="utf-8") as fh:
                site_health = json.load(fh)
        except Exception as exc:
            logger.warning("enrich_run_context: could not load site summary %s — %s",
                           run.site_summary_file, exc)
            site_health = None

    # ── Active scan filters ───────────────────────────────────────────────────
    active_filters = []
    if run.scan_filters:
        try:
            active_filters = json.loads(run.scan_filters)
        except Exception:
            active_filters = []

    return {
        "data":             report_data,   # always list
        "ai_insight":       ai_insight,
        "metrics":          metrics,       # always dict
        "raw_data":         raw_data,      # always list — the critical fix
        "site_health":      site_health,
        "current_run":      run,
        "active_filters":   active_filters,
        "scan_filter_defs": SCAN_FILTER_DEFS,
    }


# ── Auth Routes ────────────────────────────────────────────────────────────────

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        if User.query.filter_by(username=username).first():
            return render_template("register.html", error="Username already exists")
        user = User(
            username=username,
            password=generate_password_hash(password),
            otp_secret=pyotp.random_base32(),
            is_2fa_enabled=False,
            plan="free",
            scan_limit=5,
            page_limit_default=50,
        )
        db.session.add(user)
        db.session.commit()
        return redirect("/login")
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("2fa_user_id"):
        user = User.query.get(session["2fa_user_id"])
        totp = pyotp.TOTP(user.otp_secret)
        if request.method == "POST":
            otp = request.form.get("otp")
            if otp and totp.verify(otp):
                user.is_2fa_enabled = True
                db.session.commit()
                login_user(user)
                session.pop("2fa_user_id", None)
                session.pop("qr_code", None)
                return redirect("/")
            return render_template("login.html", show_otp=True,
                                   qr=session.get("qr_code"), error="Invalid OTP")
        return render_template("login.html", show_otp=True, qr=session.get("qr_code"))

    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        user = User.query.filter_by(username=username).first()
        if not user or not check_password_hash(user.password, password):
            return render_template("login.html", error="Invalid credentials")
        totp = pyotp.TOTP(user.otp_secret)
        if user.is_2fa_enabled:
            session["2fa_user_id"] = user.id
            return render_template("login.html", show_otp=True)
        uri = totp.provisioning_uri(name=user.username, issuer_name="GuardianAI")
        img = qrcode.make(uri)
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        qr_base64 = base64.b64encode(buffer.getvalue()).decode()
        session["2fa_user_id"] = user.id
        session["qr_code"] = qr_base64
        return render_template("login.html", show_otp=True, qr=qr_base64)

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect("/login")


# ── Main App Routes ────────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
@login_required
def home():
    if request.method == "POST":
        from tasks import run_scan

        url = request.form.get("url", "").strip()
        page_limit = request.form.get("page_limit") or str(current_user.page_limit_default)

        # ── Extract selected scan filters ──
        selected_filters = request.form.getlist("scan_filters")
        active_filters = [f for f in selected_filters if f in VALID_FILTER_KEYS]
        if not active_filters:
            active_filters = list(VALID_FILTER_KEYS)

        if not url:
            ctx = {
                "data": [], "ai_insight": None, "metrics": {},
                "history": load_history_from_db(), "raw_data": [],
                "site_health": None, "current_run": None,
                "error": "Please enter a URL.",
                "scan_filter_defs": SCAN_FILTER_DEFS,
                "active_filters": active_filters,
            }
            return render_template("index.html", **ctx)

        # ── Plan limit enforcement ──
        limits = _plan_limits(current_user)
        daily_limit = limits.get("scans_per_day")
        if daily_limit is not None and _scans_today(current_user) >= daily_limit:
            ctx = {
                "data": [], "ai_insight": None, "metrics": {},
                "history": load_history_from_db(), "raw_data": [],
                "site_health": None, "current_run": None,
                "error": f"Daily scan limit ({daily_limit}) reached for your {current_user.plan} plan.",
                "scan_filter_defs": SCAN_FILTER_DEFS,
                "active_filters": active_filters,
            }
            return render_template("index.html", **ctx)

        page_cap = limits.get("pages_per_scan")
        if page_cap is not None:
            try:
                page_limit = str(min(int(page_limit), page_cap))
            except (TypeError, ValueError):
                page_limit = str(page_cap)

        run = TestRun(
            target_url=url,
            status="queued",
            started_at=datetime.now(UTC),
            user_id=current_user.id,
            progress=0,
            scan_filters=json.dumps(active_filters),
        )
        db.session.add(run)
        db.session.commit()

        task_queue.enqueue(
            run_scan, run.id, url, page_limit, current_user.id, active_filters,
            job_timeout=3600
        )

        return redirect(f"/run/{run.id}")

    # GET
    run = (TestRun.query
           .filter_by(user_id=current_user.id)
           .order_by(TestRun.id.desc())
           .first())

    ctx = {
        "history": load_history_from_db(),
        "scan_filter_defs": SCAN_FILTER_DEFS,
        "active_filters": list(VALID_FILTER_KEYS),
    }
    if run:
        ctx.update(enrich_run_context(run))
    else:
        ctx.update({"data": [], "ai_insight": None, "metrics": {},
                    "raw_data": [], "site_health": None, "current_run": None})

    return render_template("index.html", **ctx)


@app.route("/run/<int:run_id>")
@login_required
def view_run(run_id):
    run = TestRun.query.get_or_404(run_id)
    if run.user_id != current_user.id:
        abort(403)
    ctx = {"history": load_history_from_db(), "scan_filter_defs": SCAN_FILTER_DEFS}
    ctx.update(enrich_run_context(run))
    return render_template("index.html", **ctx)


@app.route("/new-scan")
@login_required
def new_scan():
    return render_template("index.html",
                            data=[], ai_insight=None, metrics={},
                            history=load_history_from_db(), raw_data=[],
                            site_health=None, current_run=None,
                            scan_filter_defs=SCAN_FILTER_DEFS,
                            active_filters=list(VALID_FILTER_KEYS))


# ── Progress / Status Polling ─────────────────────────────────────────────────

def _progress_payload(run: TestRun) -> dict:
    """Build the shared progress JSON payload from a TestRun row.

    Field names cover both legacy JS (scanned / discovered) and the new
    frontend (scanned_pages / discovered_pages) so both paths keep working.
    """
    scanned    = run.scanned_pages   or 0
    discovered = run.discovered_pages or 0
    remaining  = max(0, discovered - scanned) if discovered else None

    return {
        # Core fields consumed by both old and new polling code
        "status":           run.status,
        "progress":         run.progress or 0,
        # Legacy field names (existing JS uses these)
        "scanned":          scanned,
        "discovered":       discovered,
        "remaining":        remaining,
        "total":            run.total_tests or 0,
        # New explicit field names (new polling JS uses these)
        "scanned_pages":    scanned,
        "discovered_pages": discovered,
        "eta_seconds":      run.eta_seconds,
        # Extra metadata
        "avg_scan_time_ms":  run.avg_scan_time_ms,
        "site_health_score": run.site_health_score,
        "risk_category":     run.risk_category,
        "confidence_score":  run.confidence_score,
    }


@app.route("/progress/<int:run_id>")
@login_required
def scan_progress(run_id):
    """Legacy polling endpoint — kept for backwards compatibility."""
    run = TestRun.query.get_or_404(run_id)
    if run.user_id != current_user.id:
        abort(403)
    return jsonify(_progress_payload(run))


@app.route("/api/run/<int:run_id>/progress")
@login_required
def api_run_progress(run_id):
    """
    REST-style progress endpoint used by the new live-polling frontend.
    Identical payload to /progress/<run_id> — both URLs stay alive so old
    and new JS can coexist during incremental rollout.
    """
    run = TestRun.query.get_or_404(run_id)
    if run.user_id != current_user.id:
        abort(403)
    return jsonify(_progress_payload(run))


# ── Pagination API: Per-Page Results ──────────────────────────────────────────

@app.route("/api/run/<int:run_id>/pages/paginated")
@login_required
def run_pages_paginated(run_id):
    """Returns paginated per-page results for a scan run."""
    run = TestRun.query.get_or_404(run_id)
    if run.user_id != current_user.id:
        abort(403)

    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", DEFAULT_PAGE_SIZE, type=int)
    if per_page not in ALLOWED_PAGE_SIZES:
        per_page = DEFAULT_PAGE_SIZE
    risk_filter = request.args.get("risk", "all")
    sort_by = request.args.get("sort", "health_asc")

    if not run.raw_file or not os.path.exists(run.raw_file):
        return jsonify({"pages": [], "total": 0, "page": page, "per_page": per_page, "total_pages": 0})

    with open(run.raw_file, "r", encoding="utf-8") as f:
        all_pages = json.load(f)

    # Apply risk filter
    if risk_filter != "all":
        all_pages = [p for p in all_pages if (p.get("risk_category") or "Unknown") == risk_filter]

    # Apply sorting
    def sort_key(p):
        hs = p.get("health_score")
        return hs if hs is not None else -1

    if sort_by == "health_asc":
        all_pages.sort(key=sort_key)
    elif sort_by == "health_desc":
        all_pages.sort(key=sort_key, reverse=True)
    elif sort_by == "load_asc":
        all_pages.sort(key=lambda p: p.get("load_time") or 9999)
    elif sort_by == "load_desc":
        all_pages.sort(key=lambda p: p.get("load_time") or 0, reverse=True)

    total = len(all_pages)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    end = start + per_page
    page_slice = all_pages[start:end]

    page_summaries = [
        {
            "url":                  p.get("url"),
            "title":                p.get("title"),
            "status":               p.get("status"),
            "health_score":         p.get("health_score"),
            "risk_category":        p.get("risk_category"),
            "confidence_score":     p.get("confidence_score"),
            "checks_executed":      p.get("checks_executed"),
            "failure_pattern_id":   p.get("failure_pattern_id"),
            "root_cause_tag":       p.get("root_cause_tag"),
            "self_healing_suggestion": p.get("self_healing_suggestion"),
            "performance_score":    p.get("performance_score"),
            "accessibility_score":  p.get("accessibility_score"),
            "security_score":       p.get("security_score"),
            "functional_score":     p.get("functional_score"),
            "ui_form_score":        p.get("ui_form_score"),
            "load_time":            p.get("load_time"),
            "fcp_ms":               p.get("fcp_ms"),
            "lcp_ms":               p.get("lcp_ms"),
            "ttfb_ms":              p.get("ttfb_ms"),
            "accessibility_issues": p.get("accessibility_issues"),
            "broken_links":         len(p.get("broken_links") or []),
            "js_errors":            len(p.get("js_errors") or []),
            "is_https":             p.get("is_https"),
            "screenshot":           p.get("screenshot"),
            "timestamp":            p.get("timestamp"),
            "ui_summary":           p.get("ui_summary") or {},
        }
        for p in page_slice
    ]

    return jsonify({
        "pages":       page_summaries,
        "total":       total,
        "page":        page,
        "per_page":    per_page,
        "total_pages": total_pages,
    })


# ── Pagination API: Scan History ─────────────────────────────────────────────

@app.route("/api/history/paginated")
@login_required
def history_paginated():
    """Returns paginated scan history for the dashboard."""
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", DEFAULT_PAGE_SIZE, type=int)
    if per_page not in ALLOWED_PAGE_SIZES:
        per_page = DEFAULT_PAGE_SIZE
    status_filter = request.args.get("status", "all")

    query = TestRun.query.filter_by(user_id=current_user.id)
    if status_filter != "all":
        query = query.filter_by(status=status_filter)
    query = query.order_by(TestRun.id.desc())

    total = query.count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    runs = query.offset((page - 1) * per_page).limit(per_page).all()

    data = [
        {
            "id":               r.id,
            "target_url":       r.target_url,
            "status":           r.status,
            "started_at":       r.started_at.isoformat() if r.started_at else None,
            "finished_at":      r.finished_at.isoformat() if r.finished_at else None,
            "total_tests":      r.total_tests,
            "passed":           r.passed,
            "failed":           r.failed,
            "site_health_score": r.site_health_score,
            "risk_category":    r.risk_category,
            "confidence_score": r.confidence_score,
            "avg_performance_score":   r.avg_performance_score,
            "avg_accessibility_score": r.avg_accessibility_score,
            "avg_security_score":      r.avg_security_score,
        }
        for r in runs
    ]

    return jsonify({
        "runs":        data,
        "total":       total,
        "page":        page,
        "per_page":    per_page,
        "total_pages": total_pages,
    })


# ── Scores & Data APIs ─────────────────────────────────────────────────────────

@app.route("/api/run/<int:run_id>/scores")
@login_required
def run_scores_api(run_id):
    run = TestRun.query.get_or_404(run_id)
    if run.user_id != current_user.id:
        abort(403)

    active_filters = []
    if run.scan_filters:
        try:
            active_filters = json.loads(run.scan_filters)
        except Exception:
            active_filters = []

    return jsonify({
        "run_id": run_id,
        "status": run.status,
        "site_health_score": run.site_health_score,
        "risk_category": run.risk_category,
        "confidence_score": run.confidence_score,
        "active_filters": active_filters,
        "component_scores": {
            "performance":   run.avg_performance_score,
            "accessibility": run.avg_accessibility_score,
            "security":      run.avg_security_score,
            "functional":    run.avg_functional_score,
            "ui_form":       run.avg_ui_form_score,
        },
        "issue_counts": {
            "accessibility_issues": run.total_accessibility_issues,
            "broken_links":         run.total_broken_links,
            "js_errors":            run.total_js_errors,
            "slow_pages":           run.slow_pages_count,
        },
        "page_distribution": {
            "Excellent":       run.excellent_pages,
            "Good":            run.good_pages,
            "Needs Attention": run.needs_attention_pages,
            "Critical":        run.critical_pages,
        },
        "totals": {
            "total_pages": run.total_tests,
            "passed":      run.passed,
            "failed":      run.failed,
        }
    })


@app.route("/api/run/<int:run_id>/pages")
@login_required
def run_pages_api(run_id):
    """Returns per-page scores from raw JSON file — full data visibility."""
    run = TestRun.query.get_or_404(run_id)
    if run.user_id != current_user.id:
        abort(403)

    if not run.raw_file or not os.path.exists(run.raw_file):
        return jsonify({"pages": []})

    with open(run.raw_file, "r", encoding="utf-8") as f:
        pages = json.load(f)

    page_summaries = [
        {
            "url":                  p.get("url"),
            "title":                p.get("title"),
            "status":               p.get("status"),
            "health_score":         p.get("health_score"),
            "risk_category":        p.get("risk_category"),
            "confidence_score":     p.get("confidence_score"),
            "checks_executed":      p.get("checks_executed"),
            "checks_null":          p.get("checks_null"),
            "failure_pattern_id":   p.get("failure_pattern_id"),
            "root_cause_tag":       p.get("root_cause_tag"),
            "self_healing_suggestion": p.get("self_healing_suggestion"),
            "performance_score":    p.get("performance_score"),
            "accessibility_score":  p.get("accessibility_score"),
            "security_score":       p.get("security_score"),
            "functional_score":     p.get("functional_score"),
            "ui_form_score":        p.get("ui_form_score"),
            "load_time":            p.get("load_time"),
            "fcp_ms":               p.get("fcp_ms"),
            "lcp_ms":               p.get("lcp_ms"),
            "ttfb_ms":              p.get("ttfb_ms"),
            "accessibility_issues": p.get("accessibility_issues"),
            "broken_links":         len(p.get("broken_links") or []),
            "js_errors":            len(p.get("js_errors") or []),
            "is_https":             p.get("is_https"),
            "screenshot":           p.get("screenshot"),
            "timestamp":            p.get("timestamp"),
            "ui_summary":           p.get("ui_summary") or {},
        }
        for p in pages
    ]

    return jsonify({"pages": page_summaries})


@app.route("/api/run/<int:run_id>/page-detail")
@login_required
def run_page_detail_api(run_id):
    """Returns full raw detail for a single page by URL."""
    run = TestRun.query.get_or_404(run_id)
    if run.user_id != current_user.id:
        abort(403)

    page_url = request.args.get("url")
    if not page_url:
        return jsonify({"error": "url parameter required"}), 400

    if not run.raw_file or not os.path.exists(run.raw_file):
        return jsonify({"error": "raw data not available"}), 404

    with open(run.raw_file, "r", encoding="utf-8") as f:
        pages = json.load(f)

    for p in pages:
        if p.get("url") == page_url:
            detail = {k: v for k, v in p.items() if k != "ui_elements"}
            detail["ui_elements_count"] = len(p.get("ui_elements") or [])
            detail["ui_elements_sample"] = (p.get("ui_elements") or [])[:50]
            return jsonify({"page": detail})

    return jsonify({"error": "page not found in raw data"}), 404


@app.route("/api/run/<int:run_id>/ui-elements")
@login_required
def run_ui_elements_api(run_id):
    run = TestRun.query.get_or_404(run_id)
    if run.user_id != current_user.id:
        abort(403)

    if not run.raw_file or not os.path.exists(run.raw_file):
        return jsonify({"pages": []})

    with open(run.raw_file, "r", encoding="utf-8") as f:
        pages = json.load(f)

    result = []
    for p in pages:
        ui_s = p.get("ui_summary") or {}
        forms = p.get("forms") or []
        result.append({
            "url":        p.get("url"),
            "ui_summary": ui_s,
            "forms": {
                "count":            len(forms),
                "avg_health_score": round(
                    sum(f.get("form_health_score") or 0 for f in forms) / len(forms), 1
                ) if forms else None,
                "total_issues": sum(f.get("form_issue_count") or 0 for f in forms),
            },
            "dropdowns":   len(p.get("dropdowns") or []),
            "modals":      len(p.get("modals") or []),
            "tabs":        len(p.get("tabs") or []),
            "accordions":  len(p.get("accordions") or []),
            "nav_menus":   len(p.get("nav_menus") or []),
            "pagination":  len(p.get("pagination") or []),
            "breadcrumbs": (p.get("breadcrumbs") or {}).get("found", False),
            "sidebar":     (p.get("sidebar") or {}).get("found", False),
        })

    return jsonify({"pages": result})


@app.route("/api/run/<int:run_id>/security")
@login_required
def run_security_api(run_id):
    run = TestRun.query.get_or_404(run_id)
    if run.user_id != current_user.id:
        abort(403)

    if not run.raw_file or not os.path.exists(run.raw_file):
        return jsonify({"pages": []})

    with open(run.raw_file, "r", encoding="utf-8") as f:
        pages = json.load(f)

    result = []
    for p in pages:
        sec = p.get("security_data") or {}
        result.append({
            "url":             p.get("url"),
            "security_score":  p.get("security_score"),
            "risk_level":      p.get("security_risk"),
            "is_https":        sec.get("is_https"),
            "total_issues":    sec.get("total_issues"),
            "severity_counts": sec.get("severity_counts"),
            "passed_checks":   sec.get("passed_checks"),
            "findings":        (sec.get("findings") or [])[:10],
        })

    return jsonify({"pages": result})


@app.route("/api/run/<int:run_id>/accessibility")
@login_required
def run_accessibility_api(run_id):
    run = TestRun.query.get_or_404(run_id)
    if run.user_id != current_user.id:
        abort(403)

    if not run.raw_file or not os.path.exists(run.raw_file):
        return jsonify({"pages": []})

    with open(run.raw_file, "r", encoding="utf-8") as f:
        pages = json.load(f)

    result = []
    for p in pages:
        a11y = p.get("accessibility_data") or {}
        result.append({
            "url":               p.get("url"),
            "accessibility_score": p.get("accessibility_score"),
            "risk_level":        p.get("accessibility_risk"),
            "total_issues":      a11y.get("total_issues"),
            "severity_counts":   a11y.get("severity_counts"),
            "wcag_violations":   p.get("wcag_violations"),
            "checks":            a11y.get("checks"),
            "has_skip_nav":      a11y.get("has_skip_nav"),
            "has_lang_attr":     a11y.get("has_lang_attr"),
            "has_main_landmark": a11y.get("has_main_landmark"),
            "issues":            (a11y.get("issues") or [])[:15],
        })

    return jsonify({"pages": result})


@app.route("/api/run/<int:run_id>/ai-fields")
@login_required
def run_ai_fields_api(run_id):
    """Returns AI learning fields for all pages in a run."""
    run = TestRun.query.get_or_404(run_id)
    if run.user_id != current_user.id:
        abort(403)

    page_results = (PageResult.query
                    .filter_by(run_id=run_id)
                    .order_by(PageResult.id)
                    .all())

    if page_results:
        data = [
            {
                "url":                     pr.url,
                "confidence_score":        pr.confidence_score,
                "checks_executed":         pr.checks_executed,
                "checks_null":             pr.checks_null,
                "failure_pattern_id":      pr.failure_pattern_id,
                "root_cause_tag":          pr.root_cause_tag,
                "similar_issue_ref":       pr.similar_issue_ref,
                "ai_confidence":           pr.ai_confidence,
                "self_healing_suggestion": pr.self_healing_suggestion,
                "risk_category":           pr.risk_category,
                "health_score":            pr.health_score,
            }
            for pr in page_results
        ]
        return jsonify({"pages": data})

    if not run.raw_file or not os.path.exists(run.raw_file):
        return jsonify({"pages": []})

    with open(run.raw_file, "r", encoding="utf-8") as f:
        pages = json.load(f)

    data = [
        {
            "url":                     p.get("url"),
            "confidence_score":        p.get("confidence_score"),
            "checks_executed":         p.get("checks_executed"),
            "checks_null":             p.get("checks_null"),
            "failure_pattern_id":      p.get("failure_pattern_id"),
            "root_cause_tag":          p.get("root_cause_tag"),
            "similar_issue_ref":       None,
            "ai_confidence":           p.get("ai_confidence"),
            "self_healing_suggestion": p.get("self_healing_suggestion"),
            "risk_category":           p.get("risk_category"),
            "health_score":            p.get("health_score"),
        }
        for p in pages
    ]
    return jsonify({"pages": data})


@app.route("/api/dashboard/stats")
@login_required
def dashboard_stats_api():
    runs = TestRun.query.filter_by(user_id=current_user.id).all()
    completed = [r for r in runs if r.status == "completed"]

    total_scans = len(runs)
    total_pages = sum(r.total_tests or 0 for r in completed)
    total_passed = sum(r.passed or 0 for r in completed)
    total_failed = sum(r.failed or 0 for r in completed)
    pass_rate = round((total_passed / total_pages) * 100, 1) if total_pages > 0 else None

    health_scores = [r.site_health_score for r in completed if r.site_health_score is not None]
    avg_site_health = round(sum(health_scores) / len(health_scores), 1) if health_scores else None

    confidence_scores = [r.confidence_score for r in completed if r.confidence_score is not None]
    avg_confidence = round(sum(confidence_scores) / len(confidence_scores), 1) if confidence_scores else None

    trend = [
        {"id": r.id, "url": r.target_url, "score": r.site_health_score,
         "confidence": r.confidence_score,
         "date": r.finished_at.isoformat() if r.finished_at else None}
        for r in sorted(completed, key=lambda x: x.id)[-5:]
    ]

    return jsonify({
        "total_scans":      total_scans,
        "total_pages":      total_pages,
        "total_passed":     total_passed,
        "total_failed":     total_failed,
        "pass_rate":        pass_rate,
        "avg_site_health":  avg_site_health,
        "avg_confidence":   avg_confidence,
        "trend":            trend,
    })


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    runs = TestRun.query.filter_by(user_id=current_user.id).all()
    completed = [r for r in runs if r.status == "completed"]

    total_scans = len(runs)
    total_pages = sum(r.total_tests or 0 for r in completed)
    total_passed = sum(r.passed or 0 for r in completed)
    total_failed = sum(r.failed or 0 for r in completed)
    pass_rate = round((total_passed / total_pages) * 100, 1) if total_pages > 0 else 0

    def _avg(vals):
        clean = [v for v in vals if v is not None]
        return round(sum(clean) / len(clean), 1) if clean else None

    avg_perf   = _avg([r.avg_performance_score  for r in completed])
    avg_a11y   = _avg([r.avg_accessibility_score for r in completed])
    avg_sec    = _avg([r.avg_security_score      for r in completed])
    avg_func   = _avg([r.avg_functional_score    for r in completed])
    avg_health = _avg([r.site_health_score       for r in completed])
    avg_confidence = _avg([r.confidence_score    for r in completed])

    total_a11y_issues = sum(r.total_accessibility_issues or 0 for r in completed)
    total_broken      = sum(r.total_broken_links         or 0 for r in completed)
    total_js_errors   = sum(r.total_js_errors            or 0 for r in completed)
    total_slow        = sum(r.slow_pages_count           or 0 for r in completed)

    # Paginated — first page for initial load
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", DEFAULT_PAGE_SIZE, type=int)
    if per_page not in ALLOWED_PAGE_SIZES:
        per_page = DEFAULT_PAGE_SIZE

    runs_query = (TestRun.query
                  .filter_by(user_id=current_user.id)
                  .order_by(TestRun.id.desc()))
    total_runs = runs_query.count()
    total_run_pages = max(1, (total_runs + per_page - 1) // per_page)
    page = max(1, min(page, total_run_pages))

    recent_runs = runs_query.offset((page - 1) * per_page).limit(per_page).all()

    trend_runs = sorted(completed, key=lambda x: x.id)[-10:]
    trend_labels = [f"#{r.id}" for r in trend_runs]
    trend_health  = [r.site_health_score or 0      for r in trend_runs]
    trend_perf    = [r.avg_performance_score or 0   for r in trend_runs]
    trend_a11y    = [r.avg_accessibility_score or 0 for r in trend_runs]
    trend_sec     = [r.avg_security_score or 0      for r in trend_runs]
    trend_confidence = [r.confidence_score or 0     for r in trend_runs]

    return render_template(
        "dashboard.html",
        total_scans=total_scans,
        total_pages=total_pages,
        total_passed=total_passed,
        total_failed=total_failed,
        pass_rate=pass_rate,
        avg_performance=avg_perf,
        avg_accessibility=avg_a11y,
        avg_security=avg_sec,
        avg_functional=avg_func,
        avg_health=avg_health,
        avg_confidence=avg_confidence,
        total_a11y_issues=total_a11y_issues,
        total_broken_links=total_broken,
        total_js_errors=total_js_errors,
        total_slow_pages=total_slow,
        recent_runs=recent_runs,
        # Pagination meta
        current_page=page,
        per_page=per_page,
        total_run_pages=total_run_pages,
        total_runs=total_runs,
        allowed_page_sizes=ALLOWED_PAGE_SIZES,
        trend_labels=json.dumps(trend_labels),
        trend_health=json.dumps(trend_health),
        trend_perf=json.dumps(trend_perf),
        trend_a11y=json.dumps(trend_a11y),
        trend_sec=json.dumps(trend_sec),
        trend_confidence=json.dumps(trend_confidence),
        plan=current_user.plan,
        plan_limits=_plan_limits(current_user),
    )


# ── Static ─────────────────────────────────────────────────────────────────────

@app.route("/screenshots/<path:filename>")
def serve_screenshot(filename):
    return send_from_directory(SCREENSHOT_DIR, filename)


# ── History Management ─────────────────────────────────────────────────────────

@app.route("/history/<int:run_id>", methods=["DELETE"])
@login_required
def delete_history(run_id):
    """DELETE /history/<run_id> — deletes scan + all associated files."""
    run = TestRun.query.get(run_id)
    if not run or run.user_id != current_user.id:
        abort(403)
    _delete_run(run)
    return jsonify({"status": "deleted"})


@app.route("/history/<int:run_id>/delete", methods=["POST"])
@login_required
def delete_history_post(run_id):
    """POST /history/<run_id>/delete — browser-compatible delete (no fetch DELETE)."""
    run = TestRun.query.get(run_id)
    if not run or run.user_id != current_user.id:
        abort(403)
    _delete_run(run)
    return jsonify({"status": "deleted"})


def _delete_run(run: TestRun):
    """Shared delete logic: removes DB records + associated files."""
    # Delete associated files
    for fpath in [run.report_file, run.summary_file, run.raw_file, run.site_summary_file]:
        if fpath and os.path.exists(fpath):
            try:
                os.remove(fpath)
            except OSError:
                pass

    # Delete screenshots for this run
    if run.id:
        screenshot_pattern = f"run_{run.id}_"
        try:
            for fname in os.listdir(SCREENSHOT_DIR):
                if fname.startswith(screenshot_pattern):
                    try:
                        os.remove(os.path.join(SCREENSHOT_DIR, fname))
                    except OSError:
                        pass
        except OSError:
            pass

    # Delete PageResult records
    PageResult.query.filter_by(run_id=run.id).delete()
    db.session.delete(run)
    db.session.commit()

"""
app_patch.py — GuardianAI
Paste these routes into app.py to support the new UI features:
  1. DELETE /run/<id>            — Full scan deletion (DB + files) with JSON response
  2. POST   /history/<id>/delete — Sidebar history item delete (already exists as DELETE,
                                   but sidebar JS uses POST — this alias keeps both working)

IMPORTANT: The existing DELETE /history/<int:run_id> route already handles file + DB cleanup.
This patch adds:
  • A POST alias so sidebar JS fetch('/history/id/delete', {method:'POST'}) works
  • A dedicated /run/<id>/delete endpoint used by the new "Delete Scan" button in the results view

Add these routes BEFORE the "Entry Point" section at the bottom of app.py.
"""

# ── Paste the block below into app.py ─────────────────────────────────────────

"""
@app.route("/run/<int:run_id>/delete", methods=["POST"])
@login_required
def delete_run(run_id):
    \"\"\"Full scan deletion — removes DB records and report files.\"\"\"
    run = TestRun.query.get(run_id)
    if not run or run.user_id != current_user.id:
        return jsonify({"status": "error", "message": "Not found"}), 404

    # Remove files
    for fpath in [run.report_file, run.summary_file, run.raw_file, run.site_summary_file]:
        if fpath and os.path.exists(fpath):
            try:
                os.remove(fpath)
            except OSError:
                pass

    # Remove screenshots for this run's pages
    try:
        page_results = PageResult.query.filter_by(run_id=run_id).all()
        for pr in page_results:
            if pr.screenshot_path and os.path.exists(pr.screenshot_path):
                try:
                    os.remove(pr.screenshot_path)
                except OSError:
                    pass
    except Exception:
        pass

    # Remove DB records
    PageResult.query.filter_by(run_id=run_id).delete()
    db.session.delete(run)
    db.session.commit()

    return jsonify({"status": "deleted", "run_id": run_id})


@app.route("/history/<int:run_id>/delete", methods=["POST"])
@login_required
def delete_history_post(run_id):
    \"\"\"POST alias for sidebar history delete (sidebar JS uses POST).\"\"\"
    run = TestRun.query.get(run_id)
    if not run or run.user_id != current_user.id:
        return jsonify({"status": "error"}), 404

    for fpath in [run.report_file, run.summary_file, run.raw_file, run.site_summary_file]:
        if fpath and os.path.exists(fpath):
            try:
                os.remove(fpath)
            except OSError:
                pass

    PageResult.query.filter_by(run_id=run_id).delete()
    db.session.delete(run)
    db.session.commit()
    return jsonify({"status": "deleted"})
"""

# ── Entry Point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=False, port=5000)