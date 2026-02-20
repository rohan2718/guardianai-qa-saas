"""
job_worker.py — GuardianAI
Windows-compatible RQ worker using SimpleWorker (no os.fork required).

Start with:
    python job_worker.py
"""

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


def _connect_redis() -> redis.Redis:
    pool = redis.ConnectionPool.from_url(
        REDIS_URL,
        max_connections=10,
        socket_connect_timeout=5,
        socket_timeout=5,
        retry_on_timeout=True,
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
            logger.warning(f"Redis not ready (attempt {attempt}/{STARTUP_RETRIES}): {e} — retrying in {STARTUP_RETRY_SEC}s")
            time.sleep(STARTUP_RETRY_SEC)


if __name__ == "__main__":
    redis_conn = _connect_redis()
    queue      = Queue("default", connection=redis_conn)

    logger.info("GuardianAI worker starting (SimpleWorker — Windows mode)...")

    worker = SimpleWorker(
        queues=[queue],
        connection=redis_conn,
    )
    worker.work(
        burst=False,
    )