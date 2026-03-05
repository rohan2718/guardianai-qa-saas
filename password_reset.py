"""
password_reset.py — GuardianAI
Dual-mode password reset: 6-digit OTP (primary) + magic link (fallback).

Flow:
  1. /forgot-password          → user enters email
  2. /verify-otp               → user enters 6-digit OTP from email
     /reset-password/<token>   → fallback magic link from email
  3. /reset-password/<token>   → user sets new password (reached via OTP verify OR magic link)

Extra route:
  /add-email                   → logged-in users with no email must set one here

Register in app.py:
  from password_reset import reset_bp
  app.register_blueprint(reset_bp)
"""

import logging
import random
import secrets
import smtplib
from datetime import datetime, timedelta, UTC
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import config
from decorators import write_audit_log
from flask import (
    Blueprint, redirect, render_template,
    request, session, url_for
)
from flask_login import current_user, login_required
from models import db, User, AuditLog, PasswordResetToken
from werkzeug.security import generate_password_hash

logger = logging.getLogger(__name__)

reset_bp = Blueprint("reset", __name__, template_folder="templates")

OTP_EXPIRY_MINUTES  = 10
LINK_EXPIRY_MINUTES = 30   # magic link lives longer than OTP


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL SENDER
# ══════════════════════════════════════════════════════════════════════════════

def _send_reset_email(to_address: str, otp_code: str, magic_link: str):
    """
    Send OTP + magic link fallback to the user.
    If SMTP_HOST is not configured, both are printed to the server log only
    (safe for local development — never silently fails).
    """
    logger.info(
        f"[PASSWORD RESET] To={to_address} | OTP={otp_code} | Link={magic_link}"
    )

    if not config.SMTP_HOST:
        logger.warning(
            "[PASSWORD RESET] SMTP_HOST not set — email not sent. "
            "Check server logs for the OTP and link above."
        )
        return

    try:
        msg            = MIMEMultipart("alternative")
        msg["Subject"] = "Guardian AI — Your Password Reset Code"
        msg["From"]    = config.SMTP_FROM or config.SMTP_USER
        msg["To"]      = to_address

        text_body = (
            f"Your Guardian AI password reset code is:\n\n"
            f"  {otp_code}\n\n"
            f"This code expires in {OTP_EXPIRY_MINUTES} minutes.\n\n"
            f"Alternatively, click this link to reset directly "
            f"(valid for {LINK_EXPIRY_MINUTES} minutes):\n{magic_link}\n\n"
            f"If you did not request this, ignore this email. "
            f"Your password will not change."
        )

        html_body = f"""<!DOCTYPE html>
<html>
<body style="font-family:'Helvetica Neue',Arial,sans-serif;background:#f0f4f8;
             margin:0;padding:0;">
  <div style="max-width:560px;margin:40px auto;background:#fff;border-radius:16px;
              overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">

    <!-- Header -->
    <div style="background:linear-gradient(135deg,#6366f1,#8b5cf6);
                padding:32px 40px;text-align:center;">
      <div style="font-size:32px;margin-bottom:8px;">🛡️</div>
      <h1 style="color:white;margin:0;font-size:22px;font-weight:700;
                 letter-spacing:-0.3px;">Password Reset</h1>
      <p style="color:rgba(255,255,255,0.8);margin:6px 0 0;font-size:14px;">
        Guardian AI Security
      </p>
    </div>

    <!-- Body -->
    <div style="padding:36px 40px;">
      <p style="color:#0f172a;font-size:15px;margin:0 0 24px;">
        Enter this code on the Guardian AI password reset page:
      </p>

      <!-- OTP box -->
      <div style="background:#f8fafc;border:2px dashed #c7d2fe;border-radius:12px;
                  text-align:center;padding:28px 20px;margin:0 0 28px;">
        <div style="font-size:48px;font-weight:800;letter-spacing:14px;
                    color:#0f172a;font-family:'Courier New',monospace;">
          {otp_code}
        </div>
        <p style="color:#64748b;font-size:13px;margin:12px 0 0;">
          Expires in <strong>{OTP_EXPIRY_MINUTES} minutes</strong>
        </p>
      </div>

      <!-- Divider -->
      <div style="display:flex;align-items:center;margin:0 0 24px;gap:12px;">
        <div style="flex:1;height:1px;background:#e2e8f0;"></div>
        <span style="color:#94a3b8;font-size:12px;white-space:nowrap;">
          OR USE MAGIC LINK
        </span>
        <div style="flex:1;height:1px;background:#e2e8f0;"></div>
      </div>

      <!-- Magic link button -->
      <div style="text-align:center;margin:0 0 28px;">
        <a href="{magic_link}"
           style="display:inline-block;background:linear-gradient(135deg,#6366f1,#8b5cf6);
                  color:white;padding:14px 32px;border-radius:10px;
                  text-decoration:none;font-weight:600;font-size:15px;
                  box-shadow:0 4px 14px rgba(99,102,241,0.3);">
          Reset via Magic Link →
        </a>
        <p style="color:#94a3b8;font-size:12px;margin:10px 0 0;">
          Valid for {LINK_EXPIRY_MINUTES} minutes
        </p>
      </div>

      <p style="color:#64748b;font-size:13px;line-height:1.6;
                border-top:1px solid #f1f5f9;padding-top:20px;margin:0;">
        If you did not request a password reset, you can safely ignore this email.
        Your password will not change unless you complete the process.
      </p>
    </div>

  </div>
</body>
</html>"""

        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            if config.SMTP_USER and config.SMTP_PASS:
                server.login(config.SMTP_USER, config.SMTP_PASS)
            server.sendmail(
                config.SMTP_FROM or config.SMTP_USER,
                to_address,
                msg.as_string()
            )
        logger.info(f"[PASSWORD RESET] Email sent to {to_address}")

    except Exception as e:
        import traceback
        logger.error(f"[PASSWORD RESET] Failed to send email to {to_address}: {e}")
        logger.error(traceback.format_exc())


# ══════════════════════════════════════════════════════════════════════════════
# HELPER — create token row with OTP + magic token
# ══════════════════════════════════════════════════════════════════════════════

def _create_reset_tokens(user_id: int):
    """
    Invalidate all existing unused tokens for this user, then create a new row
    containing both:
      - otp_code    : 6-digit string (primary, shown on screen)
      - token       : URL-safe 64-byte string (magic link fallback)
    Returns the new PasswordResetToken instance (not yet committed).
    """
    PasswordResetToken.query.filter_by(user_id=user_id, used=False).delete()
    db.session.flush()

    otp_code    = f"{random.SystemRandom().randint(0, 999999):06d}"
    magic_token = secrets.token_urlsafe(64)
    expires_at  = datetime.now(UTC) + timedelta(minutes=LINK_EXPIRY_MINUTES)

    prt = PasswordResetToken(
        user_id    = user_id,
        token      = magic_token,   # magic link token stored here
        otp_code   = otp_code,      # OTP stored in new column
        expires_at = expires_at,
        used       = False,
    )
    db.session.add(prt)
    return prt, otp_code, magic_token


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — Enter email
# ══════════════════════════════════════════════════════════════════════════════

@reset_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("reset/forgot_password.html")

    email = request.form.get("email", "").strip().lower()

    # Always show the same message to prevent user enumeration
    success_msg = (
        "If an account with that email exists, we've sent a 6-digit reset code. "
        "Check your inbox (and spam folder)."
    )

    if not email:
        return render_template("reset/forgot_password.html", success=success_msg)

    user = User.query.filter(db.func.lower(User.email) == email).first()

    if user and user._is_active:
        prt, otp_code, magic_token = _create_reset_tokens(user.id)
        db.session.commit()

        magic_link = url_for("reset.reset_password", token=magic_token, _external=True)
        _send_reset_email(user.email, otp_code, magic_link)

        # Store user_id in session so verify-otp knows who to verify
        # (never store the OTP itself in the session cookie)
        session["reset_user_id"] = user.id

        write_audit_log(
            db, AuditLog,
            user_id    = user.id,
            action     = "password_reset_requested",
            extra_data = {"email": email},
        )

    return render_template("reset/forgot_password.html", success=success_msg)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2a — Enter OTP (primary path)
# ══════════════════════════════════════════════════════════════════════════════

@reset_bp.route("/verify-otp", methods=["GET", "POST"])
def verify_otp():
    user_id = session.get("reset_user_id")
    if not user_id:
        # No active reset session — send back to start
        return redirect(url_for("reset.forgot_password"))

    if request.method == "GET":
        return render_template("reset/verify_otp.html")

    entered_otp = request.form.get("otp", "").strip()

    # Look up the latest unused, unexpired token for this user
    prt = (
        PasswordResetToken.query
        .filter_by(user_id=user_id, used=False)
        .order_by(PasswordResetToken.created_at.desc())
        .first()
    )

    if not prt or prt.expires_at < datetime.now(UTC):
        session.pop("reset_user_id", None)
        return render_template(
            "reset/verify_otp.html",
            error="Your reset code has expired. Please request a new one.",
            expired=True,
        )

    # OTP expires faster than the magic link
    otp_cutoff = prt.created_at + timedelta(minutes=OTP_EXPIRY_MINUTES)
    if datetime.now(UTC) > otp_cutoff:
        session.pop("reset_user_id", None)
        return render_template(
            "reset/verify_otp.html",
            error=f"Your {OTP_EXPIRY_MINUTES}-minute code has expired. "
                  f"You can still use the magic link from your email, "
                  f"or request a new code.",
            expired=True,
        )

    if entered_otp != prt.otp_code:
        write_audit_log(
            db, AuditLog,
            user_id    = user_id,
            action     = "password_reset_otp_failed",
            extra_data = {"entered": entered_otp},
        )
        return render_template(
            "reset/verify_otp.html",
            error="Incorrect code. Please try again.",
        )

    # OTP correct — mark token as OTP-verified so reset_password can proceed
    # We do NOT mark it used yet; reset_password will do that after new password is set
    session["reset_token_id"] = prt.id
    session.pop("reset_user_id", None)

    write_audit_log(
        db, AuditLog,
        user_id    = user_id,
        action     = "password_reset_otp_verified",
        extra_data = {},
    )

    return redirect(url_for("reset.reset_password", token=prt.token))


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2b / 3 — Set new password (reached via OTP verify OR magic link)
# ══════════════════════════════════════════════════════════════════════════════

@reset_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    prt = PasswordResetToken.query.filter_by(token=token, used=False).first()

    if not prt or prt.expires_at < datetime.now(UTC):
        return render_template(
            "reset/reset_password.html",
            error="This reset link is invalid or has expired. Please request a new one.",
            invalid=True,
            token=token,
        )

    # Security: if reached via direct URL (magic link path), verify session OR allow
    # magic link directly (token in URL IS the proof of identity for the magic link path)
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
    session.pop("reset_token_id", None)
    db.session.commit()

    write_audit_log(
        db, AuditLog,
        user_id    = user.id,
        action     = "password_reset_completed",
        extra_data = {"username": user.username},
    )

    return redirect(url_for("login", reset_success=1))


# ══════════════════════════════════════════════════════════════════════════════
# ADD EMAIL — for existing users who registered without one
# ══════════════════════════════════════════════════════════════════════════════

@reset_bp.route("/add-email", methods=["GET", "POST"])
@login_required
def add_email():
    """
    Logged-in users with no email on file are redirected here.
    After saving, they go to the page they originally wanted.
    """
    # If user already has an email, nothing to do
    if current_user.email:
        return redirect(url_for("home"))

    next_url = request.args.get("next") or url_for("home")

    if request.method == "GET":
        return render_template("reset/add_email.html", next_url=next_url)

    email = request.form.get("email", "").strip().lower()

    if not email or "@" not in email:
        return render_template(
            "reset/add_email.html",
            next_url=next_url,
            error="Please enter a valid email address.",
        )

    # Check uniqueness
    existing = User.query.filter(
        db.func.lower(User.email) == email,
        User.id != current_user.id,
    ).first()
    if existing:
        return render_template(
            "reset/add_email.html",
            next_url=next_url,
            error="That email is already registered to another account.",
        )

    current_user.email = email
    db.session.commit()

    write_audit_log(
        db, AuditLog,
        user_id    = current_user.id,
        action     = "email_added",
        extra_data = {"email": email},
    )

    return redirect(next_url)