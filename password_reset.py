"""
password_reset.py — GuardianAI
Full password reset flow: token generation, email stub, form handling, audit logging.
Register as a Blueprint in app.py.
"""

import logging
import os
import secrets
from datetime import datetime, timedelta, UTC

from flask import (
    Blueprint, redirect, render_template,
    request, url_for
)
from werkzeug.security import generate_password_hash

from decorators import write_audit_log
from models import db, User, AuditLog, PasswordResetToken

logger = logging.getLogger(__name__)

reset_bp = Blueprint("reset", __name__, template_folder="templates")

TOKEN_EXPIRY_HOURS = 1


# ── Email stub ─────────────────────────────────────────────────────────────────

def _send_reset_email(to_address: str, reset_link: str):
    """
    Email sending stub. Replace the body of this function with your
    transactional email provider (SendGrid, Postmark, AWS SES, etc).

    For local development, the link is logged to stdout.
    Set SMTP_HOST / SENDGRID_API_KEY in .env to enable real sending.
    """
    logger.info(f"[PASSWORD RESET] To: {to_address}  Link: {reset_link}")

    smtp_host = os.environ.get("SMTP_HOST")
    if not smtp_host:
        # No SMTP configured — link is only in the logs.
        return

    try:
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        smtp_port = int(os.environ.get("SMTP_PORT", 587))
        smtp_user = os.environ.get("SMTP_USER", "")
        smtp_pass = os.environ.get("SMTP_PASS", "")
        from_addr = os.environ.get("SMTP_FROM", smtp_user)

        msg              = MIMEMultipart("alternative")
        msg["Subject"]   = "Guardian AI — Password Reset"
        msg["From"]      = from_addr
        msg["To"]        = to_address

        text_body = (
            f"You requested a password reset for your Guardian AI account.\n\n"
            f"Click the link below to reset your password (valid for {TOKEN_EXPIRY_HOURS} hour):\n\n"
            f"{reset_link}\n\n"
            f"If you did not request this, ignore this email. Your password will not change."
        )
        html_body = f"""
        <html><body style="font-family:sans-serif;max-width:600px;margin:auto;padding:24px;">
          <h2 style="color:#6366f1">Guardian AI — Password Reset</h2>
          <p>You requested a password reset for your account.</p>
          <p>
            <a href="{reset_link}"
               style="background:#6366f1;color:white;padding:12px 24px;
                      border-radius:8px;text-decoration:none;display:inline-block;">
              Reset My Password
            </a>
          </p>
          <p style="color:#64748b;font-size:13px;">
            This link expires in {TOKEN_EXPIRY_HOURS} hour. If you did not request a reset,
            ignore this email.
          </p>
        </body></html>
        """

        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body,  "html"))

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls()
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.sendmail(from_addr, to_address, msg.as_string())

    except Exception as e:
        logger.error(f"Failed to send reset email to {to_address}: {e}")


# ── Request reset ──────────────────────────────────────────────────────────────

@reset_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("reset/forgot_password.html")

    email = request.form.get("email", "").strip().lower()

    # Always show success to prevent user enumeration
    success_msg = "If an account with that email exists, a reset link has been sent."

    if not email:
        return render_template("reset/forgot_password.html", success=success_msg)

    user = User.query.filter(
        db.func.lower(User.email) == email
    ).first()

    if user and user._is_active:
        # Invalidate any existing unused tokens for this user
        PasswordResetToken.query.filter_by(
            user_id=user.id, used=False
        ).delete()
        db.session.flush()

        token_value = secrets.token_urlsafe(64)
        expires_at  = datetime.now(UTC) + timedelta(hours=TOKEN_EXPIRY_HOURS)

        prt = PasswordResetToken(
            user_id    = user.id,
            token      = token_value,
            expires_at = expires_at,
            used       = False,
        )
        db.session.add(prt)
        db.session.commit()

        reset_link = url_for("reset.reset_password", token=token_value, _external=True)
        _send_reset_email(user.email, reset_link)

        write_audit_log(
            db, AuditLog,
            user_id=user.id,
            action="password_reset_requested",
            metadata={"email": email},
        )

    return render_template("reset/forgot_password.html", success=success_msg)


# ── Perform reset ─────────────────────────────────────────────────────────────

@reset_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    prt = PasswordResetToken.query.filter_by(token=token, used=False).first()

    if not prt or prt.expires_at < datetime.now(UTC):
        return render_template(
            "reset/reset_password.html",
            error="This reset link is invalid or has expired.",
            token=token,
            invalid=True,
        )

    if request.method == "GET":
        return render_template("reset/reset_password.html", token=token)

    new_password = request.form.get("password", "")
    confirm      = request.form.get("confirm_password", "")

    if len(new_password) < 8:
        return render_template(
            "reset/reset_password.html",
            token=token,
            error="Password must be at least 8 characters.",
        )
    if new_password != confirm:
        return render_template(
            "reset/reset_password.html",
            token=token,
            error="Passwords do not match.",
        )

    user = db.session.get(User, prt.user_id)
    if not user:
        return render_template(
            "reset/reset_password.html",
            error="Account not found.",
            token=token,
            invalid=True,
        )

    user.password = generate_password_hash(new_password)
    prt.used      = True
    db.session.commit()

    write_audit_log(
        db, AuditLog,
        user_id=user.id,
        action="password_reset_completed",
        metadata={"username": user.username},
    )

    return redirect(url_for("login", reset_success=1))
