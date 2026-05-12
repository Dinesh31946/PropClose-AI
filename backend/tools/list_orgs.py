"""Quick diagnostic: list every organization in the live Supabase project.

Run from backend/:
    .\\venv\\Scripts\\python.exe .\\tools\\list_orgs.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.supabase_client import get_supabase_client


def main() -> int:
    rows = (
        get_supabase_client()
        .table("organizations")
        .select("id,slug,name,subscription_tier,created_at")
        .order("created_at")
        .execute()
        .data
        or []
    )
    print(f"Found {len(rows)} organisation(s):")
    for r in rows:
        print(
            f"  - slug={r['slug']!r:30s} "
            f"name={r['name']!r:30s} "
            f"tier={r['subscription_tier']:10s} "
            f"id={r['id']}"
        )
    if not rows:
        print("  (no organisations seeded)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
