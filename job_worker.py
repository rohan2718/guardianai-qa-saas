"""
job_worker.py — GuardianAI
Windows-compatible RQ worker.

ROOT CAUSE FIX (Windows deadlock):
  RQ's SimpleWorker runs jobs synchronously in the same thread.
  tasks.py calls asyncio.run(run_crawler(...)) which creates a new
  ProactorEventLoop on Windows. This conflicts with RQ's own internal
  select()-based polling when both share the same thread — the worker
  silently accepts the job then hangs at 0% forever with no error.

SOLUTION:
  Run each job in a dedicated daemon thread with its own event loop.
  The main thread stays free for RQ's heartbeat/polling.
  ThreadedWorker below handles this transparently.
"""

import logging
import os
import sys
import time
import threading

import redis
from redis.exceptions import ConnectionError as RedisConnectionError
from rq import Queue
from rq.worker import SimpleWorker
from rq.job import Job

from dotenv import load_dotenv
from pathlib import Path
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("guardianai.worker")

REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
REDIS_URL  = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"

STARTUP_RETRIES   = 10
STARTUP_RETRY_SEC = 3


# ── No-op death penalty (SIGALRM doesn't exist on Windows) ────────────────────

class _NoOpDeathPenalty:
    def __init__(self, timeout, exception, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, exc_type, exc_val, exc_tb): return False
    def cancel(self): pass
    def handle_death_penalty(self, *args, **kwargs): pass


# ── Thread-isolated worker ─────────────────────────────────────────────────────

class ThreadedWindowsWorker(SimpleWorker):
    """
    Runs each job in a dedicated daemon thread with its own asyncio event loop.

    Why this fixes the hang:
      - SimpleWorker.perform_job() calls job.perform() in the CURRENT thread.
      - tasks.py → asyncio.run() creates a Windows ProactorEventLoop.
      - ProactorEventLoop on Windows uses IOCP which blocks the thread's
        select() — the same mechanism RQ uses for pub/sub heartbeats.
      - By offloading to a fresh thread, the main thread stays free for RQ
        and the job thread gets a clean event loop with no conflicts.
    """
    death_penalty_class = _NoOpDeathPenalty

    def perform_job(self, job: Job, queue: Queue, *args, **kwargs):
        """Override: run the job in a dedicated thread, wait for completion."""
        result_container = {"exc": None}

        def _run():
            import asyncio
            # Force a fresh ProactorEventLoop for this thread (Windows default
            # for asyncio.run(), but explicit is safer inside a new thread)
            if sys.platform == "win32":
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                # Delegate to SimpleWorker's normal job execution path
                super(ThreadedWindowsWorker, self).perform_job(job, queue, *args, **kwargs)
            except Exception as exc:
                result_container["exc"] = exc
                logger.error(f"Job {job.id} failed in thread: {exc}", exc_info=True)
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

        thread = threading.Thread(target=_run, daemon=True, name=f"rq-job-{job.id[:8]}")
        thread.start()
        # Block main thread until job thread completes (preserves RQ's
        # sequential job handling — one job at a time per worker process)
        thread.join()

        if result_container["exc"]:
            raise result_container["exc"]


# ── Redis connection with retry ────────────────────────────────────────────────

def _connect_redis() -> redis.Redis:
    pool = redis.ConnectionPool.from_url(
        REDIS_URL,
        max_connections=10,
        socket_connect_timeout=5,
        socket_timeout=None,
        retry_on_timeout=True,
        health_check_interval=30,
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


# ── Entrypoint ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Windows: set ProactorEventLoop as default BEFORE any asyncio import
    if sys.platform == "win32":
        import asyncio
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    redis_conn = _connect_redis()

    queues = [
        Queue("quick",   connection=redis_conn),
        Queue("default", connection=redis_conn),
    ]

    logger.info("GuardianAI worker starting (ThreadedWindowsWorker — asyncio-safe)...")
    logger.info(f"Listening on queues: {[q.name for q in queues]}")

    worker = ThreadedWindowsWorker(
        queues=queues,
        connection=redis_conn,
    )
    worker.work(burst=False)