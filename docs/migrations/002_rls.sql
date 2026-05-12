-- =========================================================================
-- PropClose AI — Row Level Security (RLS) for tenant isolation
-- =========================================================================
--   Enterprise isolation layer - mandatory for SaaS scalability.
--
-- After 001_multitenant.sql, every tenant-bearing table has org_id and
-- the FastAPI layer scopes every query.  RLS is the database-side
-- defense in depth: even if the API layer ever forgets a `.eq("org_id")`
-- (or the dashboard reads directly via supabase-js with the anon key),
-- the database itself refuses cross-tenant reads.
--
-- IMPORTANT — who is affected:
--   * service_role  (used by FastAPI) ALWAYS bypasses RLS.  The API path
--     keeps working unchanged after this migration.
--   * anon          (Supabase JS client without a user JWT) -> sees 0 rows.
--   * authenticated (anon client + a user JWT) -> sees only its own org's
--     rows, based on the org_id claim inside the JWT.
--
-- Run order: apply AFTER 001_multitenant.sql.
-- This script is idempotent.
-- =========================================================================

-- ----- 1. Enable RLS ----------------------------------------------------
alter table public.organizations    enable row level security;
alter table public.properties       enable row level security;
alter table public.leads            enable row level security;
alter table public.unit_inventory   enable row level security;
alter table public.brochure_chunks  enable row level security;
alter table public.chat_history     enable row level security;

-- ----- 2. Helper that reads org_id from the verified JWT ---------------
-- We accept either ``app_metadata.org_id`` (Supabase's recommended
-- location for custom claims) or a top-level ``org_id`` claim.  Returns
-- NULL when no JWT is present, which means "no rows" under RLS.
create or replace function public.current_org_id()
returns uuid
language sql stable
as $$
    select coalesce(
        nullif(auth.jwt() -> 'app_metadata' ->> 'org_id', ''),
        nullif(auth.jwt() ->> 'org_id', '')
    )::uuid;
$$;

-- ----- 3. Policies ------------------------------------------------------
-- We drop-then-create so the script is idempotent.  Naming convention:
--   <table>_<operation>_tenant_only

-- organizations: a user can SEE / UPDATE only its own org.  Inserts and
-- deletes are reserved for service_role (which bypasses RLS anyway).
drop policy if exists organizations_select_tenant_only on public.organizations;
create policy organizations_select_tenant_only on public.organizations
    for select
    using (id = public.current_org_id());

drop policy if exists organizations_update_tenant_only on public.organizations;
create policy organizations_update_tenant_only on public.organizations
    for update
    using (id = public.current_org_id())
    with check (id = public.current_org_id());

-- properties / leads / unit_inventory / brochure_chunks / chat_history:
-- full CRUD scoped by org_id.  We split into 4 policies per table so each
-- operation has an explicit predicate (Supabase / Postgres convention).
do $$
declare
    tbl text;
begin
    foreach tbl in array array[
        'properties', 'leads', 'unit_inventory', 'brochure_chunks', 'chat_history'
    ]
    loop
        execute format('drop policy if exists %I_select_tenant_only on public.%I', tbl, tbl);
        execute format(
            'create policy %I_select_tenant_only on public.%I for select using (org_id = public.current_org_id())',
            tbl, tbl
        );

        execute format('drop policy if exists %I_insert_tenant_only on public.%I', tbl, tbl);
        execute format(
            'create policy %I_insert_tenant_only on public.%I for insert with check (org_id = public.current_org_id())',
            tbl, tbl
        );

        execute format('drop policy if exists %I_update_tenant_only on public.%I', tbl, tbl);
        execute format(
            'create policy %I_update_tenant_only on public.%I for update using (org_id = public.current_org_id()) with check (org_id = public.current_org_id())',
            tbl, tbl
        );

        execute format('drop policy if exists %I_delete_tenant_only on public.%I', tbl, tbl);
        execute format(
            'create policy %I_delete_tenant_only on public.%I for delete using (org_id = public.current_org_id())',
            tbl, tbl
        );
    end loop;
end $$;

-- ----- 4. Verification queries (read-only; safe to run any time) -------
-- Confirm RLS is on for every tenant-bearing table.
do $$
declare
    rec record;
begin
    for rec in
        select c.relname, c.relrowsecurity
        from pg_class c
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'public'
          and c.relname in ('organizations','properties','leads',
                            'unit_inventory','brochure_chunks','chat_history')
    loop
        if not rec.relrowsecurity then
            raise exception 'RLS not enabled on %', rec.relname;
        end if;
    end loop;
end $$;

-- ----- 5. Rollback (commented out, for emergencies) --------------------
--
-- alter table public.organizations    disable row level security;
-- alter table public.properties       disable row level security;
-- alter table public.leads            disable row level security;
-- alter table public.unit_inventory   disable row level security;
-- alter table public.brochure_chunks  disable row level security;
-- alter table public.chat_history     disable row level security;
