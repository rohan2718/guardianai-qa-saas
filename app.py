"""
app.py — GuardianAI Flask Application
Production SaaS QA Platform.

SECURITY FIXES applied in this version:
  1. SECRET_KEY hard-fails on startup if not set (via config.py)
  2. CSRF protection via Flask-WTF on all POST forms
  3. Rate limiting via Flask-Limiter on login + scan submission
  4. 2FA QR code stored in Redis (TTL=300s), NOT in the session cookie
  5. All JSON API endpoints exempt from CSRF (stateless Bearer-style auth via flask-login)

ARCHITECTURE FIXES:
  6. Single DB_URL imported from config — no duplicate construction
  7. run_pages_paginated queries PageResult table (LIMIT/OFFSET) — no JSON file load
  8. history_days plan limit enforced in all history queries
  9. scan_filters read/written as native JSONB list (no json.dumps/loads)
 10. generate_metrics_from_run() replaces Excel-file-based aggregation on dashboard
 11. Deprecated Session.query.get() → db.session.get()
 12. Redis connection pool with startup ping validation
"""

from pathlib import Path
from dotenv import load_dotenv

# Always load .env from the project directory, regardless of cwd
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

# config.py validates SECRET_KEY and exits the process if missing
import config  # noqa: E402 — must import before anything else touches os.environ

import base64
import json
import logging
import os
from datetime import datetime, timedelta, UTC
from io import BytesIO

import markdown
import pandas as pd
import pyotp
import qrcode
import redis
from redis.exceptions import ConnectionError as RedisConnectionError
from flask import (Flask, abort, redirect, render_template,
                   request, send_from_directory, session, jsonify)
from flask_login import (LoginManager, login_required,
                         login_user, logout_user, current_user)
from flask_wtf.csrf import CSRFProtect, generate_csrf
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from rq import Queue
from rq.job import Retry
from werkzeug.security import check_password_hash, generate_password_hash

from ai_analyzer import analyze_site
from analytics import generate_metrics, generate_metrics_from_run
from models import db, User, TestRun, PageResult, AuditLog
from decorators import write_audit_log

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── App Setup ──────────────────────────────────────────────────────────────────

app = Flask(__name__)

# Single source of truth — config.py already validated this is non-empty
app.config["SECRET_KEY"]                  = config.SECRET_KEY
app.config["SQLALCHEMY_DATABASE_URI"]     = config.DB_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["WTF_CSRF_TIME_LIMIT"]         = 3600   # 1 hour token lifetime

print("DB URL", str(config.DB_URL) )

SCREENSHOT_DIR = config.SCREENSHOT_DIR
os.makedirs("reports", exist_ok=True)
os.makedirs("raw", exist_ok=True)

# ── Extensions ─────────────────────────────────────────────────────────────────

db.init_app(app)

csrf = CSRFProtect(app)

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=[],           # No global limit — apply per-route
    storage_uri=config.REDIS_URL,
)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ── Redis ──────────────────────────────────────────────────────────────────────

def _make_redis() -> redis.Redis:
    pool = redis.ConnectionPool.from_url(
        config.REDIS_URL,
        max_connections=20,
        socket_connect_timeout=5,
        socket_timeout=None,
        retry_on_timeout=True,
        health_check_interval=30
    )
    conn = redis.Redis(connection_pool=pool)
    try:
        conn.ping()
        logger.info(f"Redis connected: {config.REDIS_HOST}:{config.REDIS_PORT}")
    except RedisConnectionError as e:
        logger.error(f"Redis unavailable at startup: {e}. Job queue will not function.")
    return conn


redis_conn = _make_redis()
task_queue = Queue("default", connection=redis_conn)

# ── Filter definitions (server-side source of truth) ──────────────────────────

SCAN_FILTER_DEFS = [
    {"key": "ui_elements",     "label": "UI Elements",     "group": "UI",          "desc": "Buttons, links, nav, dropdowns, modals, tabs, accordions, pagination"},
    {"key": "form_validation", "label": "Form Validation", "group": "UI",          "desc": "Input presence, labels, required fields, submit, error messages"},
    {"key": "functional",      "label": "Functional",      "group": "QA",          "desc": "Broken links, 404s, redirect chains, JS errors, API failures"},
    {"key": "accessibility",   "label": "Accessibility",   "group": "Compliance",  "desc": "ARIA, alt text, keyboard nav, color contrast, screen reader"},
    {"key": "performance",     "label": "Performance",     "group": "Performance", "desc": "Load time, FCP, LCP, unused JS/CSS, image optimization"},
    {"key": "security",        "label": "Security",        "group": "Security",    "desc": "HTTPS, mixed content, CSP, XSS patterns, CSRF"},
]

VALID_FILTER_KEYS = {f["key"] for f in SCAN_FILTER_DEFS}

# ── Filter icon map (passed to templates for filter pill/card display) ─────────

FILTER_ICONS = {
    "ui_elements":     "fa-solid fa-layer-group",
    "form_validation": "fa-solid fa-wpforms",
    "functional":      "fa-solid fa-link",
    "accessibility":   "fa-solid fa-universal-access",
    "performance":     "fa-solid fa-gauge-high",
    "security":        "fa-solid fa-shield-halved",
}

# ── Pagination defaults ────────────────────────────────────────────────────────

DEFAULT_PAGE_SIZE  = 25
ALLOWED_PAGE_SIZES = [10, 25, 50]

# ── 2FA QR Redis key helpers ───────────────────────────────────────────────────

_QR_TTL = 300  # seconds — QR code expires in 5 minutes

def _qr_redis_key(user_id: int) -> str:
    return f"guardianai:qr:{user_id}"

def _store_qr(user_id: int, qr_base64: str):
    try:
        redis_conn.setex(_qr_redis_key(user_id), _QR_TTL, qr_base64)
    except Exception as e:
        logger.warning(f"Could not store QR in Redis: {e}")

def _get_qr(user_id: int) -> str | None:
    try:
        val = redis_conn.get(_qr_redis_key(user_id))
        return val.decode() if val else None
    except Exception:
        return None

def _delete_qr(user_id: int):
    try:
        redis_conn.delete(_qr_redis_key(user_id))
    except Exception:
        pass


# ── Helpers ────────────────────────────────────────────────────────────────────

def _plan_limits(user: User) -> dict:
    from config import PLAN_LIMITS
    return PLAN_LIMITS.get(user.plan, PLAN_LIMITS["free"])


def _history_cutoff(user: User) -> datetime:
    """Returns the earliest datetime a user's plan allows them to view."""
    limits = _plan_limits(user)
    days   = limits.get("history_days", 7)
    return datetime.now(UTC) - timedelta(days=days)


def _scans_today(user: User) -> int:
    from datetime import date
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=UTC)
    return TestRun.query.filter(
        TestRun.user_id    == user.id,
        TestRun.started_at >= today_start,
    ).count()


def _history_query(user: User):
    """Base query for scan history respecting history_days plan limit."""
    cutoff = _history_cutoff(user)
    return (
        TestRun.query
        .filter(
            TestRun.user_id    == user.id,
            TestRun.started_at >= cutoff,
        )
    )


# ── Jinja2 context processor ───────────────────────────────────────────────────

@app.context_processor
def inject_sidebar_globals():
    if current_user.is_authenticated:
        return {
            "plan":        current_user.plan,
            "plan_limits": _plan_limits(current_user),
        }
    return {"plan": "free", "plan_limits": {}}


# ── History loader (sidebar) ───────────────────────────────────────────────────

def load_history_from_db() -> list:
    runs = (
        _history_query(current_user)
        .order_by(TestRun.id.desc())
        .limit(15)
        .all()
    )
    return [
        {
            "id":             r.id,
            "url":            r.target_url,
            "status":         r.status,
            "time":           r.finished_at.strftime("%d %b %H:%M") if r.finished_at else "Pending",
            "health_score":   r.site_health_score,
            "risk_category":  r.risk_category,
            "total_pages":    r.total_tests,
            "confidence_score": r.confidence_score,
        }
        for r in runs
    ]


# ── Run context builder ────────────────────────────────────────────────────────

def enrich_run_context(run: TestRun) -> dict:
    """
    Loads all data files and returns template context for a given run.
    Uses generate_metrics_from_run() for dashboard metrics (no Excel read).
    Excel is only read when raw file is needed for legacy endpoints.

    Safety contract: raw_data, data, metrics are always list/list/dict — never None.
    """
    raw_data    = []
    report_data = []
    ai_insight  = None
    site_health = None

    # Metrics from DB aggregate fields (fast — no file I/O)
    metrics = generate_metrics_from_run(run) if run.status == "completed" else {}

    # Raw JSON — only loaded for the template's detail view (not for pagination)
    if run.raw_file and os.path.exists(run.raw_file):
        try:
            with open(run.raw_file, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            raw_data = loaded if isinstance(loaded, list) else []
        except Exception as exc:
            logger.warning("enrich_run_context: could not load raw file %s — %s", run.raw_file, exc)

    # AI summary markdown
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
            logger.warning("enrich_run_context: could not load summary %s — %s", run.summary_file, exc)

    # Site health JSON
    if run.site_summary_file and os.path.exists(run.site_summary_file):
        try:
            with open(run.site_summary_file, "r", encoding="utf-8") as fh:
                site_health = json.load(fh)
        except Exception as exc:
            logger.warning("enrich_run_context: could not load site summary %s — %s", run.site_summary_file, exc)

    # Active scan filters — read from JSONB column (already a list)
    active_filters = run.scan_filters or []

    # Dashboard intelligence bundle
    intel = {}
    return {
        "data":             report_data,
        "ai_insight":       ai_insight,
        "metrics":          metrics,
        "raw_data":         raw_data,
        "site_health":      site_health,
        "current_run":      run,
        "active_filters":   active_filters,
        "scan_filter_defs": SCAN_FILTER_DEFS,
        "filter_icons":     FILTER_ICONS,
        "intel":            intel,
    }


# ── Progress payload builder ───────────────────────────────────────────────────

def _progress_payload(run: TestRun) -> dict:
    scanned    = run.scanned_pages    or 0
    discovered = run.discovered_pages or 0
    remaining  = max(0, discovered - scanned) if discovered else None

    return {
        "status":            run.status,
        "progress":          run.progress or 0,
        "scanned":           scanned,
        "discovered":        discovered,
        "remaining":         remaining,
        "total":             run.total_tests or 0,
        "scanned_pages":     scanned,
        "discovered_pages":  discovered,
        "eta_seconds":       run.eta_seconds,
        "avg_scan_time_ms":  run.avg_scan_time_ms,
        "site_health_score": run.site_health_score,
        "risk_category":     run.risk_category,
        "confidence_score":  run.confidence_score,
    }


# ── Delete helper ──────────────────────────────────────────────────────────────

def _delete_run(run: TestRun):
    for fpath in [run.report_file, run.summary_file, run.raw_file, run.site_summary_file]:
        if fpath and os.path.exists(fpath):
            try:
                os.remove(fpath)
            except OSError:
                pass

    if run.id:
        prefix = f"run_{run.id}_"
        try:
            for fname in os.listdir(SCREENSHOT_DIR):
                if fname.startswith(prefix):
                    try:
                        os.remove(os.path.join(SCREENSHOT_DIR, fname))
                    except OSError:
                        pass
        except OSError:
            pass

    PageResult.query.filter_by(run_id=run.id).delete()
    db.session.delete(run)
    db.session.commit()


# ════════════════════════════════════════════════════════════════════════════════
# AUTH ROUTES
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/register", methods=["GET", "POST"])
@limiter.limit("20 per hour")
def register():
    if not config.REGISTRATION_OPEN:
        return render_template("register.html", registration_closed=True)

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email    = request.form.get("email", "").strip().lower() or None
        password = request.form.get("password", "")

        if not username or not password:
            return render_template("register.html", error="Username and password are required.")

        if User.query.filter_by(username=username).first():
            return render_template("register.html", error="Username already exists.")

        if email and User.query.filter(
            db.func.lower(User.email) == email
        ).first():
            return render_template("register.html", error="Email already registered.")

        user = User(
            username           = username,
            email              = email,
            password           = generate_password_hash(password),
            otp_secret         = pyotp.random_base32(),
            is_2fa_enabled     = False,
            plan               = "free",
            scan_limit         = 5,
            page_limit_default = 50,
            is_admin           = False,
        )
        user._is_active = True
        db.session.add(user)
        db.session.commit()

        write_audit_log(
            db, AuditLog,
            user_id  = user.id,
            action   = "register",
            extra_data = {"username": username, "email": email},
        )

        return redirect("/login")

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("20 per minute")
def login():
    reset_success = request.args.get("reset_success")

    # ── 2FA verification flow ────────────────────────────────────────────────
    if session.get("2fa_user_id"):
        user_id = session["2fa_user_id"]
        user    = db.session.get(User, user_id)
        if not user:
            session.clear()
            return redirect("/login")

        totp = pyotp.TOTP(user.otp_secret)

        if request.method == "POST":
            otp = request.form.get("otp", "")
            if otp and totp.verify(otp):
                user.is_2fa_enabled = True
                user.last_login_at  = datetime.now(UTC)
                db.session.commit()
                login_user(user)
                session.pop("2fa_user_id", None)
                _delete_qr(user_id)
                write_audit_log(
                    db, AuditLog,
                    user_id  = user.id,
                    action   = "login_2fa_success",
                    extra_data = {"username": user.username},
                )
                return redirect("/")

            write_audit_log(
                db, AuditLog,
                user_id  = user_id,
                action   = "login_2fa_failed",
                extra_data = {"username": user.username if user else "unknown"},
            )
            qr = _get_qr(user_id)
            return render_template("login.html", show_otp=True, qr=qr, error="Invalid OTP")

        qr = _get_qr(user_id)
        return render_template("login.html", show_otp=True, qr=qr)

    # ── Credential check ─────────────────────────────────────────────────────
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user     = User.query.filter_by(username=username).first()

        if user and not user._is_active:
            write_audit_log(
                db, AuditLog,
                user_id  = user.id,
                action   = "login_suspended",
                extra_data = {"username": username},
            )
            return render_template("login.html", error="Invalid credentials")

        if not user or not check_password_hash(user.password, password):
            write_audit_log(
                db, AuditLog,
                user_id  = user.id if user else None,
                action   = "login_failed",
                extra_data = {"username": username},
            )
            return render_template("login.html", error="Invalid credentials")

        totp = pyotp.TOTP(user.otp_secret)
        session["2fa_user_id"] = user.id

        if user.is_2fa_enabled:
            return render_template("login.html", show_otp=True)

        # First-time 2FA setup — generate QR, store in Redis
        uri = totp.provisioning_uri(name=user.username, issuer_name="GuardianAI")
        img = qrcode.make(uri)
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        qr_base64 = base64.b64encode(buffer.getvalue()).decode()
        _store_qr(user.id, qr_base64)

        return render_template("login.html", show_otp=True, qr=qr_base64)

    return render_template("login.html", reset_success=reset_success)

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect("/login")


# ════════════════════════════════════════════════════════════════════════════════
# MAIN APP ROUTES
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/", methods=["GET", "POST"])
@login_required
@limiter.limit("30 per minute", methods=["POST"])   # Scan submission rate limit
def home():
    if request.method == "POST":
        from tasks import run_scan

        url = request.form.get("url", "").strip()

        # ── Resolve page_limit as int immediately — never pass a raw string downstream ──
        try:
            page_limit = int(request.form.get("page_limit") or current_user.page_limit_default)
        except (TypeError, ValueError):
            page_limit = int(current_user.page_limit_default)

        selected_filters = request.form.getlist("scan_filters")
        active_filters   = [f for f in selected_filters if f in VALID_FILTER_KEYS]
        if not active_filters:
            active_filters = list(VALID_FILTER_KEYS)

        if not url:
            ctx = _empty_ctx(error="Please enter a URL.", active_filters=active_filters)
            return render_template("index.html", **ctx)

        # ── Plan limit enforcement ──
        limits      = _plan_limits(current_user)
        daily_limit = limits.get("scans_per_day")
        if daily_limit is not None and _scans_today(current_user) >= daily_limit:
            ctx = _empty_ctx(
                error=f"Daily scan limit ({daily_limit}) reached for your {current_user.plan} plan.",
                active_filters=active_filters,
            )
            return render_template("index.html", **ctx)

        page_cap = limits.get("pages_per_scan")
        if page_cap is not None:
            page_limit = min(page_limit, page_cap)

        run = TestRun(
            target_url=url,
            status="queued",
            started_at=datetime.now(UTC),
            user_id=current_user.id,
            progress=0,
            scan_filters=active_filters,  # Native JSONB list — no json.dumps()
        )
        db.session.add(run)
        db.session.commit()

        task_queue.enqueue(
            run_scan,
            run.id, url, page_limit, current_user.id, active_filters,
            job_timeout=config.JOB_TIMEOUT,
            retry=Retry(max=1, interval=60),
        )

        return redirect(f"/run/{run.id}")

    # GET — show most recent run
    run = (
        _history_query(current_user)
        .order_by(TestRun.id.desc())
        .first()
    )

    ctx = {
        "history":          load_history_from_db(),
        "scan_filter_defs": SCAN_FILTER_DEFS,
        "active_filters":   list(VALID_FILTER_KEYS),
        "filter_icons":     FILTER_ICONS,
    }
    if run:
        ctx.update(enrich_run_context(run))
    else:
        ctx.update({"data": [], "ai_insight": None, "metrics": {},
                    "raw_data": [], "site_health": None, "current_run": None,
                    "intel": {}})

    return render_template("index.html", **ctx)


def _empty_ctx(error: str = None, active_filters: list = None) -> dict:
    return {
        "data":             [],
        "ai_insight":       None,
        "metrics":          {},
        "history":          load_history_from_db(),
        "raw_data":         [],
        "site_health":      None,
        "current_run":      None,
        "error":            error,
        "scan_filter_defs": SCAN_FILTER_DEFS,
        "active_filters":   active_filters or list(VALID_FILTER_KEYS),
        "filter_icons":     FILTER_ICONS,
        "intel":            {},
    }


@app.route("/run/<int:run_id>")
@login_required
def view_run(run_id):
    run = db.session.get(TestRun, run_id)
    if not run:
        abort(404)
    if run.user_id != current_user.id:
        abort(403)
    ctx = {"history": load_history_from_db(), "scan_filter_defs": SCAN_FILTER_DEFS, "filter_icons": FILTER_ICONS}
    ctx.update(enrich_run_context(run))
    return render_template("index.html", **ctx)


@app.route("/new-scan")
@login_required
def new_scan():
    return render_template(
        "index.html",
        data=[], ai_insight=None, metrics={},
        history=load_history_from_db(), raw_data=[],
        site_health=None, current_run=None,
        scan_filter_defs=SCAN_FILTER_DEFS,
        active_filters=list(VALID_FILTER_KEYS),
        filter_icons=FILTER_ICONS,
        intel={},
    )


# ════════════════════════════════════════════════════════════════════════════════
# PROGRESS / STATUS POLLING  (JSON — CSRF exempt)
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/progress/<int:run_id>")
@login_required
@csrf.exempt
def scan_progress(run_id):
    run = db.session.get(TestRun, run_id)
    if not run:
        abort(404)
    if run.user_id != current_user.id:
        abort(403)
    return jsonify(_progress_payload(run))


@app.route("/api/run/<int:run_id>/progress")
@login_required
@csrf.exempt
def api_run_progress(run_id):
    run = db.session.get(TestRun, run_id)
    if not run:
        abort(404)
    if run.user_id != current_user.id:
        abort(403)
    return jsonify(_progress_payload(run))


# ════════════════════════════════════════════════════════════════════════════════
# PAGINATED PAGES API  — queries PageResult DB table, NOT raw JSON file
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/api/run/<int:run_id>/pages/paginated")
@login_required
@csrf.exempt
def run_pages_paginated(run_id):
    """
    Returns paginated per-page results from the PageResult DB table.
    LIMIT/OFFSET pagination — no full JSON file loaded into memory.
    """
    run = db.session.get(TestRun, run_id)
    if not run:
        abort(404)
    if run.user_id != current_user.id:
        abort(403)

    page     = request.args.get("page",     1,                type=int)
    per_page = request.args.get("per_page", DEFAULT_PAGE_SIZE, type=int)
    if per_page not in ALLOWED_PAGE_SIZES:
        per_page = DEFAULT_PAGE_SIZE

    risk_filter = request.args.get("risk", "all")
    sort_by     = request.args.get("sort", "health_asc")

    query = PageResult.query.filter_by(run_id=run_id)

    if risk_filter != "all":
        query = query.filter(PageResult.risk_category == risk_filter)

    sort_map = {
        "health_asc":  PageResult.health_score.asc().nullslast(),
        "health_desc": PageResult.health_score.desc().nullsfirst(),
        "load_asc":    PageResult.load_time.asc().nullslast(),
        "load_desc":   PageResult.load_time.desc().nullsfirst(),
    }
    query = query.order_by(sort_map.get(sort_by, PageResult.health_score.asc().nullslast()))

    total       = query.count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    page        = max(1, min(page, total_pages))

    rows = query.offset((page - 1) * per_page).limit(per_page).all()

    page_summaries = [
        {
            "url":                     r.url,
            "title":                   r.title,
            "status":                  r.status,
            "health_score":            r.health_score,
            "risk_category":           r.risk_category,
            "confidence_score":        r.confidence_score,
            "checks_executed":         r.checks_executed,
            "failure_pattern_id":      r.failure_pattern_id,
            "root_cause_tag":          r.root_cause_tag,
            "self_healing_suggestion": r.self_healing_suggestion,
            "performance_score":       r.performance_score,
            "accessibility_score":     r.accessibility_score,
            "security_score":          r.security_score,
            "functional_score":        r.functional_score,
            "ui_form_score":           r.ui_form_score,
            "load_time":               r.load_time,
            "fcp_ms":                  r.fcp_ms,
            "lcp_ms":                  r.lcp_ms,
            "ttfb_ms":                 r.ttfb_ms,
            "accessibility_issues":    r.accessibility_issues,
            "broken_links":            r.broken_links_count,
            "js_errors":               r.js_errors_count,
            "is_https":                r.is_https,
            "screenshot":              r.screenshot_path,
            "ui_summary":              r.ui_summary or {},
        }
        for r in rows
    ]

    return jsonify({
        "pages":       page_summaries,
        "total":       total,
        "page":        page,
        "per_page":    per_page,
        "total_pages": total_pages,
    })


# ════════════════════════════════════════════════════════════════════════════════
# SCAN HISTORY API  (JSON — CSRF exempt)
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/api/history/paginated")
@login_required
@csrf.exempt
def history_paginated():
    """Returns paginated scan history, respecting the plan's history_days limit."""
    page     = request.args.get("page",     1,                type=int)
    per_page = request.args.get("per_page", DEFAULT_PAGE_SIZE, type=int)
    if per_page not in ALLOWED_PAGE_SIZES:
        per_page = DEFAULT_PAGE_SIZE
    status_filter = request.args.get("status", "all")

    query = _history_query(current_user)      # ← enforces history_days
    if status_filter != "all":
        query = query.filter_by(status=status_filter)
    query = query.order_by(TestRun.id.desc())

    total       = query.count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    page        = max(1, min(page, total_pages))

    runs = query.offset((page - 1) * per_page).limit(per_page).all()

    data = [
        {
            "id":                      r.id,
            "target_url":              r.target_url,
            "status":                  r.status,
            "started_at":              r.started_at.isoformat() if r.started_at  else None,
            "finished_at":             r.finished_at.isoformat() if r.finished_at else None,
            "total_tests":             r.total_tests,
            "passed":                  r.passed,
            "failed":                  r.failed,
            "site_health_score":       r.site_health_score,
            "risk_category":           r.risk_category,
            "confidence_score":        r.confidence_score,
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


# ════════════════════════════════════════════════════════════════════════════════
# SCORES & DATA APIs  (JSON — CSRF exempt)
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/api/run/<int:run_id>/scores")
@login_required
@csrf.exempt
def run_scores_api(run_id):
    run = db.session.get(TestRun, run_id)
    if not run:
        abort(404)
    if run.user_id != current_user.id:
        abort(403)

    active_filters = run.scan_filters or []   # Native JSONB list

    return jsonify({
        "run_id":            run_id,
        "status":            run.status,
        "site_health_score": run.site_health_score,
        "risk_category":     run.risk_category,
        "confidence_score":  run.confidence_score,
        "active_filters":    active_filters,
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
        },
    })


@app.route("/api/run/<int:run_id>/pages")
@login_required
@csrf.exempt
def run_pages_api(run_id):
    """Returns all per-page data from raw JSON — used for full-detail export views."""
    run = db.session.get(TestRun, run_id)
    if not run:
        abort(404)
    if run.user_id != current_user.id:
        abort(403)

    if not run.raw_file or not os.path.exists(run.raw_file):
        return jsonify({"pages": []})

    with open(run.raw_file, "r", encoding="utf-8") as f:
        pages = json.load(f)

    summaries = [
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
        for p in pages
    ]
    return jsonify({"pages": summaries})


@app.route("/api/run/<int:run_id>/page-detail")
@login_required
@csrf.exempt
def run_page_detail_api(run_id):
    run = db.session.get(TestRun, run_id)
    if not run:
        abort(404)
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
            detail["ui_elements_count"]  = len(p.get("ui_elements") or [])
            detail["ui_elements_sample"] = (p.get("ui_elements") or [])[:50]
            return jsonify({"page": detail})

    return jsonify({"error": "page not found"}), 404


@app.route("/api/run/<int:run_id>/ui-elements")
@login_required
@csrf.exempt
def run_ui_elements_api(run_id):
    run = db.session.get(TestRun, run_id)
    if not run:
        abort(404)
    if run.user_id != current_user.id:
        abort(403)

    if not run.raw_file or not os.path.exists(run.raw_file):
        return jsonify({"pages": []})

    with open(run.raw_file, "r", encoding="utf-8") as f:
        pages = json.load(f)

    result = []
    for p in pages:
        ui_s  = p.get("ui_summary") or {}
        forms = p.get("forms") or []
        result.append({
            "url":        p.get("url"),
            "ui_summary": ui_s,
            "forms": {
                "count": len(forms),
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
@csrf.exempt
def run_security_api(run_id):
    run = db.session.get(TestRun, run_id)
    if not run:
        abort(404)
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
            "findings":        sec.get("findings") or [],
        })
    return jsonify({"pages": result})


# ════════════════════════════════════════════════════════════════════════════════
# HISTORY MANAGEMENT  (CSRF protected — these mutate data)
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/history/<int:run_id>", methods=["DELETE"])
@login_required
@csrf.exempt   # DELETE verb — no form-based CSRF risk; protected by login_required + user_id check
def delete_history(run_id):
    run = db.session.get(TestRun, run_id)
    if not run or run.user_id != current_user.id:
        abort(403)
    _delete_run(run)
    return jsonify({"status": "deleted"})


@app.route("/history/<int:run_id>/delete", methods=["POST"])
@login_required
def delete_history_post(run_id):
    """POST alias — browser JS uses POST for sidebar delete."""
    run = db.session.get(TestRun, run_id)
    if not run or run.user_id != current_user.id:
        abort(403)
    _delete_run(run)
    return jsonify({"status": "deleted"})


@app.route("/run/<int:run_id>/delete", methods=["POST"])
@login_required
def delete_run_post(run_id):
    """Delete from the results view."""
    run = db.session.get(TestRun, run_id)
    if not run or run.user_id != current_user.id:
        return jsonify({"status": "error", "message": "Not found"}), 404
    _delete_run(run)
    return jsonify({"status": "deleted", "run_id": run_id})


# ════════════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/dashboard")
@login_required
def dashboard():
    from sqlalchemy import func

    page     = request.args.get("page",     1,                type=int)
    per_page = request.args.get("per_page", DEFAULT_PAGE_SIZE, type=int)
    if per_page not in ALLOWED_PAGE_SIZES:
        per_page = DEFAULT_PAGE_SIZE

    # All queries respect history_days plan limit
    base_q = _history_query(current_user)

    total_runs  = base_q.count()
    total_pages = base_q.with_entities(func.sum(TestRun.total_tests)).scalar() or 0
    total_passed = base_q.with_entities(func.sum(TestRun.passed)).scalar() or 0
    total_failed = base_q.with_entities(func.sum(TestRun.failed)).scalar() or 0
    pass_rate    = round(total_passed / (total_passed + total_failed) * 100, 1) if (total_passed + total_failed) else 0

    avg_perf      = base_q.with_entities(func.avg(TestRun.avg_performance_score)).scalar()
    avg_a11y      = base_q.with_entities(func.avg(TestRun.avg_accessibility_score)).scalar()
    avg_sec       = base_q.with_entities(func.avg(TestRun.avg_security_score)).scalar()
    avg_func      = base_q.with_entities(func.avg(TestRun.avg_functional_score)).scalar()
    avg_ui_form   = base_q.with_entities(func.avg(TestRun.avg_ui_form_score)).scalar()
    avg_health    = base_q.with_entities(func.avg(TestRun.site_health_score)).scalar()
    avg_confidence = base_q.with_entities(func.avg(TestRun.confidence_score)).scalar()

    def _r(v):
        return round(v, 1) if v is not None else None

    total_a11y_issues = base_q.with_entities(func.sum(TestRun.total_accessibility_issues)).scalar() or 0
    total_broken      = base_q.with_entities(func.sum(TestRun.total_broken_links)).scalar() or 0
    total_js_errors   = base_q.with_entities(func.sum(TestRun.total_js_errors)).scalar() or 0
    total_slow        = base_q.with_entities(func.sum(TestRun.slow_pages_count)).scalar() or 0

    total_run_pages = max(1, (total_runs + per_page - 1) // per_page)
    page            = max(1, min(page, total_run_pages))

    recent_runs = (
        base_q
        .order_by(TestRun.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    trend_runs = base_q.order_by(TestRun.id.desc()).limit(10).all()[::-1]
    # ── Top AI Suggestions across recent runs ─────────────────────────────────
    # Pull the top 8 worst pages (by health score) from the most recent completed run
    top_suggestions = []
    latest_completed = (
        base_q
        .filter(TestRun.status == "completed")
        .order_by(TestRun.id.desc())
        .first()
    )
    if latest_completed:
        from models import PageResult
        worst_pages = (
            PageResult.query
            .filter_by(run_id=latest_completed.id)
            .filter(PageResult.self_healing_suggestion.isnot(None))
            .order_by(PageResult.health_score.asc().nullslast())
            .limit(8)
            .all()
        )
        top_suggestions = [
            {
                "url":        p.url,
                "health":     p.health_score,
                "risk":       p.risk_category,
                "suggestion": p.self_healing_suggestion,
            }
            for p in worst_pages
        ]
    trend_labels     = [r.started_at.strftime("%d %b") if r.started_at else "" for r in trend_runs]
    trend_health     = [r.site_health_score          or 0 for r in trend_runs]
    trend_perf       = [r.avg_performance_score       or 0 for r in trend_runs]
    trend_a11y       = [r.avg_accessibility_score     or 0 for r in trend_runs]
    trend_sec        = [r.avg_security_score          or 0 for r in trend_runs]
    trend_confidence = [r.confidence_score            or 0 for r in trend_runs]

    return render_template(
        "dashboard.html",
        total_scans=total_runs,
        total_pages=total_pages,
        total_passed=total_passed,
        total_failed=total_failed,
        pass_rate=pass_rate,
        avg_performance=_r(avg_perf),
        avg_accessibility=_r(avg_a11y),
        avg_security=_r(avg_sec),
        avg_functional=_r(avg_func),
        avg_ui_form=_r(avg_ui_form),
        avg_health=_r(avg_health),
        avg_confidence=_r(avg_confidence),
        total_a11y_issues=total_a11y_issues,
        total_broken_links=total_broken,
        total_js_errors=total_js_errors,
        total_slow_pages=total_slow,
        recent_runs=recent_runs,
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
@login_required
def serve_screenshot(filename):
    # login_required prevents unauthenticated access to screenshots
    return send_from_directory(SCREENSHOT_DIR, filename)



# ── Admin + Password Reset Blueprints ─────────────────────────────────────────

@app.errorhandler(403)
def forbidden(e):
    return render_template("403.html"), 403

from admin import admin_bp
app.register_blueprint(admin_bp)

from password_reset import reset_bp
app.register_blueprint(reset_bp)

# ════════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=config.DEBUG, port=5000)
