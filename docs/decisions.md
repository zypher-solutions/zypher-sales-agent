# Decisions

Format: what we decided, why, and the trade-off. Newest first.

## 2026-10-19: Leads come from Google Maps via Apify; emails only from the company's own site
- **Decision:** `zypher_agent source` runs Apify's `compass/crawler-google-places` (places with a website, open only) and imports companies with no email. During research the pipeline takes the best public business address from their own pages (own domain first; careers/no-reply/HR inboxes skipped), checks the domain accepts mail, and checks the do-not-contact list. No usable address: status `nurture`, phone kept for a human.
- **Why:** automation instead of hand-built spreadsheets; public business addresses are the defensible source for B2B outreach; we do not buy Apify's contact enrichment (personal data, extra cost).
- **Trade-offs:** Maps categories are noisy ("general trading company" also returns shops and travel agents); the research agent's disqualifiers and your approval filter them. Sites on shared hosts (netlify.app, base44.app and similar) are skipped because the domain cannot identify the company. Measured live: 30 Dubai companies for about $0.13; 3 of 3 researched leads had a usable public email.

## 2026-10-19: Supabase Data API is locked (RLS on, no policies)
- **Decision:** migration 003 enables row-level security on every table with no policies.
- **Why:** Supabase exposes all `public` tables through its REST API, and the `anon` key is meant to be public. Without RLS, anyone with that key could read or change leads. The agent connects as the table owner, which RLS does not apply to.
- **Trade-off:** none for the agent. When we later add a UI or per-tenant access through the API, we write real policies then.

## 2026-10-19: First touch is question-first and pitches one offer (automation)
- **Decision:** `icp.yaml` now lists three offers (automation, software, branding). Only the one marked `first_touch` is pitched in email 1; the others wait for the discovery call. First touch is 50 to 80 words, ends with a yes/no question offering a one-page sketch ("no call needed"), and must not contain a call request, links or urgency. Prompts and the own-name filter are built from config (no business name in code).
- **Why:** Saad's offer-first method and the cold-email research: a hyper-specific single promise and a tiny ask get replies; three vague services or a meeting request in email 1 do not. Websites and branding are crowded commodity markets where the cheapest wins, so they are follow-ups, not openers.
- **Trade-offs:** a small test set of regexes (call request, links, urgency) can mis-flag odd wording ("a phone call" in an observation); the retry loop and your approval catch that. Offer wording in `icp.yaml` is a draft the owner must confirm. Pricing and the USR proof details are still undecided.

## 2026-10-18: LLM provider is configurable; MVP runs on free open-weight models via Groq
- **Decision:** All LLM calls go through the `LLM` interface. `LLM_PROVIDER=openai_compat` (default) talks to any OpenAI-compatible API (Groq, Ollama, Together, Mistral, NIM). Claude stays available (`anthropic`). Default models: `openai/gpt-oss-120b`, falling back to `openai/gpt-oss-20b`.
- **Why:** The owner cannot pay for an API yet, a local model cannot be given to partners or demo clients, and the product must be able to switch provider later without a rewrite. Groq's free tier supports strict JSON-schema output on both models. **Measured live (2026-10-19):** free tier allows 8K tokens per minute per request on gpt-oss, so research input is capped at about 3.5K tokens (`PROMPT_TEXT_BUDGET`); Qwen 3.8 allows only 1K output tokens per minute and burned 1K on hidden reasoning for one short email, so it was dropped as the fallback.
- **How quality is protected:** strict schema output, Pydantic validation with one corrective retry, a model chain on rate limits, the verified-quote check in research, the guardrails, and human approval. `scripts/eval_models.py` compares models on real leads (guardrail pass rate, verified hypotheses, blind-rated drafts) so the default is chosen by measurement.
- **Trade-offs:** free tier has no SLA, a 200K tokens/day cap per model (about 14 leads/day per model), and unstated data-handling terms. Not a permanent production foundation: move to a paid tier or a paid host before partners depend on it. Free-tier calls are logged without a cost (no price known).

## 2026-10-18: Cold email is sent from the main domain (owner's decision, against advice)
- **Decision:** Sender was `info@zypher-solutions.com`; changed on 2026-10-19 to the founder's own address `muhammedhasheem@zypher-solutions.com` (founder-led outreach: owners reply to owners). Still the main domain.
- **Advice given:** use a separate domain and a personal address. Spam complaints and bounces damage the domain that carries the company's real mail, and recovery takes weeks.
- **Mitigations to build in Phase 2 (sender module, not tenant-configurable):** hard daily cap (start at 10/day), automatic pause if bounce rate passes 2% or any spam complaint arrives, SPF/DKIM/DMARC verified before first send, warmup ramp, plain-text first touch with no links.
- **Revisit:** move to a separate domain before volume grows or if any deliverability warning appears. The sender address is configuration, so switching needs no code change.

## 2026-10-17: Tenancy is enforced in the repository layer, suppression is per tenant
- **Decision:** `tenant_id` on every table. A repository is bound to one tenant at construction and scopes every query. Other tenants' ids behave as nonexistent. Suppression and domain uniqueness are per tenant.
- **Why:** hard to forget (no per-call argument), and no auth or row-level security is needed yet.
- **Trade-off:** a global list for hard bounces and spam complaints (useful for deliverability) is deferred to the sender module in Phase 2. A bug in the repository layer would leak across tenants, so isolation has contract tests that run against both implementations.
