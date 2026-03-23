"""
config.py — GuardianAI
Single source of truth for all configuration.
app.py must import from here — no duplicate URL construction.

CHANGE 3: pro and enterprise plans now have pages_per_scan: None (unlimited).
          Added max_concurrent_pages advisory key.
"""

import os
import sys
from pathlib import Path
from sqlalchemy.engine import URL

from dotenv import load_dotenv

# Always load .env relative to this file's directory, not cwd
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / ".env")

# ── Directories ────────────────────────────────────────────────────────────────

SCREENSHOT_DIR = os.path.join(BASE_DIR, "screenshots")
RAW_DIR        = os.path.join(BASE_DIR, "raw")
REPORT_DIR     = os.path.join(BASE_DIR, "reports")

for _d in [SCREENSHOT_DIR, RAW_DIR, REPORT_DIR]:
    os.makedirs(_d, exist_ok=True)

# ── Security — Hard fail on missing SECRET_KEY ─────────────────────────────────

SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    print(
        "\n[FATAL] SECRET_KEY environment variable is not set.\n"
        "        Set it before starting the application:\n"
        "        export SECRET_KEY=$(python -c 'import secrets; print(secrets.token_hex(32))')\n",
        file=sys.stderr,
    )
    sys.exit(1)

# ── Database ───────────────────────────────────────────────────────────────────

DB_URL = URL.create(
    drivername="postgresql",
    username=os.environ.get("DB_USER", "postgres"),
    password=os.environ.get("DB_PASS", "root"),
    host=os.environ.get("DB_HOST", "localhost"),
    port=int(os.environ.get("DB_PORT", 5432)),
    database=os.environ.get("DB_NAME", "qa_system"),
)

# ── Redis ──────────────────────────────────────────────────────────────────────

REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
REDIS_URL  = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"

# ── App ────────────────────────────────────────────────────────────────────────

DEBUG = os.environ.get("FLASK_DEBUG", "false").lower() == "true"

# ── AI ────────────────────────────────────────────────────────────────────────

COHERE_API_KEY = os.environ.get("COHERE_API_KEY", "")

# ── Worker ────────────────────────────────────────────────────────────────────

# Default maximum wall-clock seconds a scan job may run before RQ kills it.
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", 3600))

# Safety cap for unlimited scans — prevents infinite crawls on sites with
# auto-generated or paginated URLs. Configurable via env var.
MAX_UNLIMITED_PAGES = int(os.environ.get("MAX_UNLIMITED_PAGES", "2000"))

# ── SaaS Plan Limits ──────────────────────────────────────────────────────────
# CHANGE 3:
#   - pro.pages_per_scan → None (unlimited)
#   - enterprise.pages_per_scan → None (already was None)
#   - Added max_concurrent_pages as advisory (not hard-enforced at scan level)
#   - free plan unchanged

PLAN_LIMITS: dict[str, dict] = {
    "free": {
        "scans_per_day":       5,
        "pages_per_scan":      50,    # Free: hard capped at 50 pages
        "history_days":        7,
        "max_concurrent_pages": 50,   # advisory
    },
    "pro": {
        "scans_per_day":       50,
        "pages_per_scan":      None,  # CHANGE 3: Unlimited pages
        "history_days":        90,
        "max_concurrent_pages": None, # advisory — unlimited
    },
    "enterprise": {
        "scans_per_day":       None,   # Unlimited
        "pages_per_scan":      None,   # Unlimited
        "history_days":        365,
        "max_concurrent_pages": None,  # advisory — unlimited
    },
}

# ── Registration Gate ─────────────────────────────────────────────────────────
REGISTRATION_OPEN = os.environ.get("REGISTRATION_OPEN", "true").lower() == "true"

# ── SMTP (password reset emails) ─────────────────────────────────────────────
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", ""))