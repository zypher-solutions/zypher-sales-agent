# Step 1: Sales Agent runs 24/7 on the Oracle server

On approval this file is copied to `zypher-sales-agent/docs/plans/step1-server.md`.

## Context
Today the agent only runs while `.\zypher.ps1 start` is open on the laptop. Step 1 moves it into Docker on the Oracle free ARM server. There it restarts by itself, drafts on a schedule, gets backed up nightly, and alerts the owner when it is down. Telegram approvals stay exactly as they are. The Console (Step 2) replaces them later. No UI is built and approval behaviour does not change.

**Found while reading (facts, checked 2026-10-21):**
- The venv is broken: `.venv` points at the old folder `Desktop\projects\zypher-sales-agent\src`, so `python -m zypher_agent` fails with "No module named zypher_agent". `zypher.ps1` is broken for the same reason. Fix: reinstall in editable mode.
- `.env` is **live**: `SENDING_ENABLED=true` with `SEND_ALLOWLIST` empty. The database has 1 company emailed (owner confirmed it was his send), 3 cards awaiting approval and 4 new leads. `docs/next-steps.md` is out of date and gets corrected.
- The database URL uses Supabase's **session pooler on port 5432**. That matters: session-level advisory locks work there (used for the single-copy guard below). They would not work on the transaction pooler (6543).
- `.env.example` has `SMTP_HOST`/`SMTP_PORT` twice. The second block (`SMTP_PORT=587`, empty host) would override the GoDaddy values for anyone copying the example. It also lists about 15 keys the code never reads (Slack, Notion, WhatsApp, Apollo, old send-policy keys).
- `config.REPO_ROOT` is derived from the package's file location, so a normal (non-editable) `pip install` inside Docker would lose `icp.yaml` and `assets/`. The image installs in editable mode from `/app`, which keeps the paths unchanged.

## Owner decisions (asked 2026-10-21)
- Daily drafting: **10 leads**, **Mon–Fri at 08:00 IST**. Trade-off: the warmup sends only 5 a day in week 1 and 8 in week 2, so cards will queue. `MAX_OPEN_CARDS=10` already keeps Telegram to 10 at once, and drafts waiting days are fine because `refresh` exists.
- Sending days are unchanged: they already follow each lead's own working days and hours (`sender/policy.py`).
- Paid Apify sourcing runs **only on command**. It never runs on a schedule.
- **Claude runs the server commands** through an SSH session in the owner's Terminal panel, one command at a time, each one approved. The owner answers questions when asked. Secrets are never typed or shown in that session (see Secrets).

---

## Part 1: First careful live send (laptop, before any server work)
Claude stops and confirms with the owner after each step. Any failure gets fixed before Part 2.

0. **Repair the venv:** `.\.venv\Scripts\python.exe -m pip install -e ".[dev]"`, then `.\zypher.ps1 test` (460 tests) and `.\zypher.ps1 status`.
1. **Allowlist one address.** The owner picks one of the 3 waiting cards. In `.env` he sets `SEND_ALLOWLIST=<that lead's address>` himself (Claude never edits `.env`), then runs `.\zypher.ps1 preflight`, which must show all PASS. This immediately stops anything else from going out while live.
2. **Approve one card.** Run `.\zypher.ps1 start`. In Telegram: Approve → Yes. Check that the card states when the email goes out (next Dubai window: 9:00–11:30 or 13:00–16:00 Dubai time, Mon–Fri). Rejecting or approving another card outside the allowlist must be refused at once.
3. **Watch the send and the inbox handling.** Check each of these:
   - The terminal shows `sent 1`.
   - Telegram shows "✅ Sent to …".
   - The copy is in the webmail **Sent** folder.
   - No bounce arrives in the next hour. SPF/DKIM/DMARC were already proven PASS by the earlier test mail, and the lead's own copy can't be inspected.
   - Over the next cycles, the `inbox:` lines show no "cannot read the mailbox".
   - The lead moves to `in_sequence` in `status`.
4. **Check the stats.** `.\zypher.ps1 stats` and `/stats` show 2 companies emailed. `/status` and `/queue` match.

Then the owner decides whether to clear the allowlist, which means going live at warmup pace. The server image runs with `SENDING_ENABLED=false` until Part 3's server cut-over.

---

## Part 2: Design

### 1. Secrets
**Rotate before the server** (the owner does every step himself). For each key: rotate, update the laptop `.env`, run `.\zypher.ps1 preflight` / `status` to confirm, then continue.

| Key | How |
|---|---|
| Database password (`DATABASE_URL`) | Supabase → Project Settings → Database → Reset database password; paste the new URI (session pooler, port 5432) |
| `TELEGRAM_BOT_TOKEN` | @BotFather → `/revoke` → choose the bot → new token |
| `LLM_API_KEY` (Groq) | console.groq.com → API Keys → delete old, create new |
| `APIFY_TOKEN` | Apify Console → Settings → API & Integrations → reset token |
| `SUPADATA_API_KEY` | The code never uses it: **delete the key** at Supadata and remove the line |
| `SLACK_BOT_TOKEN`, `NOTION_TOKEN` | Unused by this agent and Slack is retired: revoke them at the provider and remove the lines |
| `MAIL_PASSWORD` | Recommended too, since it goes onto a server: change it in GoDaddy/Titan webmail |

**`.env` on the server:**
- **Location:** `/opt/zypher/zypher-sales-agent/.env`, owner `ubuntu`, permissions `chmod 600`. Only that user can read or write it; other users and processes on the server cannot.
- **How it gets there:** Claude runs `scp .env` from the laptop. The file is copied, never printed. It is never baked into the image (`.dockerignore` excludes it). Docker reads it at container start (`env_file`).
- **What this protects against:** anyone with root on the server, or `docker inspect`, can still see the values. That is why SSH is key-only and the keys are rotated.

**`.env.example` tidy:**
- Remove the duplicate `SMTP_*` block and the keys the code never reads.
- Add the new Step 1 keys.
- A new test, `tests/test_env_example.py`, checks that it has no duplicate keys and that every `Settings` field is listed. This tidy cannot change loading, because `Settings` ignores unknown keys and reads only its own fields.

### 2. Docker
**`Dockerfile`** (each line gets a one-line comment in the file):
- **Base:** `FROM python:3.13-slim-bookworm`. This is the same Python as the laptop venv, and the official image is multi-arch, so it builds `linux/arm64` on the server natively.
- **Environment:** `ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1`, so logs appear at once and the image stays small.
- **Code and install:** `WORKDIR /app`, then copy `pyproject.toml`, `src/`, `icp.yaml`, `assets/`, then `pip install -e .` (editable, so `REPO_ROOT` = `/app`).
  - Every dependency ships arm64 wheels, including `psycopg[binary]`, so no compiler is needed and a single build stage is enough. The build verifies this.
- **User:** `RUN useradd --uid 10001 --create-home agent && mkdir /data && chown agent /data`, then `USER agent`. The container runs as non-root.
- **Health:** `HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 CMD python -m zypher_agent health`
- **Command:** `CMD ["python", "-m", "zypher_agent", "start"]`
- **Expected size:** about 150–200 MB.

**`.dockerignore`:** `.env`, `.venv`, `.git`, `tests`, `docs/legacy`, `*.csv`, `eval_out`, caches.

**`docker-compose.yml`** (local runs and, for now, the server):
- **Service `sales-agent`:**
  - Startup: `build: .`, `env_file: .env`, `restart: unless-stopped`, `init: true` (passes Ctrl+C/stop signals cleanly), `stop_grace_period: 30s`.
  - Storage: volume `agent-state:/data` for the heartbeat and restart log.
  - Logs: `logging: json-file, max-size 10m, max-file 5`, which caps them at 50 MB.
  - `environment: HEARTBEAT_DIR=/data, AGENT_HOST_LABEL=${AGENT_HOST_LABEL:-docker}`.
- **Service `backup`:** see section 6.
- **Laptop overrides:** `docker-compose.local.yml` sets `SENDING_ENABLED=false` and `DRAFT_SCHEDULE_ENABLED=false` for Docker Desktop tests.

This compose file later moves into `zypher-console/deploy/` (hosting.md). Nothing in it is server-specific, so the move is a copy.

### 3. Scheduling: inside the `start` loop, driven by config (recommended)
**Why not a scheduler container:** a separate container would need a second copy of the pipeline, its own lock and its own database connections. `start` already runs follow-ups every 30 minutes in the same loop, so the schedule is one more check in that loop. The trade-off: a 10-lead draft (about 10–20 minutes on the free model) delays the sending/inbox cycle for that long. That is harmless, because sends are paced minutes apart and the inbox is read again right after.

- **New `src/zypher_agent/schedule.py`:** a pure function `draft_due(now, last_run_local_date, cfg) -> bool`. It returns true on a configured weekday, at or after the configured time, if there has been no run yet today. If the server was down at 08:00 and comes back at 11:00, it catches up **the same day only**, never yesterday's run.
- **Last run** is stored as an event `scheduled_draft` in the existing `events` table, so there is no schema change. A restart, or the laptop and server, can never draft twice in one day.
- **New settings** (code default → server `.env`):
  - `DRAFT_SCHEDULE_ENABLED` (false → true)
  - `DRAFT_DAILY_LIMIT` (5 → 10)
  - `DRAFT_TIME` (08:00)
  - `DRAFT_DAYS` (mon,tue,wed,thu,fri)
  - `DRAFT_TIMEZONE` (Asia/Kolkata)
- **Sourcing stays manual.** There is no schedule setting for it on purpose. Until the Console's "Find leads" button (Step 3a), it runs with `zypher.ps1 find` on the laptop (it only writes to the database, so it is safe while the server runs) or with `docker compose exec sales-agent python -m zypher_agent source ...` on the server.
- **`zypher.ps1` keeps working unchanged.** It gets one new command, `health`, and its help text mentions the lock.

### 4. Health, restarts, crash alerts
**`src/zypher_agent/health.py`:**
- **Heartbeats:** the main loop and the Telegram thread each touch their own file in `HEARTBEAT_DIR` on every iteration, whether or not Telegram or the database is reachable. A heartbeat means "not hung", not "all good". The Telegram long poll returns at least every 30 seconds, so its file stays fresh while idle. Pipeline progress lines also touch the main file during long drafts.
- **`python -m zypher_agent health`** exits 1 if the main heartbeat is older than 45 minutes or the Telegram heartbeat is older than 10. Docker's `HEALTHCHECK` uses it, and `docker compose ps` shows `healthy`/`unhealthy`.
- **Watchdog thread:** plain Docker does **not** restart an "unhealthy" container; only Swarm or Kubernetes do. So a watchdog thread inside `start` applies the same thresholds. On a stall it logs, sends a Telegram alert, then `os._exit(1)`, and `restart: unless-stopped` brings the container back. This approach was chosen over an "autoheal" container because that would need the Docker socket, which gives root on the server.
- **Crash-loop alert** (temporary, until the Console):
  - Each start appends a timestamp to `/data/starts`.
  - An unhandled exception writes its type and message to `/data/last_error` with secrets masked, using the existing `SecretStr` pattern so no URL or token is ever written.
  - At start, if there were 3 or more starts in 30 minutes, it sends at most one Telegram alert per hour: "Restarted N times in 30 min on <host>. Last error: …". If Telegram itself is the problem, the off-server monitor (section 7) still catches it.
- **Log rotation** is set in compose (above). The existing `print` lines are the log, and `docker compose logs -f sales-agent` reads them.

### 5. Server setup guide: `docs/runbooks/server-setup.md`
Plain steps, each with the command, what you should see, and what to do if not. Claude runs the server commands over SSH in the owner's Terminal panel; the owner does the browser steps.

1. **Oracle signup:** home region Mumbai (or Hyderabad); it cannot be changed later. Then **upgrade to Pay As You Go** (recommended):
   - Why: Oracle reclaims Always Free instances whose 95th-percentile CPU, network and memory all stay under 20% for 7 days, and this agent is nearly idle. PAYG accounts are exempt and still pay ₹0 within Always Free limits.
   - Safety: set a **budget alert at ₹100** first.
   - Verified against Oracle's Always Free docs; the PAYG exemption comes from Oracle's 2023 notice. The owner confirms it on the upgrade page.
2. **Create the instance:** Ampere A1 Flex, 2 OCPU / 12 GB, Ubuntu 24.04, boot volume 50 GB, public IPv4.
3. **SSH key from Windows:**
   - `ssh-keygen -t ed25519 -f $HOME\.ssh\zypher_oracle` (the owner types the passphrase).
   - Paste the `.pub` file in the Create form.
   - Add a `Host zypher` entry to `~/.ssh/config`.
4. **Ubuntu basics:**
   - `sudo apt update && sudo apt full-upgrade -y`.
   - Install `unattended-upgrades` with automatic reboot at 03:30 IST. This also tests the "comes back after reboot" rule weekly.
   - Set the timezone to Asia/Kolkata and disable SSH password login.
5. **Firewall:**
   - This agent only makes outgoing connections, so only port 22 is needed now. The Security List already allows 22.
   - Ubuntu's iptables on Oracle images is explained here (`/etc/iptables/rules.v4`), but 80/443 open in Step 2 together with Caddy, never earlier.
6. **Docker:** install Docker Engine and the compose plugin from Docker's official apt repository for Ubuntu (arm64). Add `ubuntu` to the `docker` group and confirm `systemctl is-enabled docker` = enabled, so it starts on boot.
7. **Project:**
   - Create a read-only **deploy key** on the server and add it to the GitHub repo (Settings → Deploy keys).
   - `git clone` into `/opt/zypher/zypher-sales-agent`.
   - The laptop pushes first; the repo is 6 commits ahead. That push needs the owner's OK.
8. **`.env` and first start:**
   - `scp` the `.env`, then `chmod 600`.
   - Set `SENDING_ENABLED=false` for the first 24 hours, then `docker compose up -d --build`.
   - Check `docker compose ps` and `docker compose logs -f sales-agent`.
   - Confirm that a Telegram card arrives (the laptop is closed).
9. **Cut-over:**
   - Stop `start` on the laptop.
   - Set `SENDING_ENABLED=true` on the server, then `docker compose up -d`.
10. **Deploying updates:** `git pull && docker compose up -d --build sales-agent`.
11. **"Out of capacity":**
    - Retry at off-peak hours (early morning IST).
    - Try another availability domain if the region has more than one.
    - Try 1 OCPU / 6 GB first and resize later.
    - PAYG accounts usually get capacity more easily.
    - Don't use auto-retry scripts that hammer the API.

### 6. Backups (`deploy/backup/`, portable)
- **Container:** a small `backup` service built from `postgres:17-alpine` (official, arm64) plus `rclone`. Only `DATABASE_URL`, `BACKUP_REMOTE` and `HEALTHCHECKS_BACKUP_URL` are passed to it through compose variables; it does not get the whole `.env`.
- **`backup.sh`, nightly at 02:30 IST:**
  - Runs `pg_dump --format=custom --no-owner --schema=public` to `/backups/zypher-internal-YYYY-MM-DD.dump`.
  - Deletes files older than 14 days.
  - Copies the folder to an **off-server** bucket with `rclone sync`.
  - Pings Healthchecks.io on success, or `/fail` on error.
- **Off-server target:** Backblaze B2.
  - Why: 10 GB free, no card, and a different company from Oracle, so losing the Oracle account doesn't lose the backups.
  - The dump is a few MB, so 14 copies fit easily. If `BACKUP_REMOTE` is empty, it keeps local copies only and warns in the log.
  - Assumed; the owner confirms the free tier at signup.
- **Check:** pg_dump 17 can dump Postgres 15 or 17. During the build, Claude runs `select version()` to confirm the server version.
- **Restore** (documented in the runbook and **tested once in Part 3** into a throwaway local Postgres container):
  `pg_restore --clean --if-exists --no-owner --dbname "$DATABASE_URL" <file>`, then `.\zypher.ps1 status` to compare counts.
- `--schema=public` covers all agent tables. Supabase's own `auth`/`storage` schemas are not ours. The `ops` schema gets added to the dump in Step 2, which is a one-flag change.

### 7. Uptime monitoring: Healthchecks.io (free plan, 20 checks)
The agent has no web address, so a URL checker like UptimeRobot has nothing to ping until the Console exists. A **dead-man's switch** fits a background worker: the agent calls out, and silence raises the alarm. It also catches a dead server, which the Telegram crash alert cannot.

- **Check 1, `sales-agent`:** the main loop pings `HEALTHCHECKS_URL` every 5 minutes, but only after a cycle that reached the database. Period 5 minutes, grace 20 minutes. Alerts by email, plus Telegram through Healthchecks' own integration (a separate bot, so it works even if ours is the problem).
- **Check 2, `backup`:** period 1 day, grace 2 hours.
- **Later:** UptimeRobot (free plan, 5-minute checks) for `console.<domain>/api/health` from Step 2. Its free-plan commercial-use terms changed in 2025–26, and sources conflict, so the owner confirms them at signup.
- Verified: Healthchecks.io's pricing page shows 20 free checks.

### 8. One Telegram poller only
- **Guard 1, database lock:**
  - `start`, `bot` and `worker` take a Postgres session advisory lock (`pg_try_advisory_lock`, keyed on tenant) on a dedicated connection. This needs no schema change.
  - If another copy holds the lock, the new copy **waits** instead of exiting. It prints a message every minute ("Another copy is running on <host>. Stop it first.") and sends one Telegram notice. Telegram polling, sending and drafting start only once it holds the lock.
  - The waiting copy keeps its heartbeat fresh, so it never crash-loops. This means a laptop `start` can't run alongside the server: it just waits.
  - If the lock connection drops and the lock can't be re-taken, the process exits, then restarts and waits.
  - The host label comes from `AGENT_HOST_LABEL` (default: the computer's name).
- **Guard 2, belt and braces:** Telegram's `getUpdates` returns HTTP 409 "Conflict" when two pollers run at once. `poll_forever` detects it, logs `telegram_conflict` and alerts once.
- **Docs:** README, how-to-use and `zypher.ps1 start` help say in one line: never run `start` on the laptop while the server runs. Use `zypher.ps1 status/stats/find` freely; they don't poll.

### 9. Risks and rollback
| Risk | Mitigation |
|---|---|
| Oracle reclaims or stops the free instance | PAYG upgrade; Healthchecks alert within 25 min; rebuild from runbook + B2 backup in about an hour |
| Oracle account lost entirely | Backups live at Backblaze; code on GitHub; `.env` on laptop |
| Supabase free project pauses | Agent activity every minute keeps it active |
| Bad deploy | `git checkout <previous tag> && docker compose up -d --build`. The release is tagged before each deploy |
| Duplicate sends from two copies | Advisory lock, plus the existing "never send twice" checks in the worker |

**Rollback to the laptop (5 minutes):**
1. On the server: `docker compose stop sales-agent`, which releases the lock.
2. On the laptop: `.\zypher.ps1 start`.

All state is in Supabase, so nothing is lost. If the server is unreachable, the lock is released when its connection dies (TCP keepalive, about 1–2 minutes), and the laptop copy takes over by itself.

---

## Part 3: Build order (after approval; tests first for scheduling, locking and sending)
1. Fix the venv, run the baseline tests and lint, then do Part 1.
2. `.env.example` tidy + `test_env_example.py`.
3. `schedule.py` + `tests/test_schedule.py`: weekday, time, catch-up same day only, once per day across restarts (fake repo events), disabled flag.
4. Lock + 409 handling + `tests/test_single_instance.py`: second copy waits, lost lock exits, 409 alerts once. Real-Postgres tests follow the existing `test_db.py` pattern.
5. `health.py` + watchdog + crash-loop + Healthchecks ping: `tests/test_health.py` with a fake clock and temporary directories.
6. Wire into `_send(... with_bot=True)` in `__main__.py`. Reuse the existing `_live()` pipeline (today's `followups` object) for scheduled drafting. Add the `health` command and `zypher.ps1 health`.
7. Dockerfile, `.dockerignore`, compose files, `deploy/backup/`.
8. **Local Docker Desktop test** with `SENDING_ENABLED=false`:
   - Build, run, confirm `healthy`.
   - Confirm a Telegram card arrives and the laptop `start` waits on the lock.
   - Kill the main loop (test hook) and watch the restart and crash alert.
   - Run a backup + restore into a throwaway local Postgres.
   - Run `docker buildx build --platform linux/arm64` to prove the arm64 build.
9. Full test suite + ruff, then a code review and a security review of the diff.
10. Docs:
    - README (roadmap), CLAUDE.md "Current phase" + Commands.
    - `docs/how-to-use.md`: new section "Running on the server".
    - `docs/runbooks/server-setup.md`.
    - `docs/decisions.md` (in-loop schedule; advisory-lock guard; watchdog instead of autoheal; Healthchecks instead of UptimeRobot for now; B2 off-site; PAYG).
    - `docs/next-steps.md`, correcting the "no real email sent" line.
11. Server: follow the runbook (Claude drives SSH in the owner's terminal, one approved command at a time). First 24 hours with `SENDING_ENABLED=false`, then cut over.
12. Commit only when the owner asks.

## Verification (done when)
- Tests and lint are clean. Docker Desktop run is healthy, and the arm64 build passes.
- On the server: `docker compose ps` shows `healthy`. With the laptop off, a scheduled 08:00 Mon–Fri draft produces Telegram cards, and approvals send in the lead's window.
- `sudo reboot` → the agent is back and healthy within 3 minutes, with no manual step.
- A laptop `start` waits on the lock while the server runs.
- The nightly backup file appears locally and in B2, Healthchecks shows both checks green, and one restore test has passed.
- 7 consecutive days on the server without the owner starting anything (the Healthchecks history shows it).
