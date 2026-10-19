-- Proof that a company is the kind we serve (an office team or a business-to-business side), with the verbatim
-- quote and page it came from. Shown to the reviewer on the card as "why they fit". Safe to re-run.
alter table research_reports add column if not exists qualification jsonb;  -- [{kind, quote, source_url}]
