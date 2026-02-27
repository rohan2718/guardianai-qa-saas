"""
decorators.py — GuardianAI
Shared decorators and audit logging utility.
Import into app.py and admin.py.
"""

import logging
from functools import wraps
from datetime import datetime, UTC

from flask import abort, request
from flask_login import login_required, current_user

logger = logging.getLogger(__name__)


def admin_required(f):
    """
    Decorator that requires the current user to be authenticated AND is_admin=True.
    Returns 403 for authenticated non-admins.
    Redirects to login for unauthenticated users (via @login_required chain).
    """
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated_function


def write_audit_log(db, AuditLog, user_id, action: str, extra_data: dict = None, ip: str = None):
    """
    Writes a single row to audit_logs. Safe — swallows exceptions to
    never break the calling request flow.

    Args:
        db:        SQLAlchemy db instance
        AuditLog:  AuditLog model class
        user_id:   Integer user id (may be None for anonymous actions)
        action:    Short string like "login", "scan_started", "plan_changed"
        metadata:  Dict of additional context (stored as JSONB)
        ip:        IP address string; auto-detected from request if None
    """
    try:
        resolved_ip = ip or (request.remote_addr if request else None)
        log = AuditLog(
            user_id    = user_id,
            action     = action,
            extra_data = extra_data or {},
            ip_address = resolved_ip,
            created_at = datetime.now(UTC),
        )
        db.session.add(log)
        db.session.commit()
    except Exception as e:
        logger.warning(f"AuditLog write failed [{action}]: {e}")
        try:
            db.session.rollback()
        except Exception:
            pass
