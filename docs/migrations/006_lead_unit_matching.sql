-- =========================================================================
-- PropClose AI — Lead ↔ unit_inventory matching (Phase 2 ingestion)
-- =========================================================================
-- Run order: Supabase SQL editor → paste → execute. Idempotent.
-- =========================================================================

alter table public.leads
    add column if not exists matched_unit_id uuid
        references public.unit_inventory (id) on delete set null;

alter table public.leads
    add column if not exists match_metadata jsonb not null default '{}'::jsonb;

create index if not exists leads_matched_unit_id_idx
    on public.leads (matched_unit_id)
    where matched_unit_id is not null;
