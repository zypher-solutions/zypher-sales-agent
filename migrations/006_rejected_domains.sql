-- Companies that research rejected as not a fit. The company and everything about it (contacts, research,
-- drafts) is deleted; only the website domain stays, so `find` and imports never add it again and we never
-- pay to research it twice. No names, emails or page text are kept here. Safe to re-run.
create table if not exists rejected_domains (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null default '00000000-0000-0000-0000-000000000001' references tenants(id),
  domain text not null,
  reason text not null,                  -- the rejection code, e.g. no_proof_of_proper_company
  icp_version int,                       -- icp.yaml version that rejected it (lets a future command re-open them)
  created_at timestamptz default now()
);
create unique index if not exists rejected_tenant_domain_key on rejected_domains (tenant_id, lower(domain));
alter table rejected_domains enable row level security;  -- same Data API lock as migration 003

-- One-off: move companies already rejected (and never emailed) into the list, then delete them.
-- A company we have emailed is kept: its history is needed for replies and opt-outs.
insert into rejected_domains (tenant_id, domain, reason)
select co.tenant_id, co.domain,
       coalesce(split_part((select rr.disqualify_reason from research_reports rr
                            where rr.company_id = co.id order by rr.created_at desc limit 1), ':', 1), 'not_a_fit')
from companies co
where exists (select 1 from contacts c where c.company_id = co.id and c.status = 'disqualified')
  and not exists (select 1 from contacts c join messages m on m.contact_id = c.id
                  where c.company_id = co.id and m.sent_at is not null)
on conflict do nothing;

delete from companies co
where exists (select 1 from rejected_domains r where r.tenant_id = co.tenant_id and lower(r.domain) = lower(co.domain))
  and not exists (select 1 from contacts c join messages m on m.contact_id = c.id
                  where c.company_id = co.id and m.sent_at is not null);
