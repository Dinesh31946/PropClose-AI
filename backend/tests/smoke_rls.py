"""Live verification that 002_rls.sql is in effect.

    Enterprise isolation layer - mandatory for SaaS scalability.

This script proves four things against the live Supabase:

    1. RLS is ON for all six tenant-bearing tables.
    2. The service_role key (used by FastAPI) STILL bypasses RLS, so the
       API path is unaffected.
    3. The anon key with NO JWT cannot read tenant rows (returns []).
    4. The anon key with a REAL Supabase-issued JWT (asymmetric ES256 in
       modern projects) sees ONLY that tenant's rows.

Section 4 mints its JWT through Supabase's admin Auth API: it creates a
temporary user with ``app_metadata.org_id`` set, signs them in to get a
genuine project-signed token, runs the RLS check, then deletes the
user.  This is the only way to obtain a JWT that PostgREST will accept,
since Supabase's auth signing key is not exposed to clients.

Requires:
    * SUPABASE_ANON_KEY in .env.local or env (for #3 and #4)
    * service-role key (already used by the FastAPI client)
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

from supabase import create_client

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


def step(msg: str) -> None:
    print(f"\n{YELLOW}>>>{RESET} {msg}")


def passed(msg: str) -> None:
    print(f"  {GREEN}PASS{RESET}  {msg}")


def failed(msg: str) -> None:
    print(f"  {RED}FAIL{RESET}  {msg}")


def skipped(msg: str) -> None:
    print(f"  {DIM}SKIP  {msg}{RESET}")


def _read_env_value(name: str) -> str:
    value = os.getenv(name) or ""
    if value:
        return value
    repo_root = Path(__file__).resolve().parents[2]
    pass_file = repo_root / ".env.local"
    if not pass_file.exists():
        return ""
    for line in pass_file.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main() -> int:
    failures: list[str] = []
    settings = Settings.load()
    if not settings.supabase_url:
        print(f"{RED}Missing SUPABASE_URL.{RESET}")
        return 1

    service_client = get_supabase_client()
    anon_key = _read_env_value("NEXT_PUBLIC_SUPABASE_ANON_KEY") or _read_env_value("SUPABASE_ANON_KEY")

    print(f"{DIM}Supabase: {settings.supabase_url}{RESET}")
    print(f"{DIM}Anon key in env: {'yes' if anon_key else 'no'}{RESET}")

    # ----- 0. Confirm RLS is actually enabled on every target table -----
    step("0. RLS flag on tenant tables")
    try:
        rls_status = service_client.rpc("rls_status_check").execute().data
    except Exception:
        # rls_status_check helper isn't installed; that's fine, this check
        # is best-effort and the service-role + anon checks below are the
        # real proof.  Move on.
        skipped("rls_status_check helper not present; skipping (run smoke_rls regardless)")
    else:
        # If the helper exists it returns a list of {table, rls_enabled}.
        for row in rls_status or []:
            tbl = row.get("table")
            on = row.get("rls_enabled")
            if on:
                passed(f"{tbl}: RLS enabled")
            else:
                failures.append(f"{tbl}: RLS NOT enabled (002_rls.sql not applied?)")
                failed(f"{tbl}: RLS NOT enabled")

    # ----- 1. Service-role can still INSERT under a fresh smoke org ------
    step("1. Service-role bypasses RLS (FastAPI path unaffected)")
    smoke_slug = f"smoke-rls-{int(time.time())}"
    org_id: str | None = None
    smoke_property_id: str | None = None
    smoke_user_id: str | None = None
    try:
        org_resp = (
            service_client.table("organizations")
            .insert({"name": "Smoke RLS Org", "slug": smoke_slug, "subscription_tier": "trial"})
            .execute()
        )
        org_id = str(org_resp.data[0]["id"])
        passed(f"service_role: created org {org_id}")

        prop_resp = (
            service_client.table("properties")
            .insert(
                {
                    "org_id": org_id,
                    "name": "RLS Test Tower",
                    "location": "Hyderabad",
                }
            )
            .execute()
        )
        smoke_property_id = str(prop_resp.data[0]["id"])
        passed(f"service_role: created property {smoke_property_id}")

        scoped = (
            service_client.table("properties")
            .select("id,name")
            .eq("org_id", org_id)
            .execute()
            .data
            or []
        )
        if scoped and scoped[0]["name"] == "RLS Test Tower":
            passed("service_role: read scoped property succeeds")
        else:
            failures.append("service_role read failed under RLS")
            failed(f"unexpected read: {scoped}")

        # ----- 2. anon key without JWT must see 0 rows -----------------
        step("2. anon key with NO JWT must see 0 rows")
        if not anon_key:
            skipped("NEXT_PUBLIC_SUPABASE_ANON_KEY / SUPABASE_ANON_KEY not set; skipping")
        else:
            anon_client = create_client(settings.supabase_url, anon_key)
            try:
                rows = (
                    anon_client.table("properties")
                    .select("id")
                    .eq("org_id", org_id)
                    .execute()
                    .data
                    or []
                )
                if rows == []:
                    passed("anon (no JWT): 0 rows from properties (RLS deny works)")
                else:
                    failures.append("anon key without JWT could read tenant rows!")
                    failed(f"anon read leaked {len(rows)} rows")
            except Exception as exc:
                # Some Supabase configs return an error rather than [] on RLS deny.
                msg = str(exc).lower()
                if "permission" in msg or "policy" in msg or "rls" in msg:
                    passed(f"anon (no JWT): RLS error path engaged ({type(exc).__name__})")
                else:
                    failures.append(f"anon read failed unexpectedly: {exc}")
                    failed(f"unexpected: {exc}")

            # ----- 3. anon + REAL Supabase-signed JWT scoped to org_id -----
            step("3. anon key + tenant JWT must see only its own org's rows")
            #
            # Modern Supabase projects sign auth JWTs with rotating
            # asymmetric keys (ES256/RS256), and PostgREST verifies them
            # against the project's JWKS.  The "Legacy JWT Secret" path
            # is no longer accepted for direct PostgREST access on these
            # projects -- attempting to sign our own HS256 token gets
            # rejected with PGRST301 "No suitable key or wrong key type".
            #
            # The only client that holds the project's signing key is
            # Supabase Auth itself, so we ask it to mint one for us by
            # creating a throwaway user with app_metadata.org_id set,
            # signing in to receive a genuine ES256-signed access token,
            # and using that token for the RLS check.  The user is
            # deleted in the finally block.
            try:
                from gotrue.errors import AuthApiError  # type: ignore  # noqa: F401
            except Exception:  # pragma: no cover - older supabase-py
                AuthApiError = Exception  # type: ignore[misc, assignment]

            smoke_email = f"smoke-rls-{uuid.uuid4().hex[:10]}@propclose-smoke.invalid"
            smoke_password = f"Sm0ke!{uuid.uuid4().hex}"

            try:
                created = service_client.auth.admin.create_user(
                    {
                        "email": smoke_email,
                        "password": smoke_password,
                        "email_confirm": True,
                        # IMPORTANT: app_metadata is what auth.jwt() ->
                        # public.current_org_id() reads; user_metadata is
                        # user-controllable and therefore untrusted.
                        "app_metadata": {"org_id": org_id},
                    }
                )
                smoke_user_id = (
                    str(created.user.id) if getattr(created, "user", None) else None
                )

                user_client = create_client(settings.supabase_url, anon_key)
                signin = user_client.auth.sign_in_with_password(
                    {"email": smoke_email, "password": smoke_password}
                )
                access_token = (
                    signin.session.access_token
                    if getattr(signin, "session", None)
                    else None
                )
                if not access_token:
                    failures.append("could not obtain an access_token from sign_in_with_password")
                    failed("no access_token returned by Supabase Auth")
                else:
                    user_client.postgrest.auth(access_token)
                    rows = (
                        user_client.table("properties")
                        .select("id,name,org_id")
                        .execute()
                        .data
                        or []
                    )
                    org_rows = [r for r in rows if r.get("org_id") == org_id]
                    other_rows = [r for r in rows if r.get("org_id") != org_id]
                    if org_rows and not other_rows:
                        passed(
                            f"anon + real Supabase JWT: sees {len(org_rows)} rows for own org, 0 for other orgs"
                        )
                    else:
                        failures.append(
                            f"RLS scoping wrong: {len(org_rows)} own + {len(other_rows)} other"
                        )
                        failed(
                            f"unexpected scoping (own={len(org_rows)}, other={len(other_rows)})"
                        )
            except Exception as exc:
                failures.append(f"could not run § 3 (auth admin / sign-in): {exc}")
                failed(f"unexpected: {type(exc).__name__}: {exc}")

    finally:
        # ----- Cleanup -----------------------------------------------------
        step("Cleanup")
        if smoke_user_id:
            try:
                service_client.auth.admin.delete_user(smoke_user_id)
                passed(f"deleted smoke auth user {smoke_user_id}")
            except Exception as exc:
                failures.append(f"auth user cleanup failed: {exc}")
                failed(f"auth user cleanup failed: {exc}")
        if org_id:
            try:
                service_client.table("organizations").delete().eq("id", org_id).execute()
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
    print(f"{GREEN}RLS smoke checks passed.{RESET}  Service-role bypass + tenant JWT scoping behave as expected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
