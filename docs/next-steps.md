# Where we stopped, and what is next

Read this first in a new chat, then CLAUDE.md and docs/decisions.md. Updated 2026-10-21.

## State
- Phase 1 and Phase 2 are built and tested (480 tests). `.\zypher.ps1 start` runs Telegram buttons,
  sending, inbox reading and follow-ups in one window. Guide: docs/how-to-use.md.
- **Step 1 (agent on the Oracle server) is in progress.** Plan: docs/plans/step1-server.md. Part 1 (first careful
  live send) is DONE. Part 3 code is DONE and tested on Docker Desktop (2026-10-21): daily drafting schedule, one-copy
  lock (checked live and on Supabase), heartbeat/watchdog/crash alert/Healthchecks ping, Dockerfile, compose, nightly
  backup (backup + restore tested, counts matched), arm64 build. Code review and security review of the diff: not yet.
  **Next: the server**, following docs/runbooks/server-setup.md. Before that the owner: rotates the keys (plan
  Part 2 §1), creates the Oracle account (Pay As You Go + ₹100 budget alert), creates two Healthchecks.io checks,
  optionally a Backblaze B2 bucket. Nothing is committed yet; the repo was 6 commits ahead of GitHub before this.
- Real sending is ON with `SEND_ALLOWLIST` set to one address. **2 real leads emailed so far** (first emails only, no
  replies or bounces yet). The second was the careful live send on 2026-10-21: approved, sent 16 s later inside the
  Dubai window, filed in Sent, lead moved to `in_sequence`. Earlier proof: test email with SPF, DKIM and DMARC all PASS,
  a real reply and a real bounce handled correctly.
- 2026-10-21: the venv was reinstalled after the folder moved to `Desktop\AGENTS\` (it pointed at the old path).
- 2026-10-21 fix: `mailto:` links are now URL-decoded. A site linked `mailto:%20name@...` and the stored address kept
  the `%20`, which would have bounced. The one affected contact was corrected (event `contact_email_corrected`).
- Free model (Groq gpt-oss-120b, fallback gpt-oss-20b): checks keep drafts safe, but wording quality is only decent.

## Owner decisions still open
1. **Lilacs vs USR:** icp.yaml proof point says "USR, owned by the founder's brother" and forbids naming it. The owner also mentions
   a lighting client "Lilacs" (one-click quotations in their own template from a drawing). Is it the same company? Who owns it?
   What exactly was automated, and is there one real number? Until answered, proof stays "a Dubai company we built it for".
2. Whether to pay for a better model (even $5 on Claude Haiku). `LLM_PROVIDER=anthropic` is one setting.
3. Whether to move cold email to a separate domain (the owner chose the main domain, against advice).

## Build next (suggested order)
1. **Run on the Oracle free server** (Step 1, docs/plans/step1-server.md): Part 3 build, then the server runbook.
2. **Reply drafting** (Phase 3): classify, draft a reply that agrees first and proposes two time slots, human sends.
3. Owner decision: when to clear `SEND_ALLOWLIST` (go live at warmup pace, 5 a day).
4. **Meetings booked** count (a `/booked` command or calendar link) so stats show real results.
5. CRM or Notion handoff for warm leads, then LinkedIn and WhatsApp (human-assisted).
6. Public-holiday calendar for send windows; a "lead score"; a model comparison (`scripts/eval_models.py`) on real leads.

## Known limits
- JavaScript-only websites show no text and are rejected. Some sites fail on the owner's DNS (blocked address).
- The proof-word gate is a tunable proxy (`target.proof_words` in icp.yaml), not a guarantee.
- Two separate programs (`bot` and `worker`) can rarely show a duplicate card; `start` avoids it.
- Keys shown in earlier chats must be rotated: database password, Telegram token, Groq, Apify and Supadata keys.
