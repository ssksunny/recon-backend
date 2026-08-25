# Recon — Go-Live Runbook (Render)

The paid/managed path from the two repos you have locally to a live system
at a real URL. Written for Render (backend: API + worker + Postgres +
Redis) and Vercel (frontend) — both take a `git push` and a few dashboard
clicks, neither needs you to hand-provision servers, and both have small,
predictable starter pricing. **Looking for the $0/month path instead?**
See `ORACLE_RUNBOOK.md` — one Oracle Cloud "Always Free" VM running the
same stack via Docker Compose, in exchange for doing your own ops
(patching, backups, monitoring) instead of Render doing it for you.
`DEPLOYMENT.md` in this repo is the reference checklist for hardening and
scaling later, and applies to either path; this doc is the Render-specific
one-time "make it live" sequence.

Rough cost at the settings below: **Render ≈ $30/month** (starter web
service $7 + starter worker $7 + basic Postgres $6 + starter Key Value $10)
plus **Vercel** (free Hobby tier exists, but its terms are meant for
personal/non-commercial projects — since this is a company tool, budget for
Pro, roughly $20/seat/month; confirm current pricing/terms at
vercel.com/pricing before you pick a plan) plus **Gemini API usage past its
free tier, or Anthropic API usage if you've switched `AI_PROVIDER`**
(pay-per-call, scales with document volume) plus a small S3 storage cost.
Check current numbers at render.com/pricing and vercel.com/pricing before
committing — pricing pages change.

## 0. What you need before starting

- A GitHub account (free tier is fine) — both platforms deploy from a git
  repo, not a zip upload.
- A Render account and a Vercel account.
- An Anthropic API key with billing set up (console.anthropic.com).
- An S3 bucket, or an S3-compatible one (Cloudflare R2, Backblaze B2, etc.)
  — **required**, not optional, for this topology. See step 2 for why and
  how.
- A domain you control, if you want the inbound-email feature working with
  real carriers (e.g. `reconapp.io`) — you can skip this at first and add
  it later without touching anything else.

## 1. Push the code to GitHub

Both repos in what you downloaded are already git repos with an initial
commit — you're pushing, not starting from scratch.

```bash
# Backend
cd recon-backend
gh repo create nextstock/recon-backend --private --source=. --remote=origin
git push -u origin master
# (no gh CLI? create an empty repo at github.com/new, then:)
#   git remote add origin git@github.com:<you>/recon-backend.git
#   git push -u origin master

# Frontend — same pattern, separate repo
cd ../recon-frontend
gh repo create nextstock/recon-frontend --private --source=. --remote=origin
git push -u origin master
```

## 2. Create an S3 bucket

Local disk storage does not work once the API and the worker are two
separate services with two separate filesystems (see the comment at the top
of `render.yaml`) — the worker wouldn't be able to read a file the API just
saved. This step is required before deploying, not a later hardening pass.

1. In the AWS console: S3 → Create bucket. Name it something like
   `recon-documents-prod`. Leave "Block all public access" **on** — Recon
   serves files back through its own authenticated API, never a public S3
   URL.
2. IAM → Users → create a user (e.g. `recon-app`) with programmatic access
   only, and attach a policy scoped to just that bucket:
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": ["s3:PutObject", "s3:GetObject"],
         "Resource": "arn:aws:s3:::recon-documents-prod/*"
       }
     ]
   }
   ```
3. Generate an access key for that user. You'll paste the key ID/secret
   into Render in the next step.

(Using R2/B2/MinIO instead of real S3: same idea, and set `S3_ENDPOINT_URL`
to that provider's endpoint — `app/core/storage.py` already supports it.)

## 3. Deploy the backend on Render

1. Render dashboard → **New +** → **Blueprint**.
2. Connect the `recon-backend` GitHub repo. Render reads `render.yaml` and
   shows you everything it's about to create: the `recon-db` Postgres
   instance, the `recon-redis` Key Value store, the `recon-api` web
   service, and the `recon-worker` background worker.
3. Before it lets you apply, it prompts for every value marked
   `sync: false` in `render.yaml`'s `recon-shared` env var group. Fill in:
   - `GEMINI_API_KEY` — your real key (aistudio.google.com/apikey). This
     path defaults to `AI_PROVIDER=gemini`; if you've switched it to
     `anthropic` in `render.yaml`, fill in `ANTHROPIC_API_KEY` instead.
   - `S3_BUCKET_NAME`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` — from
     step 2.
   - `INBOUND_EMAIL_DOMAIN`, `INBOUND_EMAIL_WEBHOOK_USERNAME`,
     `INBOUND_EMAIL_WEBHOOK_PASSWORD`, `MAILGUN_SIGNING_KEY` — see step 6.
     You can put placeholder values in now and come back once you've set
     the email side up — the API works fine without them, those two
     webhook routes just refuse all requests (503) until they're real.
   - `CORS_ORIGINS` — leave blank for now, you'll set it in step 5 once the
     frontend has a URL. Leaving it blank just means the API refuses
     browser requests from anywhere, which is the safe default until then.
   - `JWT_SECRET_KEY` is auto-generated by Render (`generateValue: true`)
     — you don't need to touch it.
4. Click **Apply**. Render provisions the database and Redis first, then
   builds and deploys `recon-api` and `recon-worker`. The API's build
   includes `alembic upgrade head` as a pre-deploy step, so the schema is
   created automatically — nothing to run by hand.
5. First deploy takes a few minutes. Watch the `recon-api` service's Logs
   tab; you're looking for uvicorn's "Application startup complete."
6. Note the URL Render assigns `recon-api` (Settings tab, something like
   `https://recon-api.onrender.com`). Confirm it's alive:
   ```bash
   curl https://recon-api.onrender.com/health
   # {"status":"ok","app":"Recon","env":"production"}
   ```

## 4. Deploy the frontend on Vercel

1. Vercel dashboard → **Add New** → **Project** → import the
   `recon-frontend` GitHub repo. Vercel auto-detects Next.js; no config
   needed.
2. Before deploying, add one environment variable:
   `NEXT_PUBLIC_API_URL` = `https://recon-api.onrender.com/api/v1`
   (your actual Render URL from step 3, plus the `/api/v1` prefix — this
   is a build-time variable for Next.js, so it must be set before the
   first deploy, not after).
3. Deploy. Note the URL Vercel assigns (e.g. `https://recon.vercel.app`,
   or set up a custom domain in Project Settings → Domains).

## 5. Connect the two

Back in Render, on `recon-api`'s Environment tab, set `CORS_ORIGINS` to
your exact Vercel origin — no trailing slash, no path:

```
CORS_ORIGINS=https://recon.vercel.app
```

(Comma-separate multiple origins if you add a custom domain later, e.g.
`https://recon.vercel.app,https://recon.nextstock.com`.) Saving triggers an
automatic redeploy of `recon-api`.

## 6. Create the first company + admin user

There's no registration page in the UI yet — only login — so the very
first account is created with one direct API call. After that, everyone
signs in normally through the frontend (adding teammates beyond the first
admin isn't built yet either; that's a natural next feature, not something
this runbook can shortcut).

```bash
curl -X POST https://recon-api.onrender.com/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "company_name": "NextStock",
    "company_slug": "nextstock",
    "admin_email": "you@nextstock.com",
    "admin_password": "a-real-password",
    "admin_full_name": "Your Name"
  }'
```

This returns an access token (and creates `nextstock@<INBOUND_EMAIL_DOMAIN>`
as the company's inbound address). You don't need to save that token —
just go to your Vercel URL and log in normally with the email/password you
just set.

## 7. Wire up real inbound email (optional at first)

This is the same setup covered in detail earlier in this project (DNS/MX
records, Postmark vs. Mailgun webhook configuration, signing secrets) —
repeating just the parts that change now that there's a live URL:

- Whichever provider you pick, its inbound webhook URL now points at your
  live Render service, not localhost:
  - Postmark: `https://<user>:<pass>@recon-api.onrender.com/api/v1/email/inbound/postmark`
    (the same username/password you set as
    `INBOUND_EMAIL_WEBHOOK_USERNAME`/`_PASSWORD` in step 3).
  - Mailgun: `https://recon-api.onrender.com/api/v1/email/inbound/mailgun`
    (auth is via the HMAC signature, using `MAILGUN_SIGNING_KEY`).
- Set `INBOUND_EMAIL_DOMAIN` (Render env var) to whatever domain you
  configured with the provider (e.g. `inbound.nextstock.com`) — this is
  what each company's `<slug>@<domain>` address is built from. If you
  change it after companies already exist, existing companies keep their
  old address; only new registrations pick up the new domain (see
  `app/api/auth.py`) — update existing rows by hand if you rename it.
- Until this is set up, direct file upload (already fully working) is a
  complete substitute — email ingestion is a convenience on top of it, not
  a dependency.

## 8. Go-live smoke test

- [ ] `GET https://recon-api.onrender.com/health` returns 200.
- [ ] Log into the Vercel URL with the account from step 6.
- [ ] Upload a rate confirmation + invoice PDF through the UI; confirm the
      document moves to `processed` and a match decision appears on the
      load within a few seconds (this exercises the `recon-worker`
      service — if it hangs at `received`, check the worker's logs in
      Render first).
- [ ] Approve/dispute/override a line item and confirm it shows up in the
      audit trail.
- [ ] Settings page shows the real inbound email address.
- [ ] If step 7 is done: send a real test email with an attached PDF to
      that address and confirm a load appears without any manual upload.
- [ ] From a second browser/incognito window, register a second company
      (`POST /auth/register` again with a different slug) and confirm it
      sees none of the first company's data.

Once all of that's green, you're live. Go back through `DEPLOYMENT.md` for
the hardening items (rate limiting the webhooks, error tracking, backup
verification) before real carrier volume hits the inbound address.
