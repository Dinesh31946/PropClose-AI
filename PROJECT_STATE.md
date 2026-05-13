# Project state (context anchor)

This file is the durable snapshot of product and technical progress. Update it when phases land or priorities shift.

---

## Phase 0.1 & 0.2 — Smart Ingestion

- **LeadIngestionService**: Multi-attribute matching using **Project**, **Unit Type**, and **Price**.
- **Database**: Support for `matched_unit_id` and `match_metadata` to persist how leads align to inventory.

---

## Phase 0.3 — Fact-Lock RAG

- **`backend/app/rag/grounded_generator.py`** and **`backend/app/rag/retriever.py`** updated so that:
  - **Inventory facts** (e.g. **Price** and **Area**) come from the **matched unit**.
  - **Amenities** continue to be pulled from **brochure chunks**.

---

## Phase 0.4 — Intent & Urgency

- **`conversation_intent.py`**: Detects **Emergency Call Backs** and **Expert Bridges**.
- **Hidden intent tags** for backend tracking (not surfaced as user-visible labels in the same way as normal copy).

---

## Completed (today — conversation & channel polish)

- **Senior Residential Advisor persona** wired into **`grounded_generator.py`** defaults and **`config.py`** `whatsapp_assistant_persona`: less robotic repetition, **confident negatives** when amenities/features are absent from evidence (materials simply don’t mention them — avoid instant expert-bridge reflex).
- **Response logic**: unify to **single coherent bubble** (no stacked “dual automation” feel); **footer loop** curbed (“feel free to ask”, generic Hinglish closers, etc. only when deserved).
- **WhatsApp refinement**: confirm **number-awareness** behaviour (don't ask for phone on-channel; orchestration + webhook paths aligned).

---

## Phase 1.0 — Progressive Profiling Engine (completed)

- **Scope**: Data extraction and persistence only — **no lead scoring** in this phase.
- **Technical**: **`ProfilingService`** integrated into **`ChatService`**. Each turn runs extraction against the current message and recent history; merged output is written to **`leads.profiling_data`** (`jsonb`; migration **`docs/migrations/007_lead_profiling_data.sql`**).
- **Fields captured**: **Budget**, **Timeline**, **Purpose**, and **Requirement** (structured JSON on the lead row).
- **Persona / reply policy**: **`GroundedGenerator`** follows **Satisfy → Profile** — answer the user’s question from evidence first; only then, when the model can truthfully say the ask was met, append **one** soft question for the highest-priority missing profiling key (skipped on emergency / explicit human callback paths).

---

## Current status

| Area | State |
|------|--------|
| **Technical / QA** | **Solid** — **105** backend tests passing; matching, RAG gates, webhook → chat plumbing stable; **Phase 1.0 profiling wired and merged**. |
| **Conversation UX** | **Good foundation; still tuning** — copy can feel **stiff** next to a **high-bar human agent**; flow needs iterative polish without weakening grounding. |
| **Progressive profiling** | **Deployed in code** — extraction + **`profiling_data`** persistence + Satisfy → Profile tails on the grounded path; **functional validation on WhatsApp pending** (scheduled for tomorrow morning). |

---

## Current goal

**Validate progressive profiling end-to-end on WhatsApp**: confirm extraction accuracy, **`profiling_data`** updates on real threads, and that **Satisfy → Profile** feels natural (no premature asks, no crowding urgent or hand-off replies). **Lead scoring remains out of scope** until this pass feels solid.

---

## Next session — WhatsApp functional test + profiling tune

1. **Morning**: Live **functional testing on WhatsApp** — budget / timeline / purpose / requirement phrasing across Hinglish and English; check DB snapshots for **`profiling_data`**.
2. **Follow-up**: Prompt or priority tweaks if the soft profiling line misfires (too eager, wrong gap, tone).
3. **Still deferred**: Lead scoring layer.
