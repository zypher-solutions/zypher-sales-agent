-- Multi-tenant groundwork. Run once in the Supabase SQL editor AFTER schema.sql and 001. Safe to re-run.
-- A tenant is one business using the engine. Zypher is the default tenant (id ...0001).
-- No auth or row-level security yet: isolation is enforced in the repository layer.

create table if not exists tenants (
  id uuid primary key default gen_random_uuid(),
  slug text unique not null,
  name text not null,
  created_at timestamptz default now()
);

insert into tenants (id, slug, name)
values ('00000000-0000-0000-0000-000000000001', 'zypher', 'Zypher Software Solutions LLP')
on conflict (id) do nothing;

-- Existing rows are backfilled with the default tenant by the column default.
-- The default is kept for now so older code keeps working; the repository always sets tenant_id explicitly.
alter table companies        add column if not exists tenant_id uuid not null default '00000000-0000-0000-0000-000000000001' references tenants(id);
alter table contacts         add column if not exists tenant_id uuid not null default '00000000-0000-0000-0000-000000000001' references tenants(id);
alter table research_reports add column if not exists tenant_id uuid not null default '00000000-0000-0000-0000-000000000001' references tenants(id);
alter table campaigns        add column if not exists tenant_id uuid not null default '00000000-0000-0000-0000-000000000001' references tenants(id);
alter table sequences        add column if not exists tenant_id uuid not null default '00000000-0000-0000-0000-000000000001' references tenants(id);
alter table messages         add column if not exists tenant_id uuid not null default '00000000-0000-0000-0000-000000000001' references tenants(id);
alter table suppression_list add column if not exists tenant_id uuid not null default '00000000-0000-0000-0000-000000000001' references tenants(id);
alter table events           add column if not exists tenant_id uuid not null default '00000000-0000-0000-0000-000000000001' references tenants(id);

-- Uniqueness becomes per tenant: two tenants may target the same company or suppress the same address.
alter table companies drop constraint if exists companies_domain_key;
create unique index if not exists companies_tenant_domain_key on companies (tenant_id, domain);

-- Drop the old global suppression indexes (auto-named), whatever they are called, then add per-tenant ones.
do $$
declare r record;
begin
  for r in
    select indexname from pg_indexes
    where schemaname = current_schema() and tablename = 'suppression_list'
      and indexdef ilike 'create unique index%(lower(%' and indexdef not ilike '%tenant_id%'
  loop
    execute format('drop index %I', r.indexname);
  end loop;
end $$;
create unique index if not exists suppression_tenant_email_key  on suppression_list (tenant_id, lower(email))  where email is not null;
create unique index if not exists suppression_tenant_domain_key on suppression_list (tenant_id, lower(domain)) where domain is not null;

-- Every query filters on tenant_id, so lead it in the hot indexes.
create index if not exists contacts_tenant_status_idx on contacts (tenant_id, status);
create index if not exists messages_tenant_status_idx on messages (tenant_id, status, scheduled_at);
create index if not exists events_tenant_contact_idx  on events (tenant_id, contact_id, created_at);
