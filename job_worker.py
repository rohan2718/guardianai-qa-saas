"""
job_worker.py — GuardianAI
Windows-compatible RQ worker using SimpleWorker (no os.fork required).

Start with:
    python job_worker.py
"""

import contextlib
import logging
import os
import sys
import time

import redis
from redis.exceptions import ConnectionError as RedisConnectionError
from rq import Queue
from rq.worker import SimpleWorker

from dotenv import load_dotenv
from pathlib import Path
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("guardianai.worker")

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
REDIS_URL  = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"

STARTUP_RETRIES   = 10
STARTUP_RETRY_SEC = 3


# ── Windows-compatible no-op death penalty ─────────────────────────────────────
# RQ's default death penalty uses SIGALRM which does not exist on Windows.
# This context manager is a safe no-op replacement that lets jobs run
# without a signal-based timeout. Job-level timeouts are enforced separately
# via the JOB_TIMEOUT env var passed when enqueueing (see app.py / tasks.py).

class _NoOpDeathPenalty:
    """No-op context manager — replaces SIGALRM-based timeout on Windows."""

    def __init__(self, timeout, exception, **kwargs):
        pass  # timeout value intentionally ignored; no signal is registered

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False  # never suppress exceptions

    def cancel(self):
        pass

    def handle_death_penalty(self, *args, **kwargs):
        pass


class WindowsSimpleWorker(SimpleWorker):
    """
    SimpleWorker subclass that replaces the SIGALRM-based death penalty
    with a no-op so it runs correctly on Windows.
    """
    death_penalty_class = _NoOpDeathPenalty


# ── Redis connection ───────────────────────────────────────────────────────────

def _connect_redis() -> redis.Redis:
    pool = redis.ConnectionPool.from_url(
        REDIS_URL,
        max_connections=10,
        socket_connect_timeout=5,
        socket_timeout=None,
        retry_on_timeout=True,
        health_check_interval=30
    )
    conn = redis.Redis(connection_pool=pool)

    for attempt in range(1, STARTUP_RETRIES + 1):
        try:
            conn.ping()
            logger.info(f"Redis connected: {REDIS_HOST}:{REDIS_PORT}")
            return conn
        except RedisConnectionError as e:
            if attempt == STARTUP_RETRIES:
                logger.error(f"Redis unreachable after {STARTUP_RETRIES} attempts. Exiting.")
                sys.exit(1)
            logger.warning(
                f"Redis not ready (attempt {attempt}/{STARTUP_RETRIES}): {e} "
                f"— retrying in {STARTUP_RETRY_SEC}s"
            )
            time.sleep(STARTUP_RETRY_SEC)


if __name__ == "__main__":
    redis_conn = _connect_redis()
    queue      = Queue("default", connection=redis_conn)

    logger.info("GuardianAI worker starting (WindowsSimpleWorker — SIGALRM-free mode)...")

    worker = WindowsSimpleWorker(
        queues=[queue],
        connection=redis_conn,
    )
    worker.work(burst=False)