-- Close Supabase's auto-generated Data API (PostgREST) over our tables. Safe to re-run.
-- Supabase exposes every table in `public` to the API, and the `anon` key is designed to be public.
-- Enabling row-level security with NO policies denies the anon/authenticated roles everything.
-- The agent connects as the table owner (postgres), which RLS does not apply to, so nothing changes for it.
-- This is a lock, not multi-tenant RLS: tenant isolation stays in the repository layer for now.

alter table tenants          enable row level security;
alter table companies        enable row level security;
alter table contacts         enable row level security;
alter table research_reports enable row level security;
alter table campaigns        enable row level security;
alter table sequences        enable row level security;
alter table messages         enable row level security;
alter table suppression_list enable row level security;
alter table events           enable row level security;
