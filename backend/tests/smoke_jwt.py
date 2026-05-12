"""Live HTTP smoke check for the JWT verification path.

    Enterprise isolation layer - mandatory for SaaS scalability.

Reads the test JWT secret from backend/.smoke-jwt-secret (set by the
parent shell that started uvicorn) and proves that:

    * a JWT signed with the right secret is accepted (200),
    * a JWT signed with the WRONG secret is rejected (401, bad signature),
    * an EXPIRED JWT is rejected (401, expired),
    * a JWT without an org_id claim is rejected (401),
    * a JWT with the wrong audience is rejected (401),
    * a request without ANY auth still gets 401 (gate intact).
"""
from __future__ import annotations

import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import jwt as pyjwt

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import Settings  # noqa: E402
from app.db.supabase_client import get_supabase_client  # noqa: E402

API = "http://127.0.0.1:8000"
SECRET_FILE = ROOT / ".smoke-jwt-secret"

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


def mint_jwt(
    secret: str,
    org_id: str,
    *,
    audience: str = "authenticated",
    exp_delta_seconds: int = 600,
    sub: str = "test-user",
    org_id_in_app_metadata: bool = False,
) -> str:
    now = datetime.now(timezone.utc)
    payload: dict = {
        "sub": sub,
        "aud": audience,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=exp_delta_seconds)).timestamp()),
        "role": "authenticated",
    }
    if org_id_in_app_metadata:
        payload["app_metadata"] = {"org_id": org_id}
    else:
        payload["org_id"] = org_id
    return pyjwt.encode(payload, secret, algorithm="HS256")


def main() -> int:
    settings = Settings.load()
    if not settings.supabase_url:
        print(f"{RED}Missing SUPABASE_URL in .env.local.{RESET}")
        return 1

    # Prefer the secret loaded by FastAPI itself, so the JWTs we mint here
    # are signed with the SAME key uvicorn verifies them with.  Falling
    # back to .smoke-jwt-secret keeps the older "ephemeral secret" workflow
    # alive for environments without a real Supabase legacy secret.
    secret = settings.supabase_jwt_secret
    secret_source = ".env.local (SUPABASE_JWT_SECRET)"
    if not secret:
        if not SECRET_FILE.exists():
            print(f"{RED}No SUPABASE_JWT_SECRET in .env.local and {SECRET_FILE} is missing.{RESET}")
            return 1
        secret = SECRET_FILE.read_text(encoding="ascii").strip()
        secret_source = SECRET_FILE.name
    print(f"{DIM}Using SUPABASE_JWT_SECRET from {secret_source} ({len(secret)} chars){RESET}")

    failures: list[str] = []
    supabase = get_supabase_client()

    smoke_slug = f"smoke-jwt-{int(time.time())}"
    org_id: str | None = None
    try:
        org_resp = (
            supabase.table("organizations")
            .insert({"name": "Smoke JWT Org", "slug": smoke_slug, "subscription_tier": "trial"})
            .execute()
        )
        org_id = str(org_resp.data[0]["id"])
        passed(f"created smoke org {org_id} (slug={smoke_slug})")

        supabase.table("properties").insert(
            {"org_id": org_id, "name": "Skyline Towers", "location": "Pune"}
        ).execute()

        # ----- 1. Valid JWT (top-level org_id) ---------------------------
        step("1. Valid JWT, org_id at top level")
        token = mint_jwt(secret, org_id)
        r = httpx.post(
            f"{API}/api/v1/leads",
            json={"name": "Asha", "phone": "9100000001", "property_name": "Skyline Towers"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if r.status_code == 200 and r.json().get("org_id") == org_id:
            passed(f"200 OK; lead_id={r.json()['lead_id']}")
        else:
            failures.append("valid JWT was not accepted")
            failed(f"{r.status_code}: {r.text}")

        # ----- 2. Valid JWT with org_id under app_metadata ---------------
        step("2. Valid JWT, org_id nested under app_metadata")
        token = mint_jwt(secret, org_id, org_id_in_app_metadata=True)
        r = httpx.post(
            f"{API}/api/v1/leads",
            json={"name": "Asha", "phone": "9100000002", "property_name": "Skyline Towers"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if r.status_code == 200 and r.json().get("org_id") == org_id:
            passed(f"app_metadata.org_id resolves correctly; lead_id={r.json()['lead_id']}")
        else:
            failures.append("nested claim path failed")
            failed(f"{r.status_code}: {r.text}")

        # ----- 3. Forged JWT (wrong secret) ------------------------------
        step("3. Forged JWT signed with the WRONG secret")
        wrong_secret = "definitely-not-the-right-secret-1234567890"
        token = mint_jwt(wrong_secret, org_id)
        r = httpx.post(
            f"{API}/api/v1/leads",
            json={"name": "Asha", "phone": "9100000003", "property_name": "Skyline Towers"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if r.status_code == 401 and "signature" in r.json().get("detail", "").lower():
            passed(f"401 with signature error ({r.json()['detail']})")
        else:
            failures.append("forged JWT was not rejected as bad signature")
            failed(f"{r.status_code}: {r.text}")

        # ----- 4. Expired JWT --------------------------------------------
        step("4. Expired JWT (exp in the past)")
        token = mint_jwt(secret, org_id, exp_delta_seconds=-60)
        r = httpx.post(
            f"{API}/api/v1/leads",
            json={"name": "Asha", "phone": "9100000004", "property_name": "Skyline Towers"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if r.status_code == 401 and "expired" in r.json().get("detail", "").lower():
            passed(f"401 with expired-token error ({r.json()['detail']})")
        else:
            failures.append("expired JWT was not rejected")
            failed(f"{r.status_code}: {r.text}")

        # ----- 5. JWT without an org_id claim ----------------------------
        step("5. JWT without an org_id claim")
        now = datetime.now(timezone.utc)
        token = pyjwt.encode(
            {
                "sub": "u",
                "aud": "authenticated",
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=10)).timestamp()),
            },
            secret,
            algorithm="HS256",
        )
        r = httpx.post(
            f"{API}/api/v1/leads",
            json={"name": "Asha", "phone": "9100000005", "property_name": "Skyline Towers"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if r.status_code == 401 and "org_id" in r.json().get("detail", "").lower():
            passed(f"401 with missing-claim error ({r.json()['detail']})")
        else:
            failures.append("JWT without org_id claim was not rejected")
            failed(f"{r.status_code}: {r.text}")

        # ----- 6. JWT with wrong audience --------------------------------
        step("6. JWT with the wrong audience")
        token = mint_jwt(secret, org_id, audience="not-authenticated")
        r = httpx.post(
            f"{API}/api/v1/leads",
            json={"name": "Asha", "phone": "9100000006", "property_name": "Skyline Towers"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if r.status_code == 401 and "audience" in r.json().get("detail", "").lower():
            passed(f"401 with audience error ({r.json()['detail']})")
        else:
            failures.append("wrong-audience JWT was not rejected")
            failed(f"{r.status_code}: {r.text}")

        # ----- 7. No auth at all -----------------------------------------
        step("7. No auth at all (sanity: gate intact)")
        r = httpx.post(
            f"{API}/api/v1/leads",
            json={"name": "Asha", "phone": "9100000007", "property_name": "Skyline Towers"},
            timeout=15,
        )
        if r.status_code == 401:
            passed(f"unauthenticated request -> 401")
        else:
            failures.append("missing tenant context did not return 401")
            failed(f"{r.status_code}: {r.text}")

        # ----- 8. JWT does NOT silently fall through to header ----------
        step("8. Bad JWT does NOT fall through even when X-Org-Id is also present")
        token = mint_jwt(wrong_secret, org_id)
        r = httpx.post(
            f"{API}/api/v1/leads",
            json={"name": "Asha", "phone": "9100000008", "property_name": "Skyline Towers"},
            headers={"Authorization": f"Bearer {token}", "X-Org-Id": org_id},
            timeout=15,
        )
        if r.status_code == 401:
            passed("Bad JWT + valid X-Org-Id -> 401 (no fall-through, no confusion attack)")
        else:
            failures.append("bad JWT was masked by X-Org-Id (fall-through is a security hole)")
            failed(f"{r.status_code}: {r.text}")

    finally:
        step("Cleanup")
        if org_id:
            try:
                supabase.table("organizations").delete().eq("id", org_id).execute()
                passed(f"deleted smoke org {org_id} (cascade cleared dependents)")
            except Exception as exc:
                failures.append(f"cleanup failed: {exc}")
                failed(f"cleanup failed: {exc}")

    print()
    if failures:
        print(f"{RED}{len(failures)} failure(s):{RESET}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"{GREEN}All JWT smoke checks passed.{RESET}  Signature, expiry, audience, and missing-claim paths are all enforced.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
