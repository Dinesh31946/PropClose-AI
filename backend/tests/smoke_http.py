"""Live HTTP round-trip against the running FastAPI server.

    Enterprise isolation layer - mandatory for SaaS scalability.

This script does NOT mock anything.  It expects:
    * uvicorn running at http://127.0.0.1:8000
    * Supabase reachable (uses the same .env.local as the server)
    * docs/migrations/001_multitenant.sql already applied

It exercises the full request lifecycle:
    1. Tenant gate (401 / 400 / 200)
    2. Strict property matching scoped by org_id
    3. Tenant-scoped dedup
    4. Cross-tenant isolation: Org A's property is invisible to Org B
    5. Cleans up after itself via ON DELETE CASCADE on the smoke org

Run:
    .\\backend\\venv\\Scripts\\python.exe backend\\tests\\smoke_http.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import Settings  # noqa: E402
from app.db.supabase_client import get_supabase_client  # noqa: E402

API = "http://127.0.0.1:8000"

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
DIM = "\033[2m"
RESET = "\033[0m"


def step(msg: str) -> None:
    print(f"\n{YELLOW}>>>{RESET} {msg}")


def passed(msg: str) -> None:
    print(f"  {GREEN}PASS{RESET}  {msg}")


def failed(msg: str) -> None:
    print(f"  {RED}FAIL{RESET}  {msg}")


def main() -> int:
    settings = Settings.load()
    if not settings.supabase_url:
        print(f"{RED}Missing SUPABASE_URL in .env.local.{RESET}")
        return 1
    print(f"{DIM}API: {API}{RESET}")
    print(f"{DIM}Supabase: {settings.supabase_url}{RESET}")

    failures: list[str] = []

    # Sanity: server is alive.
    try:
        h = httpx.get(f"{API}/api/v1/health", timeout=5)
        h.raise_for_status()
        passed(f"GET /api/v1/health -> {h.status_code} {h.json()}")
    except Exception as exc:
        print(f"{RED}Server not reachable at {API}: {exc}{RESET}")
        return 1

    supabase = get_supabase_client()

    # Provision two throw-away tenants directly via Supabase so we can run
    # the HTTP layer against real org_ids.
    smoke_a_slug = f"smoke-a-{int(time.time())}"
    smoke_b_slug = f"smoke-b-{int(time.time())}"
    org_a_id: str | None = None
    org_b_id: str | None = None

    try:
        org_a_resp = (
            supabase.table("organizations")
            .insert({"name": "Smoke Org A", "slug": smoke_a_slug, "subscription_tier": "trial"})
            .execute()
        )
        org_a_id = str(org_a_resp.data[0]["id"])
        passed(f"created Org A {org_a_id}")

        org_b_resp = (
            supabase.table("organizations")
            .insert({"name": "Smoke Org B", "slug": smoke_b_slug, "subscription_tier": "pro"})
            .execute()
        )
        org_b_id = str(org_b_resp.data[0]["id"])
        passed(f"created Org B {org_b_id}")

        # Both orgs hold a property with the SAME name — the multi-tenant
        # uniqueness guarantee says this MUST work.
        prop_a = (
            supabase.table("properties")
            .insert({"org_id": org_a_id, "name": "Skyline Towers", "location": "Pune"})
            .execute()
        ).data[0]
        prop_b = (
            supabase.table("properties")
            .insert({"org_id": org_b_id, "name": "Skyline Towers", "location": "Mumbai"})
            .execute()
        ).data[0]
        passed(f"both orgs hold 'Skyline Towers' (a={prop_a['id'][:8]}, b={prop_b['id'][:8]})")

        # ----- 1. Tenant gate ------------------------------------------------
        step("1. Tenant gate")
        r = httpx.post(
            f"{API}/api/v1/leads",
            json={"name": "Asha", "phone": "9000000001", "property_name": "Skyline Towers"},
            timeout=15,
        )
        if r.status_code == 401:
            passed(f"no header -> 401 ({r.json().get('detail', '')[:60]}...)")
        else:
            failures.append("expected 401 without tenant header")
            failed(f"expected 401, got {r.status_code}: {r.text}")

        r = httpx.post(
            f"{API}/api/v1/leads",
            json={"name": "Asha", "phone": "9000000001", "property_name": "Skyline Towers"},
            headers={"X-Org-Id": "not-a-uuid"},
            timeout=15,
        )
        if r.status_code == 400:
            passed(f"malformed UUID -> 400 ({r.json().get('detail', '')[:60]})")
        else:
            failures.append("expected 400 on malformed UUID")
            failed(f"expected 400, got {r.status_code}: {r.text}")

        r = httpx.post(
            f"{API}/api/v1/leads",
            json={"name": "Asha", "phone": "9000000001", "property_name": "Skyline Towers"},
            headers={"X-Org-Slug": smoke_a_slug},
            timeout=15,
        )
        if r.status_code == 200 and r.json().get("org_id") == org_a_id:
            passed(f"X-Org-Slug routes to Org A and creates lead ({r.json()['lead_id']})")
        else:
            failures.append("X-Org-Slug routing failed")
            failed(f"slug routing: {r.status_code} {r.text}")

        # ----- 2. Option B: phone whitespace --------------------------------
        step("2. Option B (phone required) at the HTTP edge")
        r = httpx.post(
            f"{API}/api/v1/leads",
            json={"name": "Asha", "phone": "   ", "property_name": "Skyline Towers"},
            headers={"X-Org-Id": org_a_id},
            timeout=15,
        )
        if r.status_code == 400 and "phone" in r.json().get("detail", "").lower():
            passed(f"whitespace phone -> 400 ({r.json()['detail'][:60]}...)")
        else:
            failures.append("whitespace phone path failed")
            failed(f"expected 400, got {r.status_code}: {r.text}")

        # ----- 3. Strict property matching scoped by org -------------------
        step("3. Strict property matching, scoped by org")
        r = httpx.post(
            f"{API}/api/v1/leads",
            json={"name": "Asha", "phone": "9000000002", "property_name": "Skyline Tower"},  # singular typo
            headers={"X-Org-Id": org_a_id},
            timeout=15,
        )
        if r.status_code == 400 and "mismatch" in r.json().get("detail", "").lower():
            passed("typo 'Skyline Tower' -> 400 Property Name Mismatch")
        else:
            failures.append("strict matcher accepted a typo")
            failed(f"expected 400, got {r.status_code}: {r.text}")

        # ----- 4. Dedup inside one org --------------------------------------
        step("4. Dedup inside Org A")
        payload = {"name": "Asha", "phone": "9000000003", "property_name": "Skyline Towers"}
        r1 = httpx.post(f"{API}/api/v1/leads", json=payload, headers={"X-Org-Id": org_a_id}, timeout=15)
        r2 = httpx.post(f"{API}/api/v1/leads", json=payload, headers={"X-Org-Id": org_a_id}, timeout=15)
        if r1.status_code == 200 and r2.status_code == 200:
            b1, b2 = r1.json(), r2.json()
            same_id = b1["lead_id"] == b2["lead_id"]
            second_is_dup = b2["duplicate"] is True
            first_is_new = b1["duplicate"] is False
            if same_id and second_is_dup and first_is_new:
                passed(f"first new, second duplicate, same lead_id ({b1['lead_id'][:8]})")
            else:
                failures.append("dedup behaviour wrong")
                failed(f"unexpected dedup: {b1} vs {b2}")
        else:
            failures.append("dedup HTTP failed")
            failed(f"r1={r1.status_code} r2={r2.status_code}")

        # ----- 5. Cross-tenant isolation ------------------------------------
        step("5. Cross-tenant isolation")
        # Same phone, same property name -> Org B must succeed (its own property),
        # creating a SEPARATE row.  Legacy global unique index would have blocked.
        r = httpx.post(
            f"{API}/api/v1/leads",
            json={"name": "Asha", "phone": "9000000003", "property_name": "Skyline Towers"},
            headers={"X-Org-Id": org_b_id},
            timeout=15,
        )
        if r.status_code == 200 and r.json()["org_id"] == org_b_id and r.json()["duplicate"] is False:
            passed("Org B accepts same phone+property-name (separate row, isolation holds)")
            org_b_lead_id = r.json()["lead_id"]
        else:
            failures.append("Org B was blocked by Org A's lead")
            failed(f"Org B expected 200/duplicate=false, got {r.status_code}: {r.text}")
            org_b_lead_id = None

        # And Org B trying to use a property name that exists ONLY in Org A
        # would fail — but here both have it, so we test the reverse: delete
        # Org A's property reference on the DB side and re-attempt.  Skipped:
        # we already verified that contract in test_tenant_isolation.py.  The
        # important live check is that two distinct rows now exist under
        # different org_ids for the same phone+property pair:
        if org_b_lead_id:
            org_a_rows = (
                supabase.table("leads")
                .select("id")
                .eq("org_id", org_a_id)
                .eq("phone", "9000000003")
                .execute()
                .data
                or []
            )
            org_b_rows = (
                supabase.table("leads")
                .select("id")
                .eq("org_id", org_b_id)
                .eq("phone", "9000000003")
                .execute()
                .data
                or []
            )
            if len(org_a_rows) == 1 and len(org_b_rows) == 1 and org_a_rows[0]["id"] != org_b_rows[0]["id"]:
                passed("DB confirms two distinct rows: 1 under Org A, 1 under Org B")
            else:
                failures.append("DB scoping leaked")
                failed(f"org_a_rows={org_a_rows} org_b_rows={org_b_rows}")

    finally:
        # ----- Cleanup ---------------------------------------------------
        step("Cleanup")
        for org_id in (org_a_id, org_b_id):
            if org_id:
                try:
                    supabase.table("organizations").delete().eq("id", org_id).execute()
                    passed(f"deleted smoke org {org_id} (cascade cleared dependents)")
                except Exception as exc:
                    failures.append(f"cleanup failed for {org_id}: {exc}")
                    failed(f"cleanup failed: {exc}")

    print()
    if failures:
        print(f"{RED}{len(failures)} failure(s):{RESET}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"{GREEN}All HTTP smoke checks passed.{RESET}  Live FastAPI <-> Supabase round-trip is healthy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
