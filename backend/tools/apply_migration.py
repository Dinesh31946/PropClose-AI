"""Apply a SQL migration against the live Supabase Postgres.

Usage:
    .\\backend\\venv\\Scripts\\python.exe backend\\tools\\apply_migration.py docs\\migrations\\002_rls.sql

Reads:
    * SUPABASE_URL from .env.local       (used to derive the project ref)
    * docs/supabase-pass.txt             (database password)

The script runs the file as a single transaction.  On failure it rolls
back and prints the exact server error.  On success it commits and prints
the elapsed time.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402

from app.core.config import Settings  # noqa: E402

GREEN = "\033[92m"
RED = "\033[91m"
DIM = "\033[2m"
RESET = "\033[0m"


def _project_ref(supabase_url: str) -> str:
    host = urlparse(supabase_url).hostname or ""
    # https://<ref>.supabase.co -> <ref>
    return host.split(".")[0]


def _read_db_password() -> str:
    repo_root = Path(__file__).resolve().parents[2]
    pass_file = repo_root / "docs" / "supabase-pass.txt"
    if not pass_file.exists():
        raise SystemExit(
            f"Database password file not found at {pass_file}. "
            "Either create it (project DB password from Supabase Project Settings → Database) "
            "or set SUPABASE_DB_PASSWORD as an env var."
        )
    return pass_file.read_text(encoding="utf-8").strip()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: apply_migration.py <path/to/file.sql>", file=sys.stderr)
        return 2

    sql_path = Path(argv[1]).resolve()
    if not sql_path.exists():
        print(f"{RED}File not found: {sql_path}{RESET}", file=sys.stderr)
        return 2

    settings = Settings.load()
    if not settings.supabase_url:
        print(f"{RED}SUPABASE_URL not set in env / .env.local.{RESET}", file=sys.stderr)
        return 1

    project_ref = _project_ref(settings.supabase_url)
    password = _read_db_password()

    # Direct connection.  If the user's project disabled IPv4 they can swap
    # to the pooler URL by setting SUPABASE_DB_HOST manually.
    conn_str = (
        f"host=db.{project_ref}.supabase.co "
        f"port=5432 "
        f"dbname=postgres "
        f"user=postgres "
        f"password={password} "
        f"sslmode=require "
        f"connect_timeout=10"
    )

    sql = sql_path.read_text(encoding="utf-8")
    print(f"{DIM}Applying {sql_path.name} ({len(sql):,} chars) to db.{project_ref}.supabase.co{RESET}")

    started = time.monotonic()
    try:
        with psycopg.connect(conn_str, autocommit=False) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
    except psycopg.errors.Error as exc:
        elapsed = time.monotonic() - started
        print(f"{RED}Migration FAILED after {elapsed:.2f}s:{RESET}")
        print(f"  {type(exc).__name__}: {exc}")
        if getattr(exc, "diag", None):
            print(f"  detail: {exc.diag.message_detail}")
            print(f"  hint:   {exc.diag.message_hint}")
        return 1
    elapsed = time.monotonic() - started
    print(f"{GREEN}Migration applied successfully in {elapsed:.2f}s.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
