-- Phase 1 additions. Run once in the Supabase SQL editor AFTER schema.sql. Safe to re-run.

-- A message the human asked to change, waiting for their feedback text.
alter type message_status add value if not exists 'needs_changes';

-- Where the approval message lives in Telegram, so button presses map back to a draft.
alter table messages add column if not exists telegram_chat_id bigint;
alter table messages add column if not exists telegram_message_id bigint;

-- Research fields the agent produces beyond the v0 table.
alter table research_reports add column if not exists disqualify_reason text;
alter table research_reports add column if not exists decision_maker text;
alter table research_reports add column if not exists public_emails text[];

create index if not exists messages_telegram_idx on messages (telegram_chat_id, status);
