"""Live end-to-end harness: Supabase inventory + lead ingestion + WhatsApp welcome.

**Not** collected by pytest (no ``test_*`` functions). Run manually from ``backend/``::

    .\\venv\\Scripts\\python.exe tests/whatsapp_rag_verify.py

Prerequisites
-------------
* ``SUPABASE_URL`` + ``SUPABASE_SERVICE_ROLE_KEY`` in env or ``backend/.env``
* ``WHATSAPP_ACCESS_TOKEN`` + ``WHATSAPP_PHONE_NUMBER_ID`` and ``WHATSAPP_DRY_RUN=false``
  for a real Graph send
* Migration ``006_lead_unit_matching.sql`` applied (``matched_unit_id``, ``match_metadata``)

Env (optional overrides)
------------------------
* ``WHATSAPP_E2E_ORG_SLUG`` — default ``default`` (seed org from ``001_multitenant.sql``)
* ``WHATSAPP_E2E_ORG_ID`` — if set, skips slug lookup
* ``WHATSAPP_E2E_PROPERTY_NAME`` — must match ``properties.name`` for strict resolver; default
  ``PropClose WhatsApp RAG Verify``
* ``WHATSAPP_E2E_PHONE`` — digits or ``+91...``; default ``917021109469``
* ``WHATSAPP_E2E_SKIP_SEND`` — set to ``1`` to insert lead but skip ``process_new_lead`` Graph call

After success, check the printed ``matched_unit_id``, then reply **Price kya hai?** on WhatsApp
to exercise matched-unit RAG fact-lock.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

# Allow ``python tests/whatsapp_rag_verify.py`` from ``backend/`` working directory.
if __name__ == "__main__" and __package__ is None:
    _here = os.path.dirname(os.path.abspath(__file__))
    _backend = os.path.dirname(_here)
    if _backend not in sys.path:
        sys.path.insert(0, _backend)

from app.db.supabase_client import get_supabase_client  # noqa: E402
from app.schemas.leads import LeadCreateRequest  # noqa: E402
from app.services.automation_service import AutomationService  # noqa: E402
from app.services.ingestion_service import LeadIngestionService  # noqa: E402

DEMO_UNIT_NAME = "Shop-X"
DEMO_CONFIGURATION = "Commercial Shop"
DEMO_PRICE = 7500000
# ``public.unit_inventory.floor_no`` is an integer in this project (``22P02`` if sent as text like "GF").
DEMO_FLOOR_NO = 0


class NoopBackgroundTasks:
    """``create_lead_impl`` expects FastAPI-compatible ``add_task``; we run automation ourselves."""

    def add_task(self, func, *args, **kwargs) -> None:
        del func, args, kwargs


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def resolve_org_id(supabase: Any) -> str:
    oid = _env("WHATSAPP_E2E_ORG_ID")
    if oid:
        return oid
    slug = _env("WHATSAPP_E2E_ORG_SLUG", "default")
    resp = (
        supabase.table("organizations")
        .select("id")
        .eq("slug", slug)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        raise SystemExit(f"No organization found for slug={slug!r}. Set WHATSAPP_E2E_ORG_ID.")
    return str(rows[0]["id"])


def ensure_property(supabase: Any, org_id: str, name: str) -> str:
    resp = (
        supabase.table("properties")
        .select("id,name")
        .eq("org_id", org_id)
        .execute()
    )
    norm = name.strip().lower()
    for row in resp.data or []:
        if str(row.get("name") or "").strip().lower() == norm:
            return str(row["id"])
    ins = (
        supabase.table("properties")
        .insert({"org_id": org_id, "name": name.strip()})
        .execute()
    )
    data = ins.data or []
    if not data:
        raise SystemExit("Failed to insert property row.")
    return str(data[0]["id"])


def upsert_demo_shop_unit(supabase: Any, org_id: str, project_id: str) -> str:
    filters = (
        supabase.table("unit_inventory")
        .select("id")
        .eq("org_id", org_id)
        .eq("project_id", project_id)
        .eq("unit_name", DEMO_UNIT_NAME)
        .eq("floor_no", DEMO_FLOOR_NO)
        .eq("configuration", DEMO_CONFIGURATION)
        .limit(1)
        .execute()
    )
    payload: dict[str, Any] = {
        "org_id": org_id,
        "project_id": project_id,
        "unit_name": DEMO_UNIT_NAME,
        "floor_no": DEMO_FLOOR_NO,
        "configuration": DEMO_CONFIGURATION,
        "carpet_area": "450 sqft",
        "price": DEMO_PRICE,
        "status": "Available",
        "ai_summary": "WhatsApp E2E: Commercial Shop Shop-X, 75 Lakh (verify script).",
        "metadata": {"source": "whatsapp_rag_verify"},
    }
    rows = filters.data or []
    if rows:
        uid = str(rows[0]["id"])
        supabase.table("unit_inventory").update(payload).eq("id", uid).execute()
        return uid
    ins = supabase.table("unit_inventory").insert(payload).execute()
    data = ins.data or []
    if not data:
        raise SystemExit("Failed to upsert demo unit_inventory row.")
    return str(data[0]["id"])


def main() -> None:
    phone_raw = _env("WHATSAPP_E2E_PHONE", "917021109469")
    phone_digits = "".join(c for c in phone_raw if c.isdigit())

    prop_name = _env("WHATSAPP_E2E_PROPERTY_NAME", "PropClose WhatsApp RAG Verify")

    supabase = get_supabase_client()
    org_id = resolve_org_id(supabase)
    property_id = ensure_property(supabase, org_id, prop_name)

    demo_unit_id = upsert_demo_shop_unit(supabase, org_id, property_id)

    ingestion = LeadIngestionService()
    payload = LeadCreateRequest(
        name="WhatsApp E2E RAG",
        phone=phone_digits,
        email=None,
        source="whatsapp_rag_verify",
        property_name=prop_name,
        status="New",
        configuration="Shop",
        budget="75 Lakhs",
    )

    noop_bg = NoopBackgroundTasks()
    result = ingestion.create_lead_impl(
        payload,
        noop_bg,
        org_id=org_id,
        extra_db_fields=None,
    )

    lead_id = result.get("lead_id")
    matched = result.get("matched_unit_id")
    meta = result.get("match_metadata")

    # Read back ``public.leads`` so we verify DB persistence (not just API return body).
    db_lead: dict[str, Any] = {}
    try:
        lr = (
            supabase.table("leads")
            .select("id,org_id,name,phone,property_id,matched_unit_id,match_metadata")
            .eq("org_id", org_id)
            .eq("id", str(lead_id))
            .limit(1)
            .execute()
        )
        rows = lr.data or []
        db_lead = dict(rows[0]) if rows else {}
    except Exception as exc:
        print(f"[WARN] Could not read back leads row: {exc}", file=sys.stderr)

    db_matched = db_lead.get("matched_unit_id")
    db_meta = db_lead.get("match_metadata")

    print("=" * 60)
    print("SUCCESS — Lead write completed")
    print("=" * 60)
    print(f"lead_id           : {lead_id}")
    print(f"org_id            : {org_id}")
    print(f"name (request)    : {payload.name}")
    print(f"phone (request)   : {phone_digits}")
    print(f"property_id       : {property_id}")
    print(f"demo_unit_row_id  : {demo_unit_id}")
    print(f"matched_unit_id (ingestion response): {matched}")
    print(f"match_metadata (ingestion response) : {json.dumps(meta, indent=2)}")
    print("-" * 60)
    print("VALIDATION — public.leads after upsert (source of truth)")
    print(f"  leads.matched_unit_id  : {db_matched}")
    print(f"  leads.match_metadata   : {json.dumps(db_meta, indent=2)}")
    if db_matched and str(db_matched) != str(matched):
        print(
            "[WARN] DB matched_unit_id differs from ingestion response — investigate PostgREST return.",
            file=sys.stderr,
        )
    print("=" * 60)

    if matched and str(matched) != str(demo_unit_id):
        print(
            "[WARN] matched_unit_id differs from refreshed demo_unit_id "
            "(another row may rank higher). Compare configuration/price hints.",
            file=sys.stderr,
        )

    if _env("WHATSAPP_E2E_SKIP_SEND") in {"1", "true", "yes"}:
        print("WHATSAPP_E2E_SKIP_SEND set — skipping process_new_lead WhatsApp delivery.")
        return

    AutomationService().process_new_lead(str(lead_id), org_id)
    print(
        "\nAutomation: process_new_lead(send_welcome_message) invoked. "
        "If Meta credentials + WHATSAPP_DRY_RUN=false, check your handset for the welcome."
    )


if __name__ == "__main__":
    main()
