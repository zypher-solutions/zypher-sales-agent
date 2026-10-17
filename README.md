# Zypher Sales Agent

An AI-assisted outbound sales system for **Zypher Software Solutions LLP**. It finds SME leads, researches each business, drafts personalized outreach around **business-process automation**, sends it through compliant channels, handles replies, and hands warm leads to a human.

Owner: Muhammed Hasheem (CEO / technical lead)
Status: Phase 0, planning and foundation. Replaces the n8n workflow "Cold Outreach - Phase 1".

---

## 1. Why we are rebuilding Phase 1

Phase 1 proved the loop (sheet -> website -> LLM draft -> Telegram approval -> SMTP send). It does not scale or sell our new positioning.

| Problem in Phase 1 | Consequence |
|---|---|
| Pitch is "improve your website" | Zypher now leads with **business automation**; web/app/CRM is secondary |
| Research reads only `<title>` and meta description | Emails are shallow; no real insight into workflows or pain |
| `llama3:8b` locally, with regex cleanup of its output | Inconsistent quality; brittle post-processing of "Here is the email..." |
| Google Sheet is the database | No history, no dedupe, race conditions, no analytics |
| No follow-ups, no reply handling | Most replies come from follow-ups 2 to 4 |
| No suppression/unsubscribe list, no opt-out footer | Legal exposure and spam-complaint risk |
| Sending from the main company domain, no rate limits | One bad batch can damage `zypher-solutions.com` deliverability |
| Single channel, single human approval path | Cannot add LinkedIn/WhatsApp or move to auto-send safely |
| Hardcoded chat IDs, no tests, no logs | Hard to debug or hand to a partner |

## 2. Goals and non-goals

**Goals (v1)**
- Source and enrich SME leads in UAE, India, Qatar, and US.
- Research each lead (site, services, tech signals, likely manual workflows) and produce a grounded "automation opportunity" hypothesis.
- Draft outreach for **email, LinkedIn (human-assist), WhatsApp**, with multi-step follow-up sequences.
- Classify replies (interested / not now / unsubscribe / out-of-office / wrong person) and route them.
- Staged autonomy: human approval first, auto-send only after quality gates are met.
- Push qualified leads into the **Notion CRM** and notify via **Slack** (the partners' existing workflow).
- Full audit trail and metrics in Postgres.

**Non-goals (v1)**
- Voice / calling agent (Phase 4).
- Fully automated LinkedIn actions (connection requests/DMs through bots risks account bans; we draft, a human sends).
- Large enterprises.
- Building a general-purpose CRM.

## 3. Architecture

```
          +-----------------+
 Sources  | Lead import     |  CSV / Apollo / Maps / manual
          +--------+--------+
                   v
          +-----------------+      +--------------------+
          | Enrichment &    |----->| Supabase (Postgres)|  source of truth
          | validation      |      | leads, messages,   |
          +--------+--------+      | events, suppression|
                   v               +---------+----------+
          +-----------------+                ^
          | Research agent  |  scrape site, extract services,
          | (Claude + tools)|  tech stack, workflow pain hypotheses
          +--------+--------+
                   v
          +-----------------+
          | Drafting agent  |  per channel, per sequence step
          +--------+--------+
                   v
          +-----------------+      Telegram / Slack approval (Level 0-1)
          | QA + guardrails |----> or auto-send (Level 2+)
          +--------+--------+
                   v
          +-----------------+
          | Sender / queue  |  rate limits, send windows, warmup
          +--------+--------+
                   v
          +-----------------+
          | Reply handler   |  classify -> Notion CRM + Slack
          +-----------------+
```

**Stack**
- Python 3.11+, FastAPI, Pydantic
- Claude API with tool use for research/drafting/classification (model configurable via env)
- Supabase Postgres as the single source of truth
- Background jobs: start with a simple worker plus Postgres job table; move to Celery/RQ only if needed
- n8n kept only for glue (Notion/Slack triggers), not for core logic
- Delivery: Google Workspace/SMTP or a cold-email platform (decision pending, see section 8)

## 4. Autonomy levels (staged)

| Level | Behavior | Promotion criteria |
|---|---|---|
| 0 | Agent drafts, human approves/edits every message | Starting point |
| 1 | Human approves only first touch; follow-ups auto-send | 100+ approved sends, under 10% edited heavily |
| 2 | Auto-send to leads scoring above a threshold; human reviews the rest | Bounce under 2%, spam complaints under 0.1%, no policy violations |
| 3 | Fully autonomous within guardrails; human reviews warm replies only | Sustained 4+ weeks at Level 2 with clean metrics |

Replies that are anything other than clear negatives always go to a human.

## 5. Compliance and deliverability (non-negotiable)

- **Separate sending domain(s)** for cold email (not the main company domain), with SPF, DKIM, DMARC, and warmup.
- Daily caps per mailbox, ramped gradually; send only in the recipient's business hours.
- Every email carries a clear opt-out and the sender's identity and postal address. Unsubscribes are honored immediately via the suppression list.
- Regional rules: CAN-SPAM (US), GDPR/ePrivacy where relevant, UAE and Qatar data and anti-spam rules, India's DPDP Act and TRAI rules for messaging. **Verify current rules per country before launch.**
- **WhatsApp:** use the official WhatsApp Business API with approved templates; no cold-blasting from personal numbers. Prefer WhatsApp for leads that have engaged or provided a number publicly for business.
- **LinkedIn:** the agent prepares research and drafts; a human sends.
- The agent never invents facts about a lead. Claims must trace to scraped evidence stored with the draft.

## 6. Repository layout (target)

```
zypher-sales-agent/
  README.md
  CLAUDE.md            # instructions for AI coding sessions on this repo
  pyproject.toml
  .env.example
  schema.sql           # Supabase schema
  icp.yaml             # ideal customer profile + offer + messaging rules
  scripts/import_leads.py
  src/zypher_agent/
    config.py
    db.py
    research/          # scraping + analysis agent
    drafting/          # per-channel drafting
    guardrails/        # QA checks, banned claims, compliance
    channels/          # email.py, whatsapp.py, linkedin.py
    replies/           # classification + routing
    integrations/      # notion.py, slack.py, telegram.py
    worker.py
  tests/
  docs/legacy/Cold Outreach - Phase 1.json
```

## 7. Roadmap

| Phase | Scope |
|---|---|
| 0 | Plan, schema, ICP, import existing sheet into Postgres (this repo's current state) |
| 1 | Research agent + email drafting + Telegram approval (parity with n8n, better quality) |
| 2 | Sending infrastructure: sending domain, warmup, queue, rate limits, suppression, follow-up sequences |
| 3 | Reply classification, Notion CRM + Slack handoff, metrics dashboard |
| 4 | WhatsApp Business API + LinkedIn assist; staged autonomy promotion |
| 5 | Voice agent, A/B testing, lead scoring from outcomes |

## 8. Open decisions

1. Lead sources and budget (Apollo, Apify/Google Maps scraping, manual lists).
2. Email delivery: own mailboxes on a separate domain vs Instantly/Smartlead-style platform.
3. Sending domain name and number of mailboxes.
4. Notion CRM database structure (needs a shared example).
5. Case studies / proof points we may legally cite in emails.
6. Pricing and offer: free workflow audit? fixed-price pilot?
7. Hosting for the agent (VPS, Railway, Fly.io).

## 9. Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
pip install -e ".[dev]"  # includes pytest
cp .env.example .env     # fill in values
# Run schema.sql, then migrations/001_phase1.sql, in the Supabase SQL editor

pytest                                        # all tests, no keys needed
python -m zypher_agent run --dry-run          # whole pipeline with fake sites + mocked LLM
python scripts/import_leads.py leads.csv --dry-run
python scripts/import_leads.py leads.csv      # real import (needs DATABASE_URL)
python -m zypher_agent run --limit 5          # research + draft new leads, cards to Telegram
python -m zypher_agent bot                    # listen for Approve / Suggest changes / Reject
```

To run the Postgres contract tests, point `TEST_DATABASE_URL` at a throwaway database (they delete rows where `source = 'test'`).

## 10. Security notes

- Never commit `.env`. Rotate any credentials that were exposed in shared workflow exports.
- Telegram/Slack approvals must verify the sender's user ID against an allowlist.
- Scraped content is untrusted data and must never be treated as instructions by the agents (prompt-injection risk from lead websites).
