-- Progressive profiling: structured fields extracted from chat (budget, timeline, purpose, requirement).
alter table public.leads
    add column if not exists profiling_data jsonb not null default '{}'::jsonb;

comment on column public.leads.profiling_data is
    'JSON snapshot of buyer profiling (e.g. budget, timeline, purpose, requirement).';
