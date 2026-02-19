"""
Job Worker — GuardianAI
RQ worker process. Runs alongside the Flask app via:
  python job_worker.py
"""

import logging
import os
import redis
from rq import SimpleWorker, Queue

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

redis_conn = redis.Redis(
    host=os.environ.get("REDIS_HOST", "localhost"),
    port=int(os.environ.get("REDIS_PORT", 6379)),
)
queue = Queue("default", connection=redis_conn)


if __name__ == "__main__":
    logger.info("GuardianAI worker starting...")
    worker = SimpleWorker([queue], connection=redis_conn)
    worker.work()