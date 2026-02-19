"""
Config — GuardianAI
All configuration from environment variables. No secrets hardcoded.
"""

import os
from sqlalchemy.engine import URL

BASE_DIR = os.getcwd()

# Directories
SCREENSHOT_DIR = os.path.join(BASE_DIR, "screenshots")
RAW_DIR = os.path.join(BASE_DIR, "raw")
REPORT_DIR = os.path.join(BASE_DIR, "reports")

for d in [SCREENSHOT_DIR, RAW_DIR, REPORT_DIR]:
    os.makedirs(d, exist_ok=True)

# Database — read from environment, never hardcoded
DB_URL = URL.create(
    drivername="postgresql",
    username=os.environ.get("DB_USER", "postgres"),
    password=os.environ.get("DB_PASS", ""),      # Must be set via env in production
    host=os.environ.get("DB_HOST", "localhost"),
    port=int(os.environ.get("DB_PORT", 5432)),
    database=os.environ.get("DB_NAME", "qa_system"),
)

# Redis
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))

# App
SECRET_KEY = os.environ.get("SECRET_KEY", "CHANGE-THIS-IN-PRODUCTION-DO-NOT-DEPLOY-WITH-DEFAULT")
DEBUG = os.environ.get("FLASK_DEBUG", "false").lower() == "true"

# AI
COHERE_API_KEY = os.environ.get("COHERE_API_KEY", "")

# SaaS Plan Limits
PLAN_LIMITS = {
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
        "scans_per_day": None,      # Unlimited
        "pages_per_scan": None,     # Unlimited
        "history_days": 365,
    },
}