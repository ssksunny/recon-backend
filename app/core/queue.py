"""
Background job queue for document processing (extraction, load-linking, and
matching — the parts of ingestion that make one or more Claude API calls and
can genuinely take a few seconds each per document).

app/services/document_service.py enqueues app/jobs/document_jobs.process_document
here instead of running it inline, so an upload request or an inbound email
webhook gets a fast HTTP response no matter how many documents (or how
large) are involved — critical for email in particular, since providers
expect a webhook to respond quickly and will retry (or eventually give up)
on one that doesn't.

BACKGROUND_JOBS_ENABLED (see app/core/config.py) mirrors the explicit-switch
philosophy already used for STORAGE_BACKEND: true (production) queues the
job for a separate `rq worker documents` process to pick up; false runs the
job synchronously the instant it's enqueued, still through Redis (RQ needs a
real connection either way — this isn't an in-memory fake), just without a
second process consuming the queue. That's for local dev without a worker
running; production should always run with the default (true) plus at least
one `rq worker` process alive, or documents will queue up and never process.
"""

from __future__ import annotations

from functools import lru_cache

import redis
from rq import Queue

from app.core.config import settings

DOCUMENT_QUEUE_NAME = "documents"


@lru_cache
def get_redis_connection() -> redis.Redis:
    return redis.from_url(settings.redis_url)


@lru_cache
def get_document_queue() -> Queue:
    return Queue(
        DOCUMENT_QUEUE_NAME,
        connection=get_redis_connection(),
        is_async=settings.background_jobs_enabled,
    )


# Module-level singleton for straightforward imports, mirroring
# app.core.storage's `storage` — callers that don't need to reason about
# construction just use this.
document_queue = get_document_queue()
