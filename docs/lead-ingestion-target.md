# Lead Ingestion Target State

## Why this is needed

Portal and social lead systems do not always send a clean CRM-ready payload in one shot.
Some channels (for example Meta leadgen webhooks) may first send event identifiers and require a follow-up retrieval step for full contact fields.

## Canonical ingest strategy

1. Accept normalized direct fields (`name`, `phone`, `email`, `property_name`) when available.
2. Also accept source-native structures:
   - Google style `user_column_data`
   - Meta style `field_data`
3. Normalize into one internal lead object and run strict property match checks.
4. **Phone is required** (Option B): `leads.phone` is NOT NULL in Postgres; email-only payloads must be **enriched upstream** (provider APIs / mapping) before calling `POST /api/v1/leads` or `POST /api/v1/leads/external`.

## Data integrity rule

Data integrity is priority #1 for this AI Lead Engine; strict matching prevents cross-project data leaks.

## Endpoint contract

- Primary CRM entry: `POST /api/v1/leads`
- Channel-aware entry: `POST /api/v1/leads/external`

## Scalability notes

- Keep source IDs (`external_lead_id`, `campaign_id`, `ad_id`, `form_id`) for future dedupe/attribution.
- Move portal/social ingestion to queue workers when traffic rises.
- Add DB constraints and indexes for source-based dedupe in next migration pass.
