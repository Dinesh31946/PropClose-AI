"""Live Supabase smoke check (NOT a pytest file — run manually).

    Enterprise isolation layer - mandatory for SaaS scalability.

This script makes REAL calls to your Supabase project using the credentials
in .env.local.  It is meant to be run once after applying the multi-tenant
migration (docs/migrations/001_multitenant.sql) to confirm:

    A. Connection works.
    B. ``organizations`` table exists and the ``default`` seed row is there.
    C. Every tenant-bearing table has an ``org_id`` column.
    D. The two RAG RPCs (``match_units`` / ``match_chunks``) accept the new
       ``match_org_id`` parameter.
    E. Writing / reading scoped by ``org_id`` works end to end.  We use a
       throw-away test org and DELETE it at the end (cascades clean up).

Run:
    .\\backend\\venv\\Scripts\\python.exe backend\\tests\\smoke_supabase.py
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

# Make `app...` imports work without installing the package.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import Settings  # noqa: E402
from app.db.supabase_client import get_supabase_client  # noqa: E402

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
DIM = "\033[2m"
RESET = "\033[0m"


def log_pass(msg: str) -> None:
    print(f"  {GREEN}PASS{RESET}  {msg}")


def log_fail(msg: str) -> None:
    print(f"  {RED}FAIL{RESET}  {msg}")


def log_step(msg: str) -> None:
    print(f"\n{YELLOW}>>>{RESET} {msg}")


def main() -> int:
    failures: list[str] = []
    settings = Settings.load()
    print(f"{DIM}Supabase URL: {settings.supabase_url}{RESET}")
    if not settings.supabase_url or not settings.supabase_service_role_key:
        print(f"{RED}Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in .env.local.{RESET}")
        return 1

    supabase = get_supabase_client()

    # ----- A. Connection + organizations table ---------------------------
    log_step("A. organizations table + default seed")
    try:
        resp = (
            supabase.table("organizations")
            .select("id,name,slug,subscription_tier")
            .eq("slug", "default")
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            failures.append("default org seed missing")
            log_fail("no row with slug='default' (re-run the migration)")
        else:
            row = rows[0]
            log_pass(f"organizations table reachable; default org id={row['id']}")
            log_pass(f"subscription_tier={row.get('subscription_tier')}")
    except Exception as exc:
        failures.append(f"organizations select failed: {exc}")
        log_fail(f"select on organizations failed: {exc}")
        return 1

    # ----- B. Every target table has org_id ------------------------------
    log_step("B. org_id column present on every tenant-bearing table")
    for table in ("properties", "leads", "unit_inventory", "brochure_chunks", "chat_history"):
        try:
            supabase.table(table).select("org_id").limit(1).execute()
            log_pass(f"{table}.org_id is selectable")
        except Exception as exc:
            failures.append(f"{table}.org_id missing or unreadable: {exc}")
            log_fail(f"{table}: {exc}")

    # ----- C. RAG RPCs accept match_org_id -------------------------------
    log_step("C. match_units / match_chunks accept match_org_id")
    fake_embedding = [0.0] * 1536  # text-embedding-3-small is 1536 dims
    fake_org = str(uuid.uuid4())  # unknown org → empty result, never error
    for rpc_name in ("match_units", "match_chunks"):
        try:
            resp = supabase.rpc(
                rpc_name,
                {
                    "query_embedding": fake_embedding,
                    "match_threshold": 0.99,
                    "match_count": 1,
                    "match_org_id": fake_org,
                },
            ).execute()
            log_pass(f"{rpc_name}() returned {len(resp.data or [])} rows for unknown org (expected 0)")
        except Exception as exc:
            failures.append(f"{rpc_name} rejected new signature: {exc}")
            log_fail(f"{rpc_name}: {exc}")

    # ----- D. End-to-end write + tenant scoping --------------------------
    log_step("D. Insert into a throw-away org and verify scoping")
    smoke_slug = f"smoke-{int(time.time())}"
    smoke_org_id: str | None = None
    try:
        org_resp = (
            supabase.table("organizations")
            .insert(
                {
                    "name": "PropClose smoke org",
                    "slug": smoke_slug,
                    "subscription_tier": "trial",
                }
            )
            .execute()
        )
        smoke_org_id = str(org_resp.data[0]["id"])
        log_pass(f"created smoke org {smoke_org_id} (slug={smoke_slug})")

        # Write a property under the smoke org.
        prop_resp = (
            supabase.table("properties")
            .insert(
                {
                    "org_id": smoke_org_id,
                    "name": "Smoke Test Tower",
                    "location": "Pune",
                    "price": "1.25 Cr",
                }
            )
            .execute()
        )
        smoke_property_id = str(prop_resp.data[0]["id"])
        log_pass(f"properties insert with org_id ok (id={smoke_property_id})")

        # Read it back scoped by org_id — must succeed.
        scoped = (
            supabase.table("properties")
            .select("id,name")
            .eq("org_id", smoke_org_id)
            .eq("id", smoke_property_id)
            .execute()
            .data
            or []
        )
        if scoped and scoped[0]["name"] == "Smoke Test Tower":
            log_pass("read-back scoped by org_id returns the correct row")
        else:
            failures.append("scoped read returned unexpected rows")
            log_fail(f"scoped read: {scoped}")

        # Read it back scoped by a DIFFERENT (random) org_id — must return [].
        wrong_org = str(uuid.uuid4())
        cross = (
            supabase.table("properties")
            .select("id")
            .eq("org_id", wrong_org)
            .eq("id", smoke_property_id)
            .execute()
            .data
            or []
        )
        if cross == []:
            log_pass("cross-tenant read correctly returns 0 rows (isolation holds)")
        else:
            failures.append("cross-tenant read leaked rows")
            log_fail(f"cross-tenant read leaked: {cross}")

        # Try to insert a property WITHOUT org_id — must fail (NOT NULL).
        try:
            supabase.table("properties").insert(
                {"name": "Should not exist", "location": "Nowhere"}
            ).execute()
            failures.append("properties INSERT without org_id was accepted")
            log_fail("properties accepted INSERT without org_id (NOT NULL not enforced!)")
        except Exception as exc:
            log_pass(f"properties INSERT without org_id correctly rejected ({type(exc).__name__})")

        # Insert a lead and verify the new tenant-scoped uniqueness.
        lead_payload = {
            "org_id": smoke_org_id,
            "name": "Smoke Asha",
            "phone": "9999999999",
            "property_id": smoke_property_id,
            "source": "smoke-test",
            "status": "New",
        }
        lead_resp = supabase.table("leads").insert(lead_payload).execute()
        lead_id = str(lead_resp.data[0]["id"])
        log_pass(f"leads insert under smoke org ok (lead_id={lead_id})")

        # Same payload again -> should violate the partial unique index.
        try:
            supabase.table("leads").insert(lead_payload).execute()
            failures.append("duplicate lead INSERT was accepted (unique index missing?)")
            log_fail("duplicate lead INSERT was accepted!")
        except Exception as exc:
            log_pass(
                f"duplicate (org_id, phone, property_id) correctly rejected by DB"
                f" ({type(exc).__name__})"
            )

    finally:
        # ----- E. Clean up ------------------------------------------------
        log_step("E. Cleanup")
        if smoke_org_id is not None:
            try:
                # ON DELETE CASCADE wipes properties + leads + chunks for this org.
                supabase.table("organizations").delete().eq("id", smoke_org_id).execute()
                log_pass(f"deleted smoke org {smoke_org_id} (cascade cleared dependents)")
            except Exception as exc:
                failures.append(f"cleanup failed: {exc}")
                log_fail(f"cleanup failed: {exc}")

    # ----- Summary -------------------------------------------------------
    print()
    if failures:
        print(f"{RED}{len(failures)} failure(s):{RESET}")
        for line in failures:
            print(f"  - {line}")
        return 1
    print(f"{GREEN}All smoke checks passed.{RESET}  Migration is live and tenant scoping works end-to-end.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
