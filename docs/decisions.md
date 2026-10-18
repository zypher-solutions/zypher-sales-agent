# Decisions

Format: what we decided, why, and the trade-off. Newest first.

## 2026-10-18: LLM provider is configurable; MVP runs on free open-weight models via Groq
- **Decision:** All LLM calls go through the `LLM` interface. `LLM_PROVIDER=openai_compat` (default) talks to any OpenAI-compatible API (Groq, Ollama, Together, Mistral, NIM). Claude stays available (`anthropic`). Default models: `openai/gpt-oss-120b`, falling back to `qwen/qwen3.8-27b`.
- **Why:** The owner cannot pay for an API yet, a local model cannot be given to partners or demo clients, and the product must be able to switch provider later without a rewrite. Groq's free tier supports strict JSON-schema output on both models.
- **How quality is protected:** strict schema output, Pydantic validation with one corrective retry, a model chain on rate limits, the verified-quote check in research, the guardrails, and human approval. `scripts/eval_models.py` compares models on real leads (guardrail pass rate, verified hypotheses, blind-rated drafts) so the default is chosen by measurement.
- **Trade-offs:** free tier has no SLA, a 200K tokens/day cap per model (about 14 leads/day per model), and unstated data-handling terms. Not a permanent production foundation: move to a paid tier or a paid host before partners depend on it. Free-tier calls are logged without a cost (no price known).

## 2026-10-18: Cold email is sent from info@zypher-solutions.com (owner's decision, against advice)
- **Decision:** Sender is `info@zypher-solutions.com` for now.
- **Advice given:** use a separate domain and a personal address. Spam complaints and bounces damage the domain that carries the company's real mail, and recovery takes weeks.
- **Mitigations to build in Phase 2 (sender module, not tenant-configurable):** hard daily cap (start at 10/day), automatic pause if bounce rate passes 2% or any spam complaint arrives, SPF/DKIM/DMARC verified before first send, warmup ramp, plain-text first touch with no links.
- **Revisit:** move to a separate domain before volume grows or if any deliverability warning appears. The sender address is configuration, so switching needs no code change.

## 2026-10-17: Tenancy is enforced in the repository layer, suppression is per tenant
- **Decision:** `tenant_id` on every table. A repository is bound to one tenant at construction and scopes every query. Other tenants' ids behave as nonexistent. Suppression and domain uniqueness are per tenant.
- **Why:** hard to forget (no per-call argument), and no auth or row-level security is needed yet.
- **Trade-off:** a global list for hard bounces and spam complaints (useful for deliverability) is deferred to the sender module in Phase 2. A bug in the repository layer would leak across tenants, so isolation has contract tests that run against both implementations.
