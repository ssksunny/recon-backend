# Recon — Production Deployment & Hardening Checklist

This is a working checklist for taking Recon from "runs on my machine" to a
real production deployment. It's written against the codebase as it stands
today (FastAPI + PostgreSQL + Redis/RQ backend, Next.js frontend, a
dual-provider AI layer defaulting to Gemini — see `app/ai/matching.py`),
and calls out the specific settings, files, and gaps that exist right now —
not generic advice. Items are grouped by area; within each area, roughly
most-important-first. Applies to either go-live path — `RUNBOOK.md`
(Render + Vercel, paid/managed) or `ORACLE_RUNBOOK.md` (Oracle Cloud Always
Free + Vercel, self-hosted, $0/month).

## 1. Secrets & environment configuration

- [ ] Every backend setting lives in `app/core/config.py` / `.env.example`.
      Copy `.env.example` to a real `.env` (or your platform's env-var
      store) per environment and fill in production values — do not deploy
      with any of the `.env.example` defaults, especially:
  - `JWT_SECRET_KEY` — must be a long, random, unique-per-environment
    secret. If this leaks, every issued token is forgeable. Rotate it if
    you ever suspect exposure (this immediately invalidates all existing
    sessions — there's no refresh-token flow yet, so users just re-login).
  - `GEMINI_API_KEY` (or `ANTHROPIC_API_KEY` if `AI_PROVIDER=anthropic` —
    see app/ai/matching.py's module docstring for the provider switch) —
    production key, separate from any dev/test key, with its own spend
    alerting (see §8). If staying on Gemini's free tier in production,
    note its terms allow submitted content to be used to improve Google's
    products — worth moving to a paid tier before real carrier documents
    flow through it.
  - `DATABASE_URL` — should point at a managed Postgres instance (RDS,
    Cloud SQL, etc.), not a local container, and should use a dedicated
    `recon` role rather than a superuser.
  - `INBOUND_EMAIL_WEBHOOK_USERNAME` / `_PASSWORD` and `MAILGUN_SIGNING_KEY`
    — see §7, these gate who can post fake carrier documents into your
    system.
  - `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` — only needed if not
    using an IAM role/instance profile; prefer an IAM role in production
    (ECS task role, EC2 instance profile, etc.) and leave these blank.
- [ ] Set `ENV=production` and `DEBUG=false`. `ENV=production` already
      disables `/docs` and `/redoc` in `app/main.py` — don't undo that by
      hardcoding `docs_url`.
- [ ] Never commit `.env`. Confirm it's gitignored before the first deploy,
      not after.
- [ ] Use your platform's secret manager (AWS Secrets Manager / SSM
      Parameter Store, GCP Secret Manager, Doppler, etc.) rather than plain
      env files on disk where you have the option — especially for
      `JWT_SECRET_KEY` and `GEMINI_API_KEY`/`ANTHROPIC_API_KEY`.

## 2. Database & migrations

- [ ] Alembic is set up (`alembic/`, baseline revision
      `aa9d09ab32d5_initial_schema.py`). `alembic/env.py` reads
      `DATABASE_URL` from `app.core.config.settings`, not from
      `alembic.ini` — so migrations always run against whatever database
      the app itself is configured for, in every environment.
- [ ] Run `alembic upgrade head` as an explicit deploy step, before the new
      app version starts serving traffic — not as something the app does
      on startup. Model it as its own CI/CD stage (e.g. a pre-deploy job
      or an init container) so a failed migration blocks the deploy
      instead of half-succeeding.
- [ ] Take a database snapshot/backup immediately before running migrations
      in production (see §9) — Alembic downgrades are tested (see the
      note below) but a snapshot is the actual safety net for anything a
      downgrade doesn't cleanly reverse.
- [ ] The baseline migration's `downgrade()` explicitly drops the 9
      Postgres ENUM types the schema uses, in addition to the tables —
      autogenerate does not do this by default (`DROP TABLE` doesn't
      `DROP TYPE`), and it was a real bug caught and fixed during
      development. If you add a new enum column later, remember the same
      pattern: autogenerate the migration, then confirm its `downgrade()`
      also drops any new enum type it introduced.
- [ ] After generating any future migration, run `alembic check` against a
      throwaway database to confirm there's no drift between the models
      and the migration before merging.
- [ ] Use a connection pooler (PgBouncer, or your managed Postgres
      provider's built-in pooler) once you're running multiple app
      instances plus RQ workers — each process holds its own SQLAlchemy
      connection pool (`app/models/database.py` uses `pool_pre_ping=True`
      but the default pool size), and that adds up across instances.

## 3. Background workers (RQ)

- [ ] `BACKGROUND_JOBS_ENABLED=true` in every real environment. This is
      the single most important flag to get right at deploy time — at
      `false` (the test/dev default), document jobs are never actually
      queued for a worker, they just run synchronously in the API
      process. Leaving it `false` in production doesn't crash anything;
      it just quietly makes every upload/email-ingest request slow
      (blocking on Claude extraction) and removes the isolation
      background jobs exist for. Verify it's `true` as an explicit
      pre-launch check, not an assumption.
- [ ] Run `python -m app.worker` (equivalently `rq worker documents --url
      $REDIS_URL`) as its own long-running process/service — a separate
      ECS service, Kubernetes deployment, systemd unit, or Heroku worker
      dyno, distinct from the web process. It is not started by the web
      app and nothing currently supervises it; if it's not deployed, or
      it crashes and isn't restarted, uploaded documents will sit in
      `received` status forever with no error surfaced to the user.
  - Run **at least two** worker processes/replicas for basic redundancy,
    and put a process supervisor in front of each (systemd `Restart=on-failure`, or your
    orchestrator's restart policy) so a worker crash self-heals.
  - Size worker count to expected email/upload volume — each extraction
    call is a blocking network call to the AI provider, so throughput
    scales roughly linearly with worker count, not CPU. If running on
    Gemini's free tier, also watch its rate limits specifically — they're
    the more likely throughput ceiling at real volume, not worker count.
- [ ] There is currently no retry policy, dead-letter queue, or job-failure
      alerting configured on the RQ side — a job that raises is caught
      inside `process_document` (which writes an `extraction_failed`
      audit entry and sets the document to `failed`), but a job that
      crashes the *worker process itself* (OOM, unhandled exception in RQ
      machinery) is simply lost. Before real volume, add:
  - RQ's built-in retry (`Retry(max=N)` on `queue.enqueue(...)`) for
    transient failures (AI provider timeouts, brief Redis blips).
  - A failed-job monitor — RQ's `FailedJobRegistry`, or forward failures
    to your alerting tool — so a stuck/failed job produces a page, not
    silence.
- [ ] Known ordering gap (documented in `app/services/email_service.py`):
      when an email has multiple attachments, they're enqueued
      rate-confirmation-first as a best-effort optimization so the load
      usually exists by the time the invoice/POD jobs run — but under
      multiple concurrent workers this ordering isn't guaranteed. An
      invoice document that gets processed before its rate confirmation
      simply won't link to a load yet. There's no "relink orphaned
      documents" sweep yet; if this matters at your volume, that's the
      next thing to build (a periodic job that retries
      `find_load_by_number` for documents still unlinked after N
      minutes).

## 4. Redis in production

- [ ] Use a managed Redis (ElastiCache, Upstash, Redis Cloud, etc.) with
      persistence/backups enabled, not a bare container — RQ's queue and
      job metadata live entirely in Redis; losing it mid-flight loses
      in-flight and queued document-processing jobs.
- [ ] Enable TLS and auth on the Redis connection in production
      (`rediss://` and a password/token in `REDIS_URL`) — the default
      `redis://localhost:6379/0` has neither.
- [ ] Redis is a single point of failure for document processing (nothing
      falls back to synchronous processing if it's unreachable — uploads
      will fail with a 422 from `ProcessingError`, since
      `document_service.receive_document` raises rather than silently
      dropping the job). Plan for that explicitly: managed Redis with
      automatic failover, or at minimum alerting on Redis availability.
- [ ] If you outgrow a single Redis instance's throughput, RQ workers
      scale horizontally against the same queue with no coordination
      needed — that's the first lever, before reaching for Redis
      clustering.

## 5. CORS & HTTPS

- [ ] `CORS_ORIGINS` in `app/core/config.py` defaults to
      `http://localhost:3000`. Set it to your real frontend origin(s)
      (comma-separated for multiple, e.g. a staging + production
      frontend) — a wildcard or leftover localhost entry in production
      CORS config is a real vulnerability, not just a lint issue.
- [ ] Terminate TLS in front of the API (load balancer, Cloudflare, or
      your platform's built-in HTTPS) — the app itself doesn't handle
      TLS. Redirect HTTP to HTTPS at that layer.
- [ ] Set `allow_credentials=True` (already the case) only alongside an
      explicit origin allowlist, never a wildcard `*` — FastAPI/Starlette
      will actually reject that combination, but worth stating as a rule
      if `cors_origins` config ever gets simplified.
- [ ] Frontend: set `NEXT_PUBLIC_API_URL` to the production API's HTTPS
      URL at build time for whichever hosting you use (Vercel env vars,
      or your CI's build step).

## 6. Inbound webhook rate limiting & abuse protection

- [ ] `POST /api/v1/email/inbound/postmark` and `/mailgun` are public
      internet-facing endpoints by necessity (that's how Postmark/Mailgun
      reach you) — they're authenticated (`INBOUND_EMAIL_WEBHOOK_USERNAME`
      /`_PASSWORD` for Postmark's HTTP Basic Auth, `MAILGUN_SIGNING_KEY`
      for Mailgun's HMAC signature), but **nothing currently rate-limits
      them**. Add rate limiting at the edge (your load balancer/API
      gateway/Cloudflare) or in-app (e.g. `slowapi`) before launch — a
      credential-stuffing attempt against the Postmark Basic Auth
      endpoint, or a flood of oversized attachments, currently has no
      throttle.
- [ ] There's also no explicit file-size limit on either the email
      attachment path or the direct `/documents/upload` endpoint today —
      `file.file.read()` in `app/api/documents.py` and the Postmark
      base64/Mailgun multipart decoding in `app/schemas/email.py` /
      `app/api/email.py` will read whatever's sent. Add a size cap (both
      at the reverse-proxy/load-balancer level, which is the cheaper
      place to reject something huge, and as a defensive check in the app)
      before this is internet-facing.
- [ ] Confirm both webhook secrets are actually set in production —
      leaving `INBOUND_EMAIL_WEBHOOK_USERNAME`/`_PASSWORD` or
      `MAILGUN_SIGNING_KEY` unset makes those endpoints refuse every
      request (503), which is the fail-safe default, but it also means a
      forgotten env var silently breaks email ingestion for every tenant
      rather than throwing an obvious startup error. Worth a startup-time
      assertion if you want a louder failure mode.
- [ ] Rotate the Postmark Basic Auth credentials and Mailgun signing key
      the same way you'd rotate any other secret if they're ever exposed
      (e.g. accidentally logged, or the webhook URL leaks) — both are
      configured directly in the provider's dashboard, not derivable from
      anything else.

## 7. Logging & monitoring

- [ ] There's no structured logging or error-tracking integration wired
      up yet beyond `app/worker.py`'s basic `logging.basicConfig`. Before
      production traffic:
  - Add an error tracker (Sentry or equivalent) to both the FastAPI app
    and the RQ worker process — the worker especially, since a failed job
    today only leaves an audit-log row on the load it was processing, not
    a global alert.
  - Log at the request level (a simple ASGI logging middleware, or your
    platform's built-in access logs) with request IDs, so a failed
    extraction can be traced from "load X's document is stuck" back to
    the actual API request and worker job.
- [ ] Track the AI provider's usage/cost and failure rate separately —
      extraction and classification calls (`app/ai/matching.py`) are the
      main external dependency and the main variable cost (or, on Gemini's
      free tier, the main rate-limit risk); a spike in failures there is
      both a UX problem (documents stuck as `failed`) and possibly a
      cost/rate-limit problem.
- [ ] `GET /health` (in `app/main.py`) is a liveness probe only — it
      confirms the process is up, not that the database or Redis are
      reachable. Point your platform's health check at it for basic
      liveness, but don't treat a 200 there as "the system is fully
      healthy"; consider adding a `/health/ready` that checks DB + Redis
      connectivity if you want a real readiness gate.
- [ ] Monitor RQ queue depth (`app/core/queue.py`'s `document_queue`) —
      a growing backlog is the leading indicator that you're short on
      worker capacity before users notice documents stuck in `received`.

## 8. Backups & disaster recovery

- [ ] Enable automated daily backups + point-in-time recovery on the
      production Postgres instance (RDS/Cloud SQL both support this
      natively) — this is the system of record for every load, document
      metadata, review, and audit-log entry.
- [ ] Back up (or enable versioning on) the S3 bucket storing original
      documents (§9) — losing the originals means losing the ability to
      re-verify an AI extraction or show a document to a carrier disputing
      a charge, even though the extracted data survives in Postgres.
- [ ] Redis backups matter less for durability (queue state is transient
      by nature) but still worth enabling on a managed Redis so a
      restart/failover doesn't drop in-flight jobs.
- [ ] Actually test a restore at least once before launch, not just
      confirm backups are "enabled" — a backup you haven't restored from
      is unverified.

## 9. Object storage (S3 switch)

- [ ] `app/core/storage.py` switches on `STORAGE_BACKEND` (`local` |
      `s3`) — this is an explicit config switch, not inferred from
      whether AWS credentials happen to be present, so flipping it is a
      deliberate one-line change: set `STORAGE_BACKEND=s3` and
      `S3_BUCKET_NAME`/`S3_REGION`.
- [ ] Prefer an IAM role over `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`
      when running on AWS infrastructure (ECS task role, EC2 instance
      profile) — leave those two blank and `boto3` will pick up the role
      automatically.
- [ ] Set the bucket to private (no public access) — documents are served
      back to the frontend through the authenticated
      `GET /documents/{id}/file` endpoint (which streams via `storage.load`
      and enforces the same tenant scoping as everything else), never via
      a direct public S3 URL.
- [ ] Enable S3 bucket versioning and server-side encryption
      (SSE-S3 or SSE-KMS) at rest.
- [ ] `local` storage (`LOCAL_STORAGE_DIR`) is fine for dev but implies
      non-durable, single-instance storage — do not run production on
      `STORAGE_BACKEND=local` if you're running more than one app
      instance, since only the instance that received the upload would
      have the file on disk.

## 10. Application hardening

- [ ] JWTs (`app/core/security.py`) currently have no refresh-token flow —
      `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` (default 60) is the only
      expiration lever, and there's no server-side revocation (a stolen
      token is valid until it expires, full stop). For an internal
      AP/ops tool this is a reasonable MVP tradeoff; if this expands to
      more sensitive access, a shorter expiry plus a refresh-token flow
      (or moving to short-lived tokens + a session store you can revoke)
      is the natural next step.
- [ ] Passwords are hashed with `bcrypt` directly (not passlib — a
      deliberate choice noted in `app/core/security.py` to avoid a known
      passlib/bcrypt≥4.1 incompatibility). No password complexity
      requirements are enforced beyond the 8–72 character bcrypt-limit
      validation in `app/schemas/auth.py` — consider whether you want a
      minimum-strength check before opening self-serve registration to
      the public (today, `POST /auth/register` creates a brand-new tenant
      with no invite/approval gate — decide if that should stay open or
      move behind an allowlist/admin-approval step in production).
- [ ] Multi-tenant isolation is enforced by convention (every query filters
      on `current_user.company_id`, per `app/api/deps.py`'s docstring) —
      this is tested (`test_company_data_isolation`), but it's worth a
      standing rule for anyone adding new endpoints: never accept a
      `company_id` from the request, always derive it from the
      authenticated user.
- [ ] `python -m pyflakes` was used as a lightweight static-check pass
      during development — worth wiring into CI as a real gate (along
      with the existing pytest suite) so it runs on every PR, not just
      manually.

## 11. Frontend deployment

- [ ] Set `NEXT_PUBLIC_API_URL` per environment at build time (see §5).
- [ ] `npx tsc --noEmit`, `npm run build`, and `npm run lint` all need to
      pass clean before deploy — this has been the verification bar for
      every change so far; keep it as a CI gate (e.g. a GitHub Actions
      workflow that runs all three on every PR) rather than a manual
      step.
- [ ] Standard Next.js production hosting (Vercel, or a Node server behind
      your own load balancer) both work fine — nothing in the app relies
      on server-only Next.js features beyond what App Router gives you by
      default.
- [ ] The JWT is stored in `localStorage` (`lib/api.ts`) — fine for an
      internal tool behind HTTPS, but note it's readable by any script
      running on the page, so keep an eye on third-party script exposure
      (analytics tags, etc.) if any get added later.

## 12. Suggested infrastructure topology

A reasonable minimal production layout, given what's built:

- 1 load balancer / reverse proxy (TLS termination, routes `/api/*` to the
  backend, everything else to the frontend if not using Vercel).
- ≥2 FastAPI app instances (stateless, safe to run behind a load balancer
  as-is — sessions are just JWTs, no server-side session state).
- ≥2 RQ worker instances (`python -m app.worker`), separate from the app
  instances (§3).
- 1 managed Postgres instance with automated backups + PITR (§2, §8).
- 1 managed Redis instance, TLS + auth enabled (§4).
- 1 S3 bucket, private, versioned, encrypted (§9).
- Frontend on Vercel or as its own set of instances behind the same load
  balancer.
- Error tracking (Sentry or equivalent) wired into both the app and worker
  processes (§7).

## 13. Pre-launch smoke test

Before pointing real carriers at the inbound email address:

- [ ] `alembic upgrade head` runs clean against the production database.
- [ ] `alembic check` shows no drift.
- [ ] A worker process is running and actually consuming jobs — upload a
      test document through the UI and confirm it moves from `received`
      → `processing` → `processed` (or `failed`, if you intentionally
      test a bad file) without any manual intervention.
- [ ] Send a real test email (through the actual configured
      Postmark/Mailgun webhook, not a curl'd fake payload) with a rate
      confirmation and invoice attached, and confirm a load is created
      and matched end to end.
- [ ] Confirm `BACKGROUND_JOBS_ENABLED=true` in the running app's actual
      environment (not just `.env.example` or a config file — check what
      the deployed process resolved).
- [ ] Confirm CORS only allows your real frontend origin(s).
- [ ] Confirm `/docs` and `/redoc` are disabled (implied by
      `ENV=production`).
- [ ] Confirm the S3 bucket is private and `STORAGE_BACKEND=s3`.
- [ ] Two people, two different company accounts: confirm neither can see
      the other's loads/documents/reviews.
