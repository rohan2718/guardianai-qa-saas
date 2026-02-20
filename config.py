"""
config.py — GuardianAI
Single source of truth for all configuration.
app.py must import from here — no duplicate URL construction.
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
    # Raise at import time so the app never boots with an insecure key.
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
    password=os.environ.get("DB_PASS", "root"),   # Must be set via env in production
    host=os.environ.get("DB_HOST", "localhost"),
    port=int(os.environ.get("DB_PORT", 5432)),
    database=os.environ.get("DB_NAME", "qa_system"),
)

# ── Redis ──────────────────────────────────────────────────────────────────────

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
REDIS_URL  = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"

# ── App ────────────────────────────────────────────────────────────────────────

DEBUG = os.environ.get("FLASK_DEBUG", "false").lower() == "true"

# ── AI ────────────────────────────────────────────────────────────────────────

COHERE_API_KEY = os.environ.get("COHERE_API_KEY", "")

# ── Worker ────────────────────────────────────────────────────────────────────

# Default maximum wall-clock seconds a scan job may run before RQ kills it.
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", 3600))

# ── SaaS Plan Limits ──────────────────────────────────────────────────────────

PLAN_LIMITS: dict[str, dict] = {
    "free": {
        "scans_per_day": 5,
        "pages_per_scan": 50,
        "history_days": 7,
    },
    "pro": {
        "scans_per_day": 50,
        "pages_per_scan": 500,
        "history_days": 90,
    },
    "enterprise": {
        "scans_per_day": None,   # Unlimited
        "pages_per_scan": None,  # Unlimited
        "history_days": 365,
    },
}