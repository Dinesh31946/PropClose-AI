-- =========================================================================
-- PropClose AI — Multi-tenant migration (P0)
-- =========================================================================
--   Enterprise isolation layer - mandatory for SaaS scalability.
--   Goal: support 200+ organizations and 1000+ concurrent leads with zero
--   cross-tenant data leakage and sub-second RAG retrieval per tenant.
--
-- Run order:  Supabase SQL editor  →  paste this entire file  →  execute.
-- This script is fully idempotent; you can re-run after a partial failure.
-- =========================================================================

create extension if not exists pgcrypto;
create extension if not exists vector;

-- =========================================================================
-- 1. organizations: the new tenant root.
-- =========================================================================
create table if not exists public.organizations (
    id                uuid        primary key default gen_random_uuid(),
    name              text        not null,
    slug              text        not null unique,
    subscription_tier text        not null default 'trial'
                                  check (subscription_tier in ('trial', 'pro', 'enterprise')),
    created_at        timestamptz not null default now()
);

create index if not exists organizations_slug_idx
    on public.organizations(slug);

-- Seed a "default" org so we can backfill existing rows before flipping
-- org_id to NOT NULL.  Move existing data to the right tenant later by
-- updating org_id directly; the FK + indexes will follow.
insert into public.organizations (name, slug, subscription_tier)
values ('Default Organization', 'default', 'trial')
on conflict (slug) do nothing;

-- =========================================================================
-- 2. Add org_id to every tenant-bearing table.
--    Step 2a: nullable column so the ADD COLUMN itself is non-breaking.
-- =========================================================================
alter table public.properties      add column if not exists org_id uuid;
alter table public.leads           add column if not exists org_id uuid;
alter table public.unit_inventory  add column if not exists org_id uuid;
alter table public.brochure_chunks add column if not exists org_id uuid;
alter table public.chat_history    add column if not exists org_id uuid;

-- Step 2b: backfill any pre-existing rows to the default org.
--   Enterprise isolation layer - mandatory for SaaS scalability.
do $$
declare
    default_org_id uuid;
begin
    select id into default_org_id from public.organizations where slug = 'default';

    update public.properties      set org_id = default_org_id where org_id is null;
    update public.leads           set org_id = default_org_id where org_id is null;
    update public.unit_inventory  set org_id = default_org_id where org_id is null;
    update public.brochure_chunks set org_id = default_org_id where org_id is null;
    update public.chat_history    set org_id = default_org_id where org_id is null;
end $$;

-- Step 2c: lock the column down to NOT NULL.  After this point, every
-- INSERT must set org_id explicitly — that is the contract the new
-- FastAPI dependency enforces.
alter table public.properties      alter column org_id set not null;
alter table public.leads           alter column org_id set not null;
alter table public.unit_inventory  alter column org_id set not null;
alter table public.brochure_chunks alter column org_id set not null;
alter table public.chat_history    alter column org_id set not null;

-- =========================================================================
-- 3. Foreign keys with ON DELETE CASCADE.
--    Drop-then-add pattern so the script is idempotent on re-runs.
-- =========================================================================
alter table public.properties      drop constraint if exists properties_org_id_fkey;
alter table public.leads           drop constraint if exists leads_org_id_fkey;
alter table public.unit_inventory  drop constraint if exists unit_inventory_org_id_fkey;
alter table public.brochure_chunks drop constraint if exists brochure_chunks_org_id_fkey;
alter table public.chat_history    drop constraint if exists chat_history_org_id_fkey;

alter table public.properties
    add constraint properties_org_id_fkey
    foreign key (org_id) references public.organizations(id) on delete cascade;

alter table public.leads
    add constraint leads_org_id_fkey
    foreign key (org_id) references public.organizations(id) on delete cascade;

alter table public.unit_inventory
    add constraint unit_inventory_org_id_fkey
    foreign key (org_id) references public.organizations(id) on delete cascade;

alter table public.brochure_chunks
    add constraint brochure_chunks_org_id_fkey
    foreign key (org_id) references public.organizations(id) on delete cascade;

alter table public.chat_history
    add constraint chat_history_org_id_fkey
    foreign key (org_id) references public.organizations(id) on delete cascade;

-- =========================================================================
-- 4. Composite B-tree indexes on the hot paths.
--    Every list / detail / time-range query in the app starts with
--    `where org_id = $1`, so org_id MUST be the leading column.
-- =========================================================================
create index if not exists properties_org_id_id_idx
    on public.properties(org_id, id);
create index if not exists properties_org_created_idx
    on public.properties(org_id, created_at desc);

create index if not exists leads_org_id_id_idx
    on public.leads(org_id, id);
create index if not exists leads_org_created_idx
    on public.leads(org_id, created_at desc);
create index if not exists leads_org_property_created_idx
    on public.leads(org_id, property_id, created_at desc);

create index if not exists unit_inventory_org_id_id_idx
    on public.unit_inventory(org_id, id);
create index if not exists unit_inventory_org_project_idx
    on public.unit_inventory(org_id, project_id);

create index if not exists brochure_chunks_org_id_id_idx
    on public.brochure_chunks(org_id, id);
create index if not exists brochure_chunks_org_property_idx
    on public.brochure_chunks(org_id, property_id);

create index if not exists chat_history_org_id_id_idx
    on public.chat_history(org_id, id);
create index if not exists chat_history_org_lead_created_idx
    on public.chat_history(org_id, lead_id, created_at desc);

-- =========================================================================
-- 5. Tenant-scoped uniqueness.
--    The pre-existing global indexes from db-alignment.sql allowed
--    `(phone, property_id)` to collide across tenants — fatal in SaaS.
--    We drop those and re-create as `(org_id, ...)` partial indexes.
-- =========================================================================
drop index if exists public.leads_phone_property_unique;
drop index if exists public.leads_phone_null_property_unique;
drop index if exists public.leads_email_property_unique;
drop index if exists public.leads_email_null_property_unique;
drop index if exists public.leads_platform_external_unique;
drop index if exists public.properties_name_normalized_unique;
drop index if exists public.unit_inventory_project_unit_floor_config_unique;

-- Same broker, same project, same phone -> single canonical row.
create unique index if not exists leads_org_phone_property_unique
    on public.leads (org_id, phone, property_id)
    where phone is not null and property_id is not null;

-- Phone-only fallback (lead captured before we know which project).
create unique index if not exists leads_org_phone_null_property_unique
    on public.leads (org_id, phone)
    where phone is not null and property_id is null;

create unique index if not exists leads_org_email_property_unique
    on public.leads (org_id, email, property_id)
    where email is not null and property_id is not null;

create unique index if not exists leads_org_email_null_property_unique
    on public.leads (org_id, email)
    where email is not null and property_id is null;

-- External attribution dedupe (Meta / Google leadgen IDs).
create unique index if not exists leads_org_platform_external_unique
    on public.leads (org_id, platform, external_lead_id)
    where platform is not null and external_lead_id is not null;

-- Property names are unique per tenant, NOT globally.  Two brokers can
-- both sell "Skyline Towers" in different cities.
create unique index if not exists properties_org_name_normalized_unique
    on public.properties (org_id, (lower(btrim(name))))
    where name is not null;

-- Inventory rows are unique per tenant + project + floor + configuration.
create unique index if not exists unit_inventory_org_project_unit_floor_config_unique
    on public.unit_inventory (org_id, project_id, unit_name, floor_no, configuration)
    where project_id is not null;

-- =========================================================================
-- 6. Vector indexes (kept; recreated only if missing).
-- =========================================================================
create index if not exists unit_inventory_embedding_ivfflat
    on public.unit_inventory
    using ivfflat (embedding vector_cosine_ops) with (lists = 100);

create index if not exists brochure_chunks_embedding_ivfflat
    on public.brochure_chunks
    using ivfflat (embedding vector_cosine_ops) with (lists = 100);

-- =========================================================================
-- 7. Updated RAG RPCs.
--    Critical: org_id is filtered IN SQL (before the ANN scan), not in
--    Python after the fact.  This is what gets us sub-second response on
--    a multi-tenant table — the planner uses
--    `brochure_chunks_org_property_idx` to prune ~99% of rows before the
--    cosine-distance computation runs.
-- =========================================================================
drop function if exists public.match_units(vector, float, int);
drop function if exists public.match_chunks(vector, float, int);
drop function if exists public.match_units(vector, float, int, uuid);
drop function if exists public.match_chunks(vector, float, int, uuid);

create or replace function public.match_units(
    query_embedding vector(1536),
    match_threshold float,
    match_count     int,
    match_org_id    uuid
)
returns table (
    id            uuid,
    org_id        uuid,
    project_id    uuid,
    unit_name     text,
    configuration text,
    floor_no      text,
    carpet_area   text,
    price         text,
    status        text,
    ai_summary    text,
    similarity    float
)
language sql stable
as $$
    select
        ui.id,
        ui.org_id,
        ui.project_id,
        ui.unit_name,
        ui.configuration,
        ui.floor_no,
        ui.carpet_area,
        ui.price,
        ui.status,
        ui.ai_summary,
        1 - (ui.embedding <=> query_embedding) as similarity
    from public.unit_inventory ui
    where ui.org_id = match_org_id
      and ui.embedding is not null
      and 1 - (ui.embedding <=> query_embedding) >= match_threshold
    order by ui.embedding <=> query_embedding
    limit match_count;
$$;

create or replace function public.match_chunks(
    query_embedding vector(1536),
    match_threshold float,
    match_count     int,
    match_org_id    uuid
)
returns table (
    id          uuid,
    org_id      uuid,
    property_id uuid,
    content     text,
    similarity  float
)
language sql stable
as $$
    select
        bc.id,
        bc.org_id,
        bc.property_id,
        bc.content,
        1 - (bc.embedding <=> query_embedding) as similarity
    from public.brochure_chunks bc
    where bc.org_id = match_org_id
      and bc.embedding is not null
      and 1 - (bc.embedding <=> query_embedding) >= match_threshold
    order by bc.embedding <=> query_embedding
    limit match_count;
$$;

-- =========================================================================
-- 8. (Optional but recommended) Row-Level Security.
--    Defense in depth: even if the FastAPI layer ever forgets a `.eq`,
--    the database itself refuses cross-tenant reads.
--
--    The service-role key (used by FastAPI) BYPASSES RLS by default, so
--    the API contract still works — the policies below take effect for
--    anon / authenticated keys (e.g. when the dashboard reads directly
--    via the Supabase JS SDK).
--
--    Setting `app.current_org_id` per request is the GUC convention.
--    Uncomment when you're ready to enforce it.
-- =========================================================================
-- alter table public.organizations    enable row level security;
-- alter table public.properties       enable row level security;
-- alter table public.leads            enable row level security;
-- alter table public.unit_inventory   enable row level security;
-- alter table public.brochure_chunks  enable row level security;
-- alter table public.chat_history     enable row level security;
--
-- create policy tenant_isolation_properties on public.properties
--     using (org_id = (current_setting('app.current_org_id', true))::uuid);
-- create policy tenant_isolation_leads on public.leads
--     using (org_id = (current_setting('app.current_org_id', true))::uuid);
-- create policy tenant_isolation_inventory on public.unit_inventory
--     using (org_id = (current_setting('app.current_org_id', true))::uuid);
-- create policy tenant_isolation_chunks on public.brochure_chunks
--     using (org_id = (current_setting('app.current_org_id', true))::uuid);
-- create policy tenant_isolation_chat on public.chat_history
--     using (org_id = (current_setting('app.current_org_id', true))::uuid);
