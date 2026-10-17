-- Zypher Sales Agent: Supabase/Postgres schema (v0)
create extension if not exists "pgcrypto";

create type lead_status as enum (
  'new','enriched','researched','drafted','awaiting_approval','approved',
  'in_sequence','replied','qualified','handed_off','nurture',
  'unsubscribed','bounced','disqualified'
);
create type channel_type as enum ('email','linkedin','whatsapp');
create type message_status as enum (
  'draft','awaiting_approval','approved','rejected','queued','sent',
  'delivered','failed','bounced'
);
create type reply_class as enum (
  'interested','not_now','not_interested','unsubscribe','out_of_office',
  'wrong_person','question','other'
);

create table companies (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  domain text unique not null,          -- normalized, e.g. example.com
  website text,
  country text,
  industry text,
  size_hint text,
  notes text,
  source text,                           -- csv, apollo, maps, manual
  created_at timestamptz default now()
);

create table contacts (
  id uuid primary key default gen_random_uuid(),
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
create index on contacts (status);

create table research_reports (
  id uuid primary key default gen_random_uuid(),
  company_id uuid references companies(id) on delete cascade,
  pages_fetched jsonb,                   -- [{url, fetched_at, text_hash}]
  services text[],
  tech_signals text[],
  workflow_hypotheses jsonb,             -- [{hypothesis, evidence, confidence}]
  summary text,
  model text,
  created_at timestamptz default now()
);

create table campaigns (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  icp_version text,
  offer text,
  autonomy_level int not null default 0 check (autonomy_level between 0 and 3),
  active boolean default true,
  created_at timestamptz default now()
);

create table sequences (
  id uuid primary key default gen_random_uuid(),
  campaign_id uuid references campaigns(id) on delete cascade,
  step int not null,
  channel channel_type not null,
  delay_days int not null default 0,
  unique (campaign_id, step)
);

create table messages (
  id uuid primary key default gen_random_uuid(),
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
  scheduled_at timestamptz,
  sent_at timestamptz,
  created_at timestamptz default now()
);
create index on messages (status, scheduled_at);

create table suppression_list (
  id uuid primary key default gen_random_uuid(),
  email text,
  domain text,
  phone text,
  reason text not null,                  -- unsubscribe, bounce, complaint, manual
  created_at timestamptz default now(),
  check (email is not null or domain is not null or phone is not null)
);
create unique index on suppression_list (lower(email)) where email is not null;
create unique index on suppression_list (lower(domain)) where domain is not null;

create table events (
  id bigserial primary key,
  contact_id uuid,
  message_id uuid,
  type text not null,                    -- approved, edited, sent, bounced, replied, llm_call...
  actor text,                            -- agent, human:<id>, system
  payload jsonb,
  created_at timestamptz default now()
);
create index on events (contact_id, created_at);
