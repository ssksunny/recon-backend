"""
Entry point for the background worker process that consumes the document
processing queue (app/core/queue.py, app/jobs/document_jobs.py) — the
extraction/load-linking/matching work app/services/document_service.py
enqueues instead of running inline.

Run it with:

    python -m app.worker

...or with RQ's own CLI directly, which works identically since this is
just a plain RQ Queue against REDIS_URL:

    rq worker documents --url redis://localhost:6379/0

Scaling is "start another worker process somewhere" — RQ workers are
independent processes pulling from the same Redis-backed queue, no
coordination between them needed.

Not needed at all for local dev/testing with BACKGROUND_JOBS_ENABLED=false
(see app/core/config.py) — in that mode jobs run synchronously the instant
they're enqueued, right in the web process, so nothing needs to consume the
queue.
"""

import logging

from rq import Worker

from app.core.queue import DOCUMENT_QUEUE_NAME, get_redis_connection

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> None:
    worker = Worker([DOCUMENT_QUEUE_NAME], connection=get_redis_connection())
    worker.work()


if __name__ == "__main__":
    main()
