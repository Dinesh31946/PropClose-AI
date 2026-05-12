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

## Current status

| Area | State |
|------|--------|
| **Technical / QA** | **Solid** — **105** backend tests passing; matching, RAG gates, webhook → chat plumbing stable. |
| **Conversation UX** | **Good foundation; still tuning** — copy can feel **stiff** next to a **high-bar human agent**; flow needs iterative polish without weakening grounding. |

---

## Current goal

Ship dialogue that stays **fact-locked** but reads **warm, concise, advisory** — closer to premium desk calibre than scripted bot cadence.

---

## Next session — Conversation Polish Phase 2

1. **Narrow the dialogue**: tighter openings, lighter transitions, less template echo across turns.
2. **Seamless info-seeking → site visit**: sharper signals for when discovery is “enough”; softer, contextual visit/call framing (no premature push).
