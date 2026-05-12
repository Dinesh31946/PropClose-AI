-- =========================================================================
-- PropClose AI — WhatsApp inbound idempotency + delivery log
-- =========================================================================
--   Enterprise isolation layer - mandatory for SaaS scalability.
--   Goal: Meta retries every webhook every few seconds for ~24h until we
--   answer 200 OK.  We answer 200 OK fast (BackgroundTasks) BUT a transient
--   network blip can still cause Meta to deliver the same `messages[].id`
--   to us twice.  This table is the durable, tenant-scoped guard rail
--   that makes our pipeline atomically idempotent.
--
--   Design:
--     * `(org_id, message_id)` is the PRIMARY KEY.  An attempted INSERT
--       with the same key fails with 23505 -- that's our dedup signal.
--     * `direction` distinguishes inbound (from Meta) and outbound
--       (replies we sent), so the same table doubles as the broker-
--       facing message log.
--     * RLS-friendly: `org_id` is NOT NULL with FK + cascade delete.
--
-- Run order:  Supabase SQL editor  →  paste  →  execute.  Idempotent.
-- =========================================================================

create extension if not exists pgcrypto;

create table if not exists public.whatsapp_messages (
    id              uuid        primary key default gen_random_uuid(),
    org_id          uuid        not null
                                references public.organizations(id) on delete cascade,
    message_id      text        not null,                         -- Meta's wamid
    direction       text        not null
                                check (direction in ('inbound', 'outbound')),
    lead_id         uuid                                          -- nullable: inbound from unknown numbers
                                references public.leads(id)         on delete set null,
    property_id     uuid                                          -- nullable: lead may not be property-mapped yet
                                references public.properties(id)    on delete set null,
    from_phone      text,                                         -- inbound: customer; outbound: phone_number_id
    to_phone        text,                                         -- inbound: phone_number_id; outbound: customer
    body            text,
    status          text        not null default 'received'
                                check (status in (
                                    'received',     -- inbound, queued
                                    'processed',    -- inbound, RAG completed
                                    'sent',         -- outbound, accepted by Meta
                                    'failed',       -- outbound, Meta rejected or RAG fell over
                                    'duplicate'     -- inbound seen before (claim attempt failed)
                                )),
    error_detail    text,
    raw_payload     jsonb,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

-- Tenant-scoped uniqueness for inbound dedup.  We deliberately scope by
-- direction because outbound `wamid`s are issued by Meta after WE send,
-- so the same `message_id` could in theory cross with an inbound id;
-- defense in depth.
create unique index if not exists whatsapp_messages_org_dir_msgid_uniq
    on public.whatsapp_messages (org_id, direction, message_id);

-- Hot path: list latest messages per lead in the broker dashboard.
create index if not exists whatsapp_messages_org_lead_created_idx
    on public.whatsapp_messages (org_id, lead_id, created_at desc);

-- Hot path: find a lead by their phone number when a message arrives
-- (org_id, phone) lookups already use leads_org_phone_property_unique
-- via 001_multitenant.sql, so no extra index is needed on `leads`.

-- Touch updated_at on every UPDATE so we can reason about delivery latency.
create or replace function public.touch_whatsapp_messages_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

drop trigger if exists trg_touch_whatsapp_messages on public.whatsapp_messages;
create trigger trg_touch_whatsapp_messages
    before update on public.whatsapp_messages
    for each row execute function public.touch_whatsapp_messages_updated_at();

-- =========================================================================
-- RLS: enable + tenant-scoped policies (mirrors 002_rls.sql for the rest
-- of the tenant-bearing tables).  Service-role keeps bypassing.
-- =========================================================================
alter table public.whatsapp_messages enable row level security;

drop policy if exists whatsapp_messages_select_own on public.whatsapp_messages;
drop policy if exists whatsapp_messages_insert_own on public.whatsapp_messages;
drop policy if exists whatsapp_messages_update_own on public.whatsapp_messages;
drop policy if exists whatsapp_messages_delete_own on public.whatsapp_messages;

create policy whatsapp_messages_select_own on public.whatsapp_messages
    for select using (org_id = public.current_org_id());

create policy whatsapp_messages_insert_own on public.whatsapp_messages
    for insert with check (org_id = public.current_org_id());

create policy whatsapp_messages_update_own on public.whatsapp_messages
    for update using (org_id = public.current_org_id())
                with check (org_id = public.current_org_id());

create policy whatsapp_messages_delete_own on public.whatsapp_messages
    for delete using (org_id = public.current_org_id());
