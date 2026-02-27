"""
admin.py — GuardianAI Admin Control Plane
Full Blueprint: user management, scan browser, audit log, stats, health, CSV exports.
All routes protected by @admin_required.
"""

import csv
import io
import json
import logging
import os
from datetime import datetime, timedelta, UTC

import redis as redis_lib
from flask import (
    Blueprint, abort, jsonify, redirect, render_template,
    request, url_for, Response, flash
)
from flask_login import current_user
from sqlalchemy import func, desc

from decorators import admin_required, write_audit_log
from models import db, User, TestRun, PageResult, AuditLog

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin", template_folder="templates")

_ALLOWED_PLANS = {"free", "pro", "enterprise"}

# ── Internal helpers ───────────────────────────────────────────────────────────

def _get_redis():
    """Returns a Redis connection using the same config as the main app."""
    try:
        import config
        r = redis_lib.Redis.from_url(config.REDIS_URL, socket_connect_timeout=2)
        r.ping()
        return r
    except Exception:
        return None


def _scan_counts_map() -> dict:
    """Returns {user_id: scan_count} for all users in one query."""
    rows = (
        db.session.query(TestRun.user_id, func.count(TestRun.id))
        .group_by(TestRun.user_id)
        .all()
    )
    return {uid: cnt for uid, cnt in rows}


def _safe_int(val, default: int = 1) -> int:
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return default


# ── Dashboard ──────────────────────────────────────────────────────────────────

@admin_bp.route("/")
@admin_required
def dashboard():
    total_users     = User.query.count()
    active_users    = User.query.filter_by(_is_active=True).count()
    suspended_users = User.query.filter_by(_is_active=False).count()
    admin_users     = User.query.filter_by(is_admin=True).count()

    total_scans = TestRun.query.count()

    week_ago = datetime.now(UTC) - timedelta(days=7)
    scans_7d = TestRun.query.filter(TestRun.started_at >= week_ago).count()

    avg_health = db.session.query(
        func.avg(TestRun.site_health_score)
    ).filter(TestRun.status == "completed").scalar()
    avg_health = round(avg_health, 1) if avg_health else 0.0

    plan_dist_rows = (
        db.session.query(User.plan, func.count(User.id))
        .group_by(User.plan)
        .all()
    )
    plan_distribution = {row[0]: row[1] for row in plan_dist_rows}

    # Recent activity — last 20 audit events
    recent_activity = (
        AuditLog.query
        .order_by(desc(AuditLog.created_at))
        .limit(20)
        .all()
    )

    # Scans per day for last 30 days — used by chart
    thirty_ago = datetime.now(UTC) - timedelta(days=30)
    daily_scans_rows = (
        db.session.query(
            func.date_trunc("day", TestRun.started_at).label("day"),
            func.count(TestRun.id).label("cnt")
        )
        .filter(TestRun.started_at >= thirty_ago)
        .group_by("day")
        .order_by("day")
        .all()
    )
    daily_labels = [r.day.strftime("%d %b") for r in daily_scans_rows]
    daily_counts = [r.cnt for r in daily_scans_rows]

    return render_template(
        "admin/dashboard.html",
        total_users=total_users,
        active_users=active_users,
        suspended_users=suspended_users,
        admin_users=admin_users,
        total_scans=total_scans,
        scans_7d=scans_7d,
        avg_health=avg_health,
        plan_distribution=plan_distribution,
        recent_activity=recent_activity,
        daily_labels=json.dumps(daily_labels),
        daily_counts=json.dumps(daily_counts),
    )


# ── Users List ─────────────────────────────────────────────────────────────────

@admin_bp.route("/users")
@admin_required
def users():
    page      = _safe_int(request.args.get("page", 1))
    per_page  = 25
    q_str     = request.args.get("q", "").strip()
    plan_f    = request.args.get("plan", "")
    status_f  = request.args.get("status", "")

    query = User.query

    if q_str:
        like = f"%{q_str}%"
        query = query.filter(
            db.or_(User.username.ilike(like), User.email.ilike(like))
        )
    if plan_f in _ALLOWED_PLANS:
        query = query.filter_by(plan=plan_f)
    if status_f == "active":
        query = query.filter_by(_is_active=True)
    elif status_f == "suspended":
        query = query.filter_by(_is_active=False)

    pagination = query.order_by(desc(User.created_at)).paginate(
        page=page, per_page=per_page, error_out=False
    )

    scan_counts = _scan_counts_map()

    return render_template(
        "admin/users.html",
        pagination=pagination,
        users=pagination.items,
        scan_counts=scan_counts,
        q=q_str,
        plan_filter=plan_f,
        status_filter=status_f,
    )


# ── User Detail ────────────────────────────────────────────────────────────────

@admin_bp.route("/users/<int:user_id>")
@admin_required
def user_detail(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)

    scan_count = TestRun.query.filter_by(user_id=user_id).count()
    total_pages_scanned = (
        db.session.query(func.sum(TestRun.total_tests))
        .filter_by(user_id=user_id)
        .scalar() or 0
    )
    avg_health = (
        db.session.query(func.avg(TestRun.site_health_score))
        .filter(TestRun.user_id == user_id, TestRun.status == "completed")
        .scalar()
    )
    avg_health = round(avg_health, 1) if avg_health else None

    recent_scans = (
        TestRun.query
        .filter_by(user_id=user_id)
        .order_by(desc(TestRun.started_at))
        .limit(20)
        .all()
    )

    user_logs = (
        AuditLog.query
        .filter_by(user_id=user_id)
        .order_by(desc(AuditLog.created_at))
        .limit(50)
        .all()
    )

    return render_template(
        "admin/user_detail.html",
        user=user,
        scan_count=scan_count,
        total_pages_scanned=total_pages_scanned,
        avg_health=avg_health,
        recent_scans=recent_scans,
        user_logs=user_logs,
        allowed_plans=sorted(_ALLOWED_PLANS),
    )


# ── User Actions (POST) ────────────────────────────────────────────────────────

@admin_bp.route("/users/<int:user_id>/suspend", methods=["POST"])
@admin_required
def suspend_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    if user.id == current_user.id:
        return jsonify({"error": "Cannot suspend yourself"}), 400

    user._is_active = False
    db.session.commit()

    write_audit_log(
        db, AuditLog,
        user_id=current_user.id,
        action="admin_suspend_user",
        extra_data={"target_user_id": user_id, "target_username": user.username},
    )
    return jsonify({"status": "suspended", "user_id": user_id})


@admin_bp.route("/users/<int:user_id>/activate", methods=["POST"])
@admin_required
def activate_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)

    user._is_active = True
    db.session.commit()

    write_audit_log(
        db, AuditLog,
        user_id=current_user.id,
        action="admin_activate_user",
        extra_data={"target_user_id": user_id, "target_username": user.username},
    )
    return jsonify({"status": "activated", "user_id": user_id})


@admin_bp.route("/users/<int:user_id>/make-admin", methods=["POST"])
@admin_required
def make_admin(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)

    user.is_admin = True
    db.session.commit()

    write_audit_log(
        db, AuditLog,
        user_id=current_user.id,
        action="admin_promote_user",
        extra_data={"target_user_id": user_id, "target_username": user.username},
    )
    return jsonify({"status": "promoted", "user_id": user_id})


@admin_bp.route("/users/<int:user_id>/remove-admin", methods=["POST"])
@admin_required
def remove_admin(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    if user.id == current_user.id:
        return jsonify({"error": "Cannot demote yourself"}), 400

    user.is_admin = False
    db.session.commit()

    write_audit_log(
        db, AuditLog,
        user_id=current_user.id,
        action="admin_demote_user",
        extra_data={"target_user_id": user_id, "target_username": user.username},
    )
    return jsonify({"status": "demoted", "user_id": user_id})


@admin_bp.route("/users/<int:user_id>/change-plan", methods=["POST"])
@admin_required
def change_plan(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)

    new_plan = request.form.get("plan", "").strip().lower()
    if new_plan not in _ALLOWED_PLANS:
        return jsonify({"error": f"Invalid plan: {new_plan}"}), 400

    old_plan = user.plan

    # Apply plan defaults
    _PLAN_DEFAULTS = {
        "free":       {"scan_limit": 5,    "page_limit_default": 50},
        "pro":        {"scan_limit": 50,   "page_limit_default": 500},
        "enterprise": {"scan_limit": 9999, "page_limit_default": 9999},
    }
    defaults = _PLAN_DEFAULTS[new_plan]
    user.plan               = new_plan
    user.scan_limit         = defaults["scan_limit"]
    user.page_limit_default = defaults["page_limit_default"]
    db.session.commit()

    write_audit_log(
        db, AuditLog,
        user_id=current_user.id,
        action="admin_change_plan",
        extra_data={
            "target_user_id":  user_id,
            "target_username": user.username,
            "old_plan":        old_plan,
            "new_plan":        new_plan,
        },
    )
    return jsonify({"status": "updated", "plan": new_plan, "user_id": user_id})


# ── Scans Browser ─────────────────────────────────────────────────────────────

@admin_bp.route("/scans")
@admin_required
def scans():
    page     = _safe_int(request.args.get("page", 1))
    per_page = 30
    status_f = request.args.get("status", "")
    user_f   = request.args.get("user_id", "")
    date_f   = request.args.get("date", "")  # YYYY-MM-DD

    query = TestRun.query

    if status_f in ("queued", "running", "completed", "failed"):
        query = query.filter(TestRun.status == status_f)
    if user_f:
        try:
            query = query.filter(TestRun.user_id == int(user_f))
        except ValueError:
            pass
    if date_f:
        try:
            d = datetime.strptime(date_f, "%Y-%m-%d")
            query = query.filter(
                TestRun.started_at >= d,
                TestRun.started_at < d + timedelta(days=1),
            )
        except ValueError:
            pass

    pagination = query.order_by(desc(TestRun.started_at)).paginate(
        page=page, per_page=per_page, error_out=False
    )

    # Attach usernames efficiently
    user_ids = {r.user_id for r in pagination.items}
    user_map = {
        u.id: u.username
        for u in User.query.filter(User.id.in_(user_ids)).all()
    } if user_ids else {}

    all_users = User.query.with_entities(User.id, User.username).order_by(User.username).all()

    return render_template(
        "admin/scans.html",
        pagination=pagination,
        runs=pagination.items,
        user_map=user_map,
        all_users=all_users,
        status_filter=status_f,
        user_filter=user_f,
        date_filter=date_f,
    )


# ── Activity Log ──────────────────────────────────────────────────────────────

@admin_bp.route("/activity")
@admin_required
def activity():
    page     = _safe_int(request.args.get("page", 1))
    per_page = 50
    user_f   = request.args.get("user_id", "")
    action_f = request.args.get("action", "")
    date_f   = request.args.get("date", "")

    query = AuditLog.query

    if user_f:
        try:
            query = query.filter(AuditLog.user_id == int(user_f))
        except ValueError:
            pass
    if action_f:
        query = query.filter(AuditLog.action.ilike(f"%{action_f}%"))
    if date_f:
        try:
            d = datetime.strptime(date_f, "%Y-%m-%d")
            query = query.filter(
                AuditLog.created_at >= d,
                AuditLog.created_at < d + timedelta(days=1),
            )
        except ValueError:
            pass

    pagination = query.order_by(desc(AuditLog.created_at)).paginate(
        page=page, per_page=per_page, error_out=False
    )

    user_ids = {log.user_id for log in pagination.items if log.user_id}
    user_map = {
        u.id: u.username
        for u in User.query.filter(User.id.in_(user_ids)).all()
    } if user_ids else {}

    all_users = User.query.with_entities(User.id, User.username).order_by(User.username).all()

    # Distinct action types for filter dropdown
    action_types = [
        row[0] for row in
        db.session.query(AuditLog.action).distinct().order_by(AuditLog.action).all()
    ]

    return render_template(
        "admin/activity.html",
        pagination=pagination,
        logs=pagination.items,
        user_map=user_map,
        all_users=all_users,
        action_types=action_types,
        user_filter=user_f,
        action_filter=action_f,
        date_filter=date_f,
    )


# ── Stats API (JSON — for Chart.js) ───────────────────────────────────────────

@admin_bp.route("/stats")
@admin_required
def stats():
    thirty_ago = datetime.now(UTC) - timedelta(days=30)

    # Scans per day
    daily_rows = (
        db.session.query(
            func.date_trunc("day", TestRun.started_at).label("day"),
            func.count(TestRun.id).label("cnt")
        )
        .filter(TestRun.started_at >= thirty_ago)
        .group_by("day")
        .order_by("day")
        .all()
    )

    # Plan distribution
    plan_rows = (
        db.session.query(User.plan, func.count(User.id))
        .group_by(User.plan)
        .all()
    )

    # Active vs suspended
    active_cnt    = User.query.filter_by(_is_active=True).count()
    suspended_cnt = User.query.filter_by(_is_active=False).count()

    # Health score trend (30d, completed scans)
    health_rows = (
        db.session.query(
            func.date_trunc("day", TestRun.started_at).label("day"),
            func.avg(TestRun.site_health_score).label("avg_h")
        )
        .filter(
            TestRun.started_at >= thirty_ago,
            TestRun.status == "completed",
            TestRun.site_health_score.isnot(None),
        )
        .group_by("day")
        .order_by("day")
        .all()
    )

    return jsonify({
        "scans_per_day": {
            "labels": [r.day.strftime("%d %b") for r in daily_rows],
            "data":   [r.cnt for r in daily_rows],
        },
        "plan_distribution": {
            "labels": [r[0] for r in plan_rows],
            "data":   [r[1] for r in plan_rows],
        },
        "user_status": {
            "active":    active_cnt,
            "suspended": suspended_cnt,
        },
        "health_trend": {
            "labels": [r.day.strftime("%d %b") for r in health_rows],
            "data":   [round(r.avg_h, 1) if r.avg_h else None for r in health_rows],
        },
    })


# ── Health Check ──────────────────────────────────────────────────────────────

@admin_bp.route("/health")
@admin_required
def health():
    import config

    # DB
    db_ok = False
    try:
        db.session.execute(db.text("SELECT 1"))
        db_ok = True
    except Exception:
        pass

    # Redis + RQ
    redis_ok     = False
    queue_depth  = None
    worker_count = None
    failed_count = None

    r = _get_redis()
    if r:
        redis_ok = True
        try:
            from rq import Queue as RQueue
            from rq.worker import Worker as RWorker
            q           = RQueue("default", connection=r)
            queue_depth = q.count
            failed_q    = RQueue("failed", connection=r)
            failed_count = failed_q.count
            workers      = RWorker.all(connection=r)
            worker_count = len(workers)
        except Exception:
            pass

    # Pending/running scans
    pending_scans = TestRun.query.filter(
        TestRun.status.in_(["queued", "running"])
    ).count()

    # App uptime estimate (process start time)
    uptime_str = "N/A"
    try:
        import psutil, os as _os
        proc = psutil.Process(_os.getpid())
        start = datetime.fromtimestamp(proc.create_time(), tz=UTC)
        delta = datetime.now(UTC) - start
        h, rem = divmod(int(delta.total_seconds()), 3600)
        m, s   = divmod(rem, 60)
        uptime_str = f"{h}h {m}m {s}s"
    except Exception:
        pass

    return render_template(
        "admin/health.html",
        db_ok=db_ok,
        redis_ok=redis_ok,
        queue_depth=queue_depth,
        worker_count=worker_count,
        failed_count=failed_count,
        pending_scans=pending_scans,
        uptime_str=uptime_str,
    )


# ── CSV Exports ────────────────────────────────────────────────────────────────

@admin_bp.route("/export/users")
@admin_required
def export_users():
    users_all = User.query.order_by(User.id).all()
    scan_counts = _scan_counts_map()

    output  = io.StringIO()
    writer  = csv.writer(output)
    writer.writerow([
        "id", "username", "email", "plan",
        "is_admin", "is_active",
        "scan_count", "created_at", "last_login_at"
    ])
    for u in users_all:
        writer.writerow([
            u.id,
            u.username,
            u.email or "",
            u.plan,
            u.is_admin,
            u._is_active,
            scan_counts.get(u.id, 0),
            u.created_at.isoformat() if u.created_at else "",
            u.last_login_at.isoformat() if u.last_login_at else "",
        ])

    output.seek(0)
    filename = f"guardian_users_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.csv"

    write_audit_log(
        db, AuditLog,
        user_id=current_user.id,
        action="admin_export_users",
        extra_data={"count": len(users_all)},
    )

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@admin_bp.route("/export/scans")
@admin_required
def export_scans():
    runs = TestRun.query.order_by(TestRun.id).all()

    user_ids = {r.user_id for r in runs}
    user_map = {
        u.id: u.username
        for u in User.query.filter(User.id.in_(user_ids)).all()
    } if user_ids else {}

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "user_id", "username", "target_url", "status",
        "started_at", "finished_at", "total_pages",
        "site_health_score", "risk_category", "confidence_score",
        "avg_performance", "avg_accessibility", "avg_security",
        "avg_functional", "avg_ui_form",
        "total_broken_links", "total_js_errors", "total_a11y_issues",
    ])
    for r in runs:
        writer.writerow([
            r.id,
            r.user_id,
            user_map.get(r.user_id, ""),
            r.target_url,
            r.status,
            r.started_at.isoformat() if r.started_at else "",
            r.finished_at.isoformat() if r.finished_at else "",
            r.total_tests,
            r.site_health_score,
            r.risk_category,
            r.confidence_score,
            r.avg_performance_score,
            r.avg_accessibility_score,
            r.avg_security_score,
            r.avg_functional_score,
            r.avg_ui_form_score,
            r.total_broken_links,
            r.total_js_errors,
            r.total_accessibility_issues,
        ])

    output.seek(0)
    filename = f"guardian_scans_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.csv"

    write_audit_log(
        db, AuditLog,
        user_id=current_user.id,
        action="admin_export_scans",
        extra_data={"count": len(runs)},
    )

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
