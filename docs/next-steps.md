# Where we stopped, and what is next

Read this first in a new chat, then CLAUDE.md and docs/decisions.md. Written 2026-10-20.

## State
- Phase 1 and Phase 2 are built and tested (460 tests on a real Postgres). `.\zypher.ps1 start` runs Telegram buttons,
  sending, inbox reading and follow-ups in one window. Guide: docs/how-to-use.md.
- The database is empty (test data wiped). Real sending is configured in TEST MODE only (`SEND_ALLOWLIST` in `.env`).
- **No real email has ever been sent to a real lead.** The live proof so far: preflight passes, a test email arrived with
  SPF, DKIM and DMARC all PASS, a real reply and a real bounce were handled correctly.
- Free model (Groq gpt-oss-120b, fallback gpt-oss-20b): checks keep drafts safe, but wording quality is only decent.

## Owner decisions still open
1. **Lilacs vs USR:** icp.yaml proof point says "USR, owned by the founder's brother" and forbids naming it. The owner also mentions
   a lighting client "Lilacs" (one-click quotations in their own template from a drawing). Is it the same company? Who owns it?
   What exactly was automated, and is there one real number? Until answered, proof stays "a Dubai company we built it for".
2. Whether to pay for a better model (even $5 on Claude Haiku). `LLM_PROVIDER=anthropic` is one setting.
3. Whether to move cold email to a separate domain (the owner chose the main domain, against advice).

## Build next (suggested order)
1. **First careful live send** (walkthrough was given): approve one card, `SEND_ALLOWLIST` to that address, watch it.
2. **Reply drafting** (Phase 3): classify, draft a reply that agrees first and proposes two time slots, human sends.
3. **Run on a server** (a $5 VPS) so it works with the PC off.
4. **Meetings booked** count (a `/booked` command or calendar link) so stats show real results.
5. CRM or Notion handoff for warm leads, then LinkedIn and WhatsApp (human-assisted).
6. Public-holiday calendar for send windows; a "lead score"; a model comparison (`scripts/eval_models.py`) on real leads.

## Known limits
- JavaScript-only websites show no text and are rejected. Some sites fail on the owner's DNS (blocked address).
- The proof-word gate is a tunable proxy (`target.proof_words` in icp.yaml), not a guarantee.
- Two separate programs (`bot` and `worker`) can rarely show a duplicate card; `start` avoids it.
- Keys shown in earlier chats must be rotated: database password, Telegram token, Groq, Apify and Supadata keys.
