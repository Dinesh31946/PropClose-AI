-- =========================================================================
-- PropClose AI — Property-scoped RAG retrieval
-- =========================================================================
--   Enterprise isolation layer - mandatory for SaaS scalability.
--   Goal: when the AI is talking to a lead about Property X, the vector
--   ANN scan must stop at the property boundary, not at the org boundary.
--   Two reasons:
--
--     1. Correctness (the user's #1 ask): the AI must NEVER pull a
--        chunk or unit from a sibling project under the same org.
--     2. Cost / latency: a broker with 50 active projects has ~50x more
--        rows in unit_inventory + brochure_chunks than they want
--        considered for a single chat turn.  Filtering at SQL prunes
--        ~98% of rows BEFORE the cosine-distance computation.
--
-- The new RPCs accept an OPTIONAL ``match_property_id``.  Passing NULL
-- restores org-wide search (used by the "broader search" path that
-- only runs after the lead explicitly opts in via the redirect prompt).
--
-- Run order:  Supabase SQL editor -> paste -> execute.  Idempotent.
-- =========================================================================

-- Drop every overload we have ever shipped, including the new
-- (vector,float,int,uuid,uuid) signature so re-runs are clean.
drop function if exists public.match_units(vector, float, int);
drop function if exists public.match_units(vector, float, int, uuid);
drop function if exists public.match_units(vector, float, int, uuid, uuid);
drop function if exists public.match_chunks(vector, float, int);
drop function if exists public.match_chunks(vector, float, int, uuid);
drop function if exists public.match_chunks(vector, float, int, uuid, uuid);

-- =========================================================================
-- match_units: vector ANN over unit_inventory, scoped by org + (optional) project.
-- =========================================================================
create or replace function public.match_units(
    query_embedding   vector(1536),
    match_threshold   float,
    match_count       int,
    match_org_id      uuid,
    match_property_id uuid default null   -- NULL = org-wide (post-consent path)
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
      and (match_property_id is null or ui.project_id = match_property_id)
      and ui.embedding is not null
      and 1 - (ui.embedding <=> query_embedding) >= match_threshold
    order by ui.embedding <=> query_embedding
    limit match_count;
$$;

-- =========================================================================
-- match_chunks: vector ANN over brochure_chunks, scoped by org + (optional) property.
-- =========================================================================
create or replace function public.match_chunks(
    query_embedding   vector(1536),
    match_threshold   float,
    match_count       int,
    match_org_id      uuid,
    match_property_id uuid default null   -- NULL = org-wide (post-consent path)
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
      and (match_property_id is null or bc.property_id = match_property_id)
      and bc.embedding is not null
      and 1 - (bc.embedding <=> query_embedding) >= match_threshold
    order by bc.embedding <=> query_embedding
    limit match_count;
$$;

-- =========================================================================
-- The composite indexes already exist from 001_multitenant.sql:
--     unit_inventory_org_project_idx  (org_id, project_id)
--     brochure_chunks_org_property_idx (org_id, property_id)
-- They keep the property-id branch above index-only fast.
-- =========================================================================
