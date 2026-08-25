# Recon — Go-Live on Oracle Cloud "Always Free" (self-hosted, $0/month)

The free alternative to the Render path in `RUNBOOK.md`: one Oracle Cloud
VM, permanently free, running Postgres + Redis + the API + the background
worker as Docker containers via `docker-compose.yml`. In exchange for $0
hosting, you're the one patching the OS, watching disk space, and renewing
nothing (Caddy handles TLS automatically) — see the honest trade-off
discussion in the conversation that led here. The frontend still deploys to
Vercel exactly as in `RUNBOOK.md` step 4 — only the backend moves.

## 0. What you need before starting

- An Oracle Cloud account (cloud.oracle.com — a credit card is required for
  identity verification, but Always Free resources are never charged).
- A domain you control, with the ability to add an A record (e.g.
  `api.nextstock.com` pointing at the VM). Let's Encrypt (which Caddy uses
  automatically) will not issue a certificate for a bare IP address — a real
  domain is required, not optional, for HTTPS.
- A Gemini API key (aistudio.google.com/apikey) — free tier, no billing
  required for that specific step.
- If you want real inbound email working: a Postmark or Mailgun account
  (see step 7).

## 1. Create the Always Free VM

1. In the Oracle Cloud console: **Compute → Instances → Create Instance**.
2. Under **Image and shape**, pick an Ubuntu image, then **Change shape** →
   **Ampere (Arm-based processor)** → `VM.Standard.A1.Flex`. Set it to 2
   OCPUs / 12 GB RAM (leaves headroom to run a second free instance later
   if you ever want one — the Always Free allowance is 4 OCPUs / 24 GB
   total across your Ampere instances).
3. Add your SSH public key under **Add SSH keys** (generate one locally
   first if you don't have one: `ssh-keygen -t ed25519`).
4. Create the instance. Note its public IP once it's running.
5. **Networking → Virtual Cloud Networks** → your VM's VCN → the subnet's
   **Security List** → **Add Ingress Rules**: allow TCP 80 and TCP 443 from
   `0.0.0.0/0` (Caddy needs both — 80 for the Let's Encrypt challenge, 443
   for HTTPS traffic itself). Port 22 (SSH) is open by default.

## 2. Point DNS at it

Add an A record for your chosen subdomain (e.g. `api.nextstock.com`)
pointing at the VM's public IP, at whatever registrar/DNS provider you use.
Give it a few minutes to propagate before step 5 — Caddy's automatic
certificate request will fail if the domain doesn't resolve yet.

## 3. Install Docker on the VM

SSH in (`ssh ubuntu@<vm-public-ip>`) and run:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker
```

## 4. Get the code onto the VM and configure it

```bash
git clone <your-recon-backend-github-url> recon-backend
cd recon-backend
cp .env.example .env
nano .env   # or vim/whatever you have
```

In `.env`, at minimum set:

- `POSTGRES_PASSWORD` — a real random password (this also becomes the
  Postgres container's password — `docker-compose.yml` reads it from here).
- `JWT_SECRET_KEY` — a long random string (`openssl rand -hex 32` works).
- `AI_PROVIDER=gemini` and `GEMINI_API_KEY` — your real key.
- `ENV=production`, `DEBUG=false`.
- `CORS_ORIGINS` — leave blank for now, come back to it in step 6.
- `INBOUND_EMAIL_DOMAIN`, `INBOUND_EMAIL_WEBHOOK_USERNAME`/`_PASSWORD`,
  `MAILGUN_SIGNING_KEY` — can be placeholders for now, see step 7.

Leave `DATABASE_URL`, `REDIS_URL`, and `STORAGE_BACKEND` as they are —
`docker-compose.yml` overrides those three regardless of what's in `.env`
(see the comments in `.env.example`).

Then edit `Caddyfile` and replace `api.your-domain.com` with your real
subdomain from step 2.

## 5. Bring the stack up

```bash
docker compose up -d --build
docker compose logs -f api
```

The `api` service's startup command runs `alembic upgrade head` before
starting uvicorn, so the schema is created automatically — nothing to run
by hand. Watch the logs for "Application startup complete", then Ctrl-C out
of the log tail (the stack keeps running in the background).

Confirm it's reachable over HTTPS (give Caddy a minute on first boot to get
its certificate):

```bash
curl https://api.nextstock.com/health
# {"status":"ok","app":"Recon","env":"production"}
```

## 6. Deploy the frontend and connect the two

Same as `RUNBOOK.md` steps 4–5: deploy `recon-frontend` to Vercel with
`NEXT_PUBLIC_API_URL=https://api.nextstock.com/api/v1`, note the Vercel
URL you get, then SSH back into the VM and set the real `CORS_ORIGINS` in
`.env`:

```bash
# in recon-backend/.env on the VM
CORS_ORIGINS=https://recon.vercel.app
```

```bash
docker compose up -d api   # restarts just the api container to pick up the new .env
```

## 7. Create the first company + admin user

Identical to `RUNBOOK.md` step 6 — one `curl` call to `/auth/register`
against your live domain instead of a Render URL:

```bash
curl -X POST https://api.nextstock.com/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "company_name": "NextStock",
    "company_slug": "nextstock",
    "admin_email": "you@nextstock.com",
    "admin_password": "a-real-password",
    "admin_full_name": "Your Name"
  }'
```

## 8. Wire up real inbound email (optional at first)

Same as `RUNBOOK.md` step 7, with your VM's domain as the webhook target:

- Postmark: `https://<user>:<pass>@api.nextstock.com/api/v1/email/inbound/postmark`
- Mailgun: `https://api.nextstock.com/api/v1/email/inbound/mailgun`

## 9. Keeping it running

This is the part that's free instead of managed — a short list of what
Render would otherwise have done for you:

- **Updates**: `git pull && docker compose up -d --build` redeploys a new
  version. There's no CI gate on this VM itself — run `pytest` locally (or
  let GitHub Actions run it on the repo, per `.github/workflows/ci.yml`)
  *before* pulling onto the VM.
- **Backups**: nothing backs up the `db_data` volume automatically. At
  minimum, cron a nightly `docker compose exec db pg_dump -U recon recon`
  to a file, and copy it off the VM (Oracle Object Storage's Always Free
  tier — 10GB — is a reasonable free place to put it).
- **OS updates**: `sudo apt update && sudo apt upgrade` periodically — Oracle
  doesn't patch the VM's OS for you.
- **Disk space**: `docker system df` and `docker system prune` occasionally
  — old images/build layers accumulate.
- **Monitoring**: nothing alerts you if a container crashes.
  `docker compose ps` shows current status; all `restart: unless-stopped`
  in `docker-compose.yml` means Docker itself will restart a crashed
  container, but nothing tells *you* it happened. A free option: Oracle
  Cloud's own monitoring/alarms on the instance, or an external uptime
  checker (UptimeRobot's free tier) pinging `/health`.

Go back through `DEPLOYMENT.md` for the rest of the hardening checklist
(rate limiting the webhooks, error tracking) before real carrier volume.
