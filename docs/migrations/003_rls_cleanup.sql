-- =========================================================================
-- PropClose AI — RLS cleanup: enforce tenant-only policies, evict squatters
-- =========================================================================
--   Enterprise isolation layer - mandatory for SaaS scalability.
--
-- 002_rls.sql turned RLS on and added our `*_tenant_only` policies, but
-- any pre-existing permissive policies (created via the Supabase
-- Dashboard or earlier experiments) are OR'd into the row visibility
-- check.  smoke_rls.py caught one of those leaking 1 row to anon.
--
-- This script:
--   1. Lists every current policy on the 6 tenant-bearing tables (NOTICE).
--   2. Drops ALL of them, regardless of name.
--   3. Re-creates ONLY the tenant-scoped policies from 002.
--   4. Installs a `rls_status_check()` helper so future smoke scripts can
--      verify the configuration without DDL access.
--
-- Idempotent.  Apply AFTER 002_rls.sql.
-- =========================================================================

-- Sanity: 001 + 002 must already be in place.
do $$
begin
    if not exists (
        select 1 from pg_class c
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'public' and c.relname = 'organizations' and c.relrowsecurity
    ) then
        raise exception
            'organizations does not exist OR RLS is off on it. Run 001_multitenant.sql and 002_rls.sql first.';
    end if;
end $$;

-- ----- 1 + 2.  Drop EVERY policy on every tenant-bearing table -----------
do $$
declare
    rec record;
    dropped int := 0;
begin
    for rec in
        select schemaname, tablename, policyname
        from pg_policies
        where schemaname = 'public'
          and tablename in (
              'organizations', 'properties', 'leads',
              'unit_inventory', 'brochure_chunks', 'chat_history'
          )
    loop
        raise notice 'Dropping policy %.% on %.%',
            rec.policyname, rec.tablename, rec.schemaname, rec.tablename;
        execute format('drop policy if exists %I on public.%I',
                       rec.policyname, rec.tablename);
        dropped := dropped + 1;
    end loop;
    raise notice 'Total policies dropped: %', dropped;
end $$;

-- ----- 3.  Re-create the tenant-scoped policies (same logic as 002) -----
-- helper: re-affirm in case it was dropped
create or replace function public.current_org_id()
returns uuid
language sql stable
as $$
    select coalesce(
        nullif(auth.jwt() -> 'app_metadata' ->> 'org_id', ''),
        nullif(auth.jwt() ->> 'org_id', '')
    )::uuid;
$$;

create policy organizations_select_tenant_only on public.organizations
    for select
    using (id = public.current_org_id());

create policy organizations_update_tenant_only on public.organizations
    for update
    using (id = public.current_org_id())
    with check (id = public.current_org_id());

do $$
declare
    tbl text;
begin
    foreach tbl in array array[
        'properties', 'leads', 'unit_inventory', 'brochure_chunks', 'chat_history'
    ]
    loop
        execute format(
            'create policy %I_select_tenant_only on public.%I '
            'for select using (org_id = public.current_org_id())',
            tbl, tbl
        );
        execute format(
            'create policy %I_insert_tenant_only on public.%I '
            'for insert with check (org_id = public.current_org_id())',
            tbl, tbl
        );
        execute format(
            'create policy %I_update_tenant_only on public.%I '
            'for update using (org_id = public.current_org_id()) '
            'with check (org_id = public.current_org_id())',
            tbl, tbl
        );
        execute format(
            'create policy %I_delete_tenant_only on public.%I '
            'for delete using (org_id = public.current_org_id())',
            tbl, tbl
        );
    end loop;
end $$;

-- ----- 4.  rls_status_check helper for smoke scripts --------------------
-- SECURITY DEFINER + grant to anon/authenticated so the smoke script can
-- read it; the function itself only exposes the (table, rls_enabled,
-- policy_count) triple, no row data.
create or replace function public.rls_status_check()
returns table (
    "table" text,
    rls_enabled boolean,
    policy_count int
)
language sql stable
security definer
set search_path = public, pg_catalog
as $$
    select
        c.relname::text as "table",
        c.relrowsecurity as rls_enabled,
        coalesce(p.policy_count, 0)::int as policy_count
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    left join (
        select polrelid, count(*)::int as policy_count
        from pg_policy
        group by polrelid
    ) p on p.polrelid = c.oid
    where n.nspname = 'public'
      and c.relname in (
          'organizations','properties','leads',
          'unit_inventory','brochure_chunks','chat_history'
      )
    order by c.relname;
$$;

grant execute on function public.rls_status_check() to anon, authenticated, service_role;

-- ----- 5.  Final verification: every table must have exactly the policies
--          we created above (organizations: 2, others: 4).
do $$
declare
    rec record;
begin
    for rec in
        select tablename, count(*) as cnt
        from pg_policies
        where schemaname = 'public'
          and tablename in (
              'organizations','properties','leads',
              'unit_inventory','brochure_chunks','chat_history'
          )
        group by tablename
    loop
        if rec.tablename = 'organizations' and rec.cnt <> 2 then
            raise exception 'organizations should have 2 policies, has %', rec.cnt;
        elsif rec.tablename <> 'organizations' and rec.cnt <> 4 then
            raise exception '% should have 4 policies, has %', rec.tablename, rec.cnt;
        end if;
        raise notice '% has % policies (OK)', rec.tablename, rec.cnt;
    end loop;
end $$;
