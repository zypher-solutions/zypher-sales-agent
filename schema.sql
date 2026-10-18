-- Zypher Sales Agent: Supabase/Postgres schema (current state, includes migrations 001 to 004).
-- Fresh database: run this file only. Existing database: run the numbered files in migrations/ instead.
create extension if not exists "pgcrypto";

create type lead_status as enum (
  'new','enriched','researched','drafted','awaiting_approval','approved',
  'in_sequence','replied','qualified','handed_off','nurture',
  'unsubscribed','bounced','disqualified'
);
create type channel_type as enum ('email','linkedin','whatsapp');
create type message_status as enum (
  'draft','awaiting_approval','approved','rejected','queued','sent',
  'delivered','failed','bounced','needs_changes'
);
create type reply_class as enum (
  'interested','not_now','not_interested','unsubscribe','out_of_office',
  'wrong_person','question','other'
);

-- A tenant is one business using the engine. Zypher is the default tenant.
-- No auth or row-level security yet: isolation is enforced in the repository layer.
create table tenants (
  id uuid primary key default gen_random_uuid(),
  slug text unique not null,
  name text not null,
  sending_paused boolean not null default false,  -- kill switch and automatic stop-loss; only a human clears it
  pause_reason text,
  paused_at timestamptz,
  created_at timestamptz default now()
);
insert into tenants (id, slug, name)
values ('00000000-0000-0000-0000-000000000001', 'zypher', 'Zypher Software Solutions LLP');

create table companies (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null default '00000000-0000-0000-0000-000000000001' references tenants(id),
  name text not null,
  domain text not null,                  -- normalized, e.g. example.com
  website text,
  country text,
  industry text,
  size_hint text,
  notes text,
  source text,                           -- csv, apollo, maps, manual
  created_at timestamptz default now()
);
create unique index companies_tenant_domain_key on companies (tenant_id, domain);

create table contacts (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null default '00000000-0000-0000-0000-000000000001' references tenants(id),
  company_id uuid references companies(id) on delete cascade,
  full_name text,
  role text,
  email text,
  phone text,
  linkedin_url text,
  email_verified boolean default false,
  status lead_status not null default 'new',
  score int,
  legacy_lead_id text,                   -- LeadID from the old Google Sheet
  created_at timestamptz default now(),
  unique (company_id, email)
);
create index contacts_tenant_status_idx on contacts (tenant_id, status);

create table research_reports (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null default '00000000-0000-0000-0000-000000000001' references tenants(id),
  company_id uuid references companies(id) on delete cascade,
  pages_fetched jsonb,                   -- [{url, fetched_at, text_hash}]
  services text[],
  tech_signals text[],
  workflow_hypotheses jsonb,             -- [{hypothesis, evidence, confidence}]
  summary text,
  model text,
  disqualify_reason text,
  decision_maker text,
  public_emails text[],
  created_at timestamptz default now()
);

create table campaigns (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null default '00000000-0000-0000-0000-000000000001' references tenants(id),
  name text not null,
  icp_version text,
  offer text,
  autonomy_level int not null default 0 check (autonomy_level between 0 and 3),
  active boolean default true,
  created_at timestamptz default now()
);

create table sequences (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null default '00000000-0000-0000-0000-000000000001' references tenants(id),
  campaign_id uuid references campaigns(id) on delete cascade,
  step int not null,
  channel channel_type not null,
  delay_days int not null default 0,
  unique (campaign_id, step)
);

create table messages (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null default '00000000-0000-0000-0000-000000000001' references tenants(id),
  contact_id uuid references contacts(id) on delete cascade,
  campaign_id uuid references campaigns(id),
  step int not null default 1,
  channel channel_type not null,
  direction text not null default 'outbound' check (direction in ('outbound','inbound')),
  subject text,
  body text,
  evidence jsonb,                        -- facts used, for audit
  version int not null default 1,
  status message_status not null default 'draft',
  feedback text,                         -- human edit instructions
  reply_class reply_class,
  provider_message_id text,
  telegram_chat_id bigint,               -- where the approval message lives, so button presses map back
  telegram_message_id bigint,
  message_id_header text,                -- the Message-ID we generated; replies are matched on it
  in_reply_to text,                      -- Message-ID of the previous email in the thread (follow-ups)
  send_after timestamptz,                -- do not send before this (deferred by window, cap or spacing)
  send_attempts int not null default 0,
  last_error text,
  sent_from text,                        -- the mailbox it left from
  scheduled_at timestamptz,
  sent_at timestamptz,
  created_at timestamptz default now()
);
create index messages_tenant_status_idx on messages (tenant_id, status, scheduled_at);
create index messages_telegram_idx on messages (telegram_chat_id, status);
create unique index messages_message_id_header_key on messages (tenant_id, message_id_header) where message_id_header is not null;
create index messages_send_queue_idx on messages (tenant_id, status, send_after);

-- Suppression is per tenant: opting out of one business is not opting out of another.
create table suppression_list (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null default '00000000-0000-0000-0000-000000000001' references tenants(id),
  email text,
  domain text,
  phone text,
  reason text not null,                  -- unsubscribe, bounce, complaint, manual
  created_at timestamptz default now(),
  check (email is not null or domain is not null or phone is not null)
);
create unique index suppression_tenant_email_key  on suppression_list (tenant_id, lower(email))  where email is not null;
create unique index suppression_tenant_domain_key on suppression_list (tenant_id, lower(domain)) where domain is not null;

create table events (
  id bigserial primary key,
  tenant_id uuid not null default '00000000-0000-0000-0000-000000000001' references tenants(id),
  contact_id uuid,
  message_id uuid,
  type text not null,                    -- approved, edited, sent, bounced, replied, llm_call...
  actor text,                            -- agent, human:<id>, system
  payload jsonb,
  created_at timestamptz default now()
);
create index events_tenant_contact_idx on events (tenant_id, contact_id, created_at);

-- Close Supabase's public Data API over these tables (RLS on, no policies). The agent connects as the
-- table owner, which RLS does not apply to. See migrations/003_lock_down_data_api.sql.
alter table tenants          enable row level security;
alter table companies        enable row level security;
alter table contacts         enable row level security;
alter table research_reports enable row level security;
alter table campaigns        enable row level security;
alter table sequences        enable row level security;
alter table messages         enable row level security;
alter table suppression_list enable row level security;
alter table events           enable row level security;
