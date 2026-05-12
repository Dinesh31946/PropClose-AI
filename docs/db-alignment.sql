-- PropClose AI DB alignment (run in Supabase SQL editor)
-- Goal: sync database with current FastAPI lead + inventory + RAG logic.

create extension if not exists pgcrypto;
create extension if not exists vector;

-- ---------------------------
-- LEADS: external ingestion support
-- ---------------------------
alter table if exists public.leads
  add column if not exists platform text,
  add column if not exists external_lead_id text,
  add column if not exists listing_external_id text,
  add column if not exists campaign_id text,
  add column if not exists ad_id text,
  add column if not exists adgroup_id text,
  add column if not exists form_id text,
  add column if not exists gcl_id text,
  add column if not exists is_test boolean default false,
  add column if not exists lead_submit_time timestamptz,
  add column if not exists raw_payload jsonb;

-- create_lead upsert(on_conflict=phone,property_id) needs this unique index.
create unique index if not exists leads_phone_property_unique
on public.leads (phone, property_id)
where phone is not null and property_id is not null;

-- Fallback dedupe paths used by API logic.
create unique index if not exists leads_phone_null_property_unique
on public.leads (phone)
where phone is not null and property_id is null;

create unique index if not exists leads_email_property_unique
on public.leads (email, property_id)
where email is not null and property_id is not null;

create unique index if not exists leads_email_null_property_unique
on public.leads (email)
where email is not null and property_id is null;

create unique index if not exists leads_platform_external_unique
on public.leads (platform, external_lead_id)
where platform is not null and external_lead_id is not null;

create index if not exists leads_property_id_idx on public.leads(property_id);
create index if not exists leads_created_at_idx on public.leads(created_at desc);
create index if not exists leads_campaign_id_idx on public.leads(campaign_id);
create index if not exists leads_source_created_idx on public.leads(source, created_at desc);

-- ---------------------------
-- PROPERTIES: strict normalized match support
-- ---------------------------
create unique index if not exists properties_name_normalized_unique
on public.properties ((lower(btrim(name))))
where name is not null;

-- ---------------------------
-- INVENTORY: dedupe and retrieval speed
-- ---------------------------
create unique index if not exists unit_inventory_project_unit_floor_config_unique
on public.unit_inventory (project_id, unit_name, floor_no, configuration)
where project_id is not null;

create index if not exists unit_inventory_project_id_idx on public.unit_inventory(project_id);

-- ---------------------------
-- CHAT & KNOWLEDGE BASE indexes
-- ---------------------------
create index if not exists chat_history_lead_created_idx
on public.chat_history(lead_id, created_at desc);

create index if not exists brochure_chunks_property_id_idx
on public.brochure_chunks(property_id);

-- ---------------------------
-- Vector indexes for match_units / match_chunks RPC
-- ---------------------------
create index if not exists unit_inventory_embedding_ivfflat
on public.unit_inventory
using ivfflat (embedding vector_cosine_ops)
with (lists = 100);

create index if not exists brochure_chunks_embedding_ivfflat
on public.brochure_chunks
using ivfflat (embedding vector_cosine_ops)
with (lists = 100);
