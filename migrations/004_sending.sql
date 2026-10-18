-- Phase 2: sending. Run once in the Supabase SQL editor (or via the migration runner). Safe to re-run.

-- What we need to send, thread, retry and audit one email.
alter table messages add column if not exists message_id_header text;     -- the Message-ID we generated; replies are matched on it
alter table messages add column if not exists in_reply_to text;           -- the Message-ID of the previous email in the thread (follow-ups)
alter table messages add column if not exists send_after timestamptz;     -- do not send before this (deferred by window, cap or spacing)
alter table messages add column if not exists send_attempts int not null default 0;
alter table messages add column if not exists last_error text;
alter table messages add column if not exists sent_from text;             -- the mailbox it left from

-- A Message-ID identifies exactly one message: guards against recording the same send twice.
create unique index if not exists messages_message_id_header_key
  on messages (tenant_id, message_id_header) where message_id_header is not null;
create index if not exists messages_send_queue_idx on messages (tenant_id, status, send_after);

-- Kill switch and automatic stop-loss. Only a human clears it.
alter table tenants add column if not exists sending_paused boolean not null default false;
alter table tenants add column if not exists pause_reason text;
alter table tenants add column if not exists paused_at timestamptz;
