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
import ipaddress
import socket
import pyotp
import qrcode
import io
from urllib.parse import urlparse
import markdown
import pandas as pd
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

# Job queues
task_queue       = Queue("default", connection=redis_conn)
task_queue_quick = Queue("quick", connection=redis_conn)


# ── Scan Concurrency Guard ───────────────────────────────

_SCAN_CONCURRENCY_KEY = "guardianai:scan_semaphore"
_MAX_CONCURRENT_SCANS = int(os.environ.get("MAX_CONCURRENT_SCANS", "4"))

def _acquire_scan_slot() -> bool:
    try:
        current = redis_conn.incr(_SCAN_CONCURRENCY_KEY)
        if current > _MAX_CONCURRENT_SCANS:
            redis_conn.decr(_SCAN_CONCURRENCY_KEY)
            return False

        redis_conn.expire(_SCAN_CONCURRENCY_KEY, 3600)
        return True
    except Exception:
        return True


def _release_scan_slot():
    try:
        val = redis_conn.decr(_SCAN_CONCURRENCY_KEY)
        if val < 0:
            redis_conn.set(_SCAN_CONCURRENCY_KEY, 0)
    except Exception:
        pass
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



def is_safe_scan_url(url: str) -> tuple[bool, str]:
    """
    Validates a user-submitted scan URL against SSRF attack vectors.
    Returns (is_safe: bool, reason: str).

    Blocks:
      - Non-http/https schemes (file://, ftp://, etc.)
      - Private RFC1918 IP ranges (10.x, 172.16-31.x, 192.168.x)
      - Loopback (127.x, ::1)
      - Link-local (169.254.x — AWS metadata endpoint)
      - Unspecified / broadcast addresses
    """
    if not url:
        return False, "URL is required."

    # Force scheme
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Invalid URL format."

    if parsed.scheme not in ("http", "https"):
        return False, "Only http and https URLs are allowed."

    hostname = parsed.hostname
    if not hostname:
        return False, "URL must include a hostname."

    # Resolve hostname to IP (catches DNS-rebinding to some extent)
    try:
        resolved_ip = socket.gethostbyname(hostname)
        ip_obj = ipaddress.ip_address(resolved_ip)
    except socket.gaierror:
        return False, f"Cannot resolve hostname: {hostname}"
    except ValueError:
        return False, "Invalid IP address resolved."

    if ip_obj.is_loopback:
        return False, "Scanning loopback addresses is not allowed."
    if ip_obj.is_private:
        return False, "Scanning private network addresses is not allowed."
    if ip_obj.is_link_local:
        return False, "Scanning link-local addresses is not allowed (AWS metadata endpoint blocked)."
    if ip_obj.is_unspecified:
        return False, "Invalid target IP."
    if ip_obj.is_reserved:
        return False, "Scanning reserved IP ranges is not allowed."

    return True, ""
# ── Run context builder ────────────────────────────────────────────────────────
def enrich_run_context(run: TestRun) -> dict:
    """
    Builds template context for a completed run.

    DATA SOURCES (in priority order):
      1. raw_data   → PageResult DB rows (DB-first, no file I/O)
                      Falls back to raw JSON file only if no DB rows exist
                      (handles scans completed before this fix was deployed).
      2. ai_insight → TestRun.ai_summary_html column (DB-first)
                      Falls back to summary_file if column is empty.
      3. metrics    → generate_metrics_from_run() — always from DB columns.
      4. site_health→ Reconstructed from TestRun columns — no JSON file needed.
    """
    raw_data    = []
    ai_insight  = None
    site_health = None

    metrics = generate_metrics_from_run(run) if run.status == "completed" else {}

    # ── 1. raw_data: DB-first via PageResult, augmented with modal detail ───────
    db_rows = PageResult.query.filter_by(run_id=run.id).order_by(PageResult.id.asc()).all()

    if db_rows:
        # Build base raw_data from DB rows (all score/metric fields)
        raw_data = [
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
                "load_time": r.load_time if r.load_time is not None else None,  # leave as-is
                "fcp_ms":                  r.fcp_ms,
                "lcp_ms":                  r.lcp_ms,
                "ttfb_ms":                 r.ttfb_ms,
                "accessibility_issues":    r.accessibility_issues,
                # Keep full relative path — template uses src="/{{ item.screenshot }}"
                # which needs "screenshots/filename.png", NOT just "filename.png"
                "screenshot":              r.screenshot_path or None,
                "ui_summary":              r.ui_summary or {},
                "result":                  "pass" if (r.status or 200) < 400 else "fail",
                # Counts from DB for page-row badges
                "broken_navigation_links": [None] * (r.broken_links_count or 0),
                "js_errors":               [None] * (r.js_errors_count or 0),
                "is_https":                r.is_https,
                # Deep Inspection modal fields — empty until augmented from raw JSON
                "ui_elements":    [],
                "forms":          [],
                "broken_links":   [],
                "connected_pages": [],
                "dom_latency":    None,
                "failure_pattern": r.failure_pattern_id,
                "viewport":       "Desktop",
            }
            for r in db_rows
        ]

        # ── Augment modal-only fields from raw JSON file ──────────────────────
        # ui_elements, forms, broken_links (with url+status objects), connected_pages
        # are NOT stored in PageResult. Load once, merge by URL.
        if run.raw_file and os.path.exists(run.raw_file):
            try:
                with open(run.raw_file, "r", encoding="utf-8") as fh:
                    raw_pages = json.load(fh)
                rich_by_url = {
                    rp.get("url"): rp
                    for rp in (raw_pages if isinstance(raw_pages, list) else [])
                    if rp.get("url")
                }
                for row in raw_data:
                    rp = rich_by_url.get(row["url"])
                    if rp:
                        row["ui_elements"]     = rp.get("ui_elements")  or []
                        row["forms"]           = rp.get("forms")        or []
                        # Template uses item.broken_links as list of {url, status} objects
                        raw_bnl = rp.get("broken_navigation_links") or rp.get("broken_links") or []
                        row["broken_links"]    = [
                            {"url": lnk, "status": 404} if isinstance(lnk, str) else lnk
                            for lnk in raw_bnl
                        ]
                        row["connected_pages"] = rp.get("connected_pages") or []
                        row["dom_latency"]     = rp.get("dom_latency") or rp.get("load_time")
                        row["viewport"]        = rp.get("viewport") or "Desktop"
                        # If screenshot missing from DB row, fill from raw JSON
                        if not row["screenshot"]:
                            row["screenshot"]  = rp.get("screenshot")
            except Exception as exc:
                logger.warning("enrich_run_context: modal augment failed %s — %s", run.raw_file, exc)

    else:
        # Fallback: everything from raw JSON (legacy scans before DB patch)
        if run.raw_file and os.path.exists(run.raw_file):
            try:
                with open(run.raw_file, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                raw_data = loaded if isinstance(loaded, list) else []
            except Exception as exc:
                logger.warning("enrich_run_context: fallback raw file load failed %s — %s", run.raw_file, exc)

    # ── 2. AI insight: DB column first ───────────────────────────────────────
    if run.ai_summary_html:
        ai_insight = run.ai_summary_html  # already HTML from tasks.py FIX4
    elif run.ai_summary:
        ai_insight = markdown.markdown(run.ai_summary)
    else:
        # Fallback to file for legacy scans
        if run.summary_file and os.path.exists(run.summary_file):
            try:
                with open(run.summary_file, "r", encoding="utf-8") as fh:
                    raw_txt = fh.read()
                if raw_txt:
                    ai_insight = markdown.markdown(raw_txt)
                    # Backfill DB so next load is instant
                    run.ai_summary      = raw_txt
                    run.ai_summary_html = ai_insight
                    db.session.commit()
            except Exception as exc:
                logger.warning("enrich_run_context: fallback summary file load failed %s — %s", run.summary_file, exc)

    # ── 3. Site health: reconstruct from TestRun columns (no file I/O) ───────
    if run.site_health_score is not None:
        site_health = {
            "site_health_score": run.site_health_score,
            "risk_category":     run.risk_category,
            "confidence_score":  run.confidence_score,
            "component_averages": {
                "performance":   run.avg_performance_score,
                "accessibility": run.avg_accessibility_score,
                "security":      run.avg_security_score,
                "functional":    run.avg_functional_score,
                "ui_form":       run.avg_ui_form_score,
            },
        }

    active_filters = run.scan_filters or []
    return {
        "data":             [],           # legacy field — raw_data is the live source
        "ai_insight":       ai_insight,
        "metrics":          metrics,
        "raw_data":         raw_data,
        "site_health":      site_health,
        "current_run":      run,
        "active_filters":   active_filters,
        "scan_filter_defs": SCAN_FILTER_DEFS,
        "filter_icons":     FILTER_ICONS,
        "intel":            {},
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
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not username or not password:
            return render_template("register.html", error="Username and password are required.")

        if not email or "@" not in email:
            return render_template("register.html", error="A valid email address is required.")

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


import pyotp  # add to imports at top

@app.route("/login", methods=["GET", "POST"])
@limiter.limit("20 per minute")
def login():
    reset_success = request.args.get("reset_success")

    if request.method == "POST":
        # ── Step 2: TOTP verification (2FA users) ──────────────────────────
        if "2fa_user_id" in session:
            user = db.session.get(User, session["2fa_user_id"])
            if not user:
                session.pop("2fa_user_id", None)
                return render_template("login.html", error="Session expired. Please log in again.")

            otp = request.form.get("otp", "").strip()
            totp = pyotp.TOTP(user.otp_secret)

            if not totp.verify(otp, valid_window=1):
                write_audit_log(db, AuditLog, user_id=user.id, action="2fa_failed",
                                extra_data={"username": user.username})
                return render_template("login.html", show_otp=True,
                                       error="Invalid or expired code. Try again.")

            # ✅ 2FA passed
            session.pop("2fa_user_id", None)
            _delete_qr(user.id)
            user.last_login_at = datetime.now(UTC)
            db.session.commit()
            login_user(user)

            write_audit_log(db, AuditLog, user_id=user.id, action="login_success_2fa",
                            extra_data={"username": user.username})

            next_page = request.args.get("next") or ("/" if user.email else "/add-email?next=/")
            return redirect(next_page)

        # ── Step 1: Username + password ────────────────────────────────────
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user     = User.query.filter_by(username=username).first()

        if user and not user._is_active:
            write_audit_log(db, AuditLog, user_id=user.id, action="login_suspended",
                            extra_data={"username": username})
            return render_template("login.html", error="Invalid credentials")

        if not user or not check_password_hash(user.password, password):
            write_audit_log(db, AuditLog, user_id=user.id if user else None,
                            action="login_failed", extra_data={"username": username})
            return render_template("login.html", error="Invalid credentials")

        # ── 2FA required? ──────────────────────────────────────────────────
        if user.is_2fa_enabled and user.otp_secret:
            session["2fa_user_id"] = user.id

            # Generate and cache QR only on first scan (new setup)
            # For returning users, no QR needed — they already have the app configured
            qr_b64 = _get_qr(user.id)   # will be None for existing 2FA users

            write_audit_log(db, AuditLog, user_id=user.id, action="login_2fa_challenged",
                            extra_data={"username": user.username})
            return render_template("login.html", show_otp=True, qr=qr_b64)

        # ── No 2FA — direct login ──────────────────────────────────────────
        user.last_login_at = datetime.now(UTC)
        db.session.commit()
        login_user(user)

        write_audit_log(db, AuditLog, user_id=user.id, action="login_success",
                        extra_data={"username": user.username})

        next_page = request.args.get("next") or ("/" if user.email else "/add-email?next=/")
        return redirect(next_page)

    # ── GET: if mid-2FA session exists, show OTP screen ───────────────────
    if "2fa_user_id" in session:
        return render_template("login.html", show_otp=True)

    return render_template("login.html", reset_success=reset_success)

# ════════════════════════════════════════════════════════════════════════════════
# PROFILE & 2FA MANAGEMENT
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/profile")
@login_required
def profile():
    two_fa_enabled  = request.args.get("2fa_enabled")
    two_fa_disabled = request.args.get("2fa_disabled")
    return render_template(
        "profile.html",
        two_fa_enabled  = two_fa_enabled,
        two_fa_disabled = two_fa_disabled,
        plan_limits     = _plan_limits(current_user),
        history         = load_history_from_db(),   # ← ADD THIS LINE
    )
@app.route("/setup-2fa", methods=["GET", "POST"])
@login_required
def setup_2fa():
    import pyotp, qrcode, io

    if request.method == "POST":
        otp    = request.form.get("otp", "").strip()
        secret = session.get("pending_2fa_secret")

        if not secret:
            return redirect("/setup-2fa")

        totp = pyotp.TOTP(secret)
        if not totp.verify(otp, valid_window=1):
            write_audit_log(db, AuditLog, user_id=current_user.id,
                            action="2fa_setup_failed", extra_data={})
            return render_template(
                "setup_2fa.html",
                error="Code didn't match. Please scan the QR again and retry.",
                qr=_get_qr(current_user.id),
            )

        # ✅ Confirmed — persist to DB, clear temp state
        current_user.otp_secret     = secret
        current_user.is_2fa_enabled = True
        db.session.commit()
        session.pop("pending_2fa_secret", None)
        _delete_qr(current_user.id)

        write_audit_log(db, AuditLog, user_id=current_user.id,
                        action="2fa_enabled", extra_data={})
        return redirect("/profile?2fa=enabled")

    # GET — generate fresh secret + QR every time this page loads
    secret = pyotp.random_base32()
    session["pending_2fa_secret"] = secret

    uri = pyotp.TOTP(secret).provisioning_uri(
        name=current_user.username,
        issuer_name="Guardian AI",
    )

    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()
    _store_qr(current_user.id, qr_b64)  # TTL=300s in Redis

    return render_template("setup_2fa.html", qr=qr_b64)


@app.route("/disable-2fa", methods=["POST"])
@login_required
def disable_2fa():
    current_user.is_2fa_enabled = False
    current_user.otp_secret     = None
    db.session.commit()
    _delete_qr(current_user.id)
    session.pop("pending_2fa_secret", None)

    write_audit_log(db, AuditLog, user_id=current_user.id,
                    action="2fa_disabled", extra_data={})
    return redirect("/profile?2fa=disabled")
@app.route("/logout")
@login_required
def logout():
    logout_user()
    session.pop("2fa_user_id", None)
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

        # ── Validate URL presence ───────────────────────────────────────────
        if not url:
            ctx = _empty_ctx(error="Please enter a URL.")
            return render_template("index.html", **ctx)

        # ── SSRF Protection ─────────────────────────────────────────────────
        safe, reason = is_safe_scan_url(url)
        if not safe:
            ctx = _empty_ctx(error=f"Invalid URL: {reason}")
            return render_template("index.html", **ctx)

        # ── Resolve page_limit safely ───────────────────────────────────────
        try:
            page_limit = int(request.form.get("page_limit") or current_user.page_limit_default)
        except (TypeError, ValueError):
            page_limit = int(current_user.page_limit_default)

        # ── Filters ─────────────────────────────────────────────────────────
        selected_filters = request.form.getlist("scan_filters")
        active_filters   = [f for f in selected_filters if f in VALID_FILTER_KEYS]
        if not active_filters:
            active_filters = list(VALID_FILTER_KEYS)

        # ── Plan limit enforcement ──────────────────────────────────────────
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

        # ── Concurrency guard (MOVE BEFORE DB INSERT) ──────────────────────
        if not _acquire_scan_slot():
            ctx = _empty_ctx(
                error="Scan capacity reached. Please try again shortly.",
                active_filters=active_filters,
            )
            return render_template("index.html", **ctx)

        # ── Create TestRun record ───────────────────────────────────────────
        run = TestRun(
            target_url=url,
            status="queued",
            started_at=datetime.now(UTC),
            user_id=current_user.id,
            progress=0,
            scan_filters=active_filters,  # Native JSONB list
        )

        db.session.add(run)
        db.session.commit()

        # ── Queue Routing (quick vs default) ───────────────────────────────
        _queue = task_queue_quick if (page_limit or 999) <= 5 else task_queue

        _queue.enqueue(
            run_scan,
            run.id,
            url,
            page_limit,
            current_user.id,
            active_filters,
            job_timeout=config.JOB_TIMEOUT,
            retry=Retry(max=1, interval=60),
        )

        return redirect(f"/run/{run.id}")

    # ── GET: Show most recent run ───────────────────────────────────────────
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
        ctx.update({
            "data": [],
            "ai_insight": None,
            "metrics": {},
            "raw_data": [],
            "site_health": None,
            "current_run": None,
            "intel": {},
        })

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
            "screenshot": os.path.basename(r.screenshot_path) if r.screenshot_path else None,
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

@app.route("/api/run/<int:run_id>/bugs")
@login_required
@csrf.exempt
def api_run_bugs(run_id):
    from models_qa import BugReport
    run = db.session.get(TestRun, run_id)
    if not run or run.user_id != current_user.id:
        abort(403)
    severity = request.args.get("severity")
    q = BugReport.query.filter_by(run_id=run_id)
    if severity:
        q = q.filter_by(severity=severity)
    bugs = [b.to_dict() for b in q.order_by(BugReport.id.asc()).all()]
    return jsonify({"bugs": bugs, "total": len(bugs)})


@app.route("/api/run/<int:run_id>/flows")
@login_required
@csrf.exempt
def api_run_flows(run_id):
    from models_qa import QAFlow
    run = db.session.get(TestRun, run_id)
    if not run or run.user_id != current_user.id:
        abort(403)
    flows = [f.to_dict() for f in QAFlow.query.filter_by(run_id=run_id).all()]
    return jsonify({"flows": flows, "total": len(flows)})


@app.route("/api/run/<int:run_id>/test-cases")
@login_required
@csrf.exempt
def api_run_test_cases(run_id):
    from models_qa import QATestCase
    run = db.session.get(TestRun, run_id)
    if not run or run.user_id != current_user.id:
        abort(403)
    cases = [tc.to_dict() for tc in QATestCase.query.filter_by(run_id=run_id).all()]
    return jsonify({"test_cases": cases, "total": len(cases)})
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
            "broken_links":         len(p.get("broken_navigation_links") or []),
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
    safe_name = os.path.basename(filename)
    return send_from_directory(SCREENSHOT_DIR, safe_name)


# ── Admin + Password Reset Blueprints ─────────────────────────────────────────

@app.errorhandler(403)
def forbidden(e):
    return render_template("403.html"), 403

from admin import admin_bp
app.register_blueprint(admin_bp)

from password_reset import reset_bp
app.register_blueprint(reset_bp)

# Rate-limit the reset request endpoints to prevent email flooding

# ════════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=config.DEBUG, port=5000)