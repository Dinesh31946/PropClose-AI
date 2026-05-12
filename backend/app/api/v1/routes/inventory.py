import json
from typing import Any

from fastapi import APIRouter, HTTPException
from openai import OpenAI
from pydantic import BaseModel, Field

from app.core.config import Settings
from app.core.tenancy import TenantDep
from app.db.supabase_client import get_supabase_client
from app.rag.embedder import Embedder

router = APIRouter()


class InventoryMapRequest(BaseModel):
    headers: list[str] = Field(default_factory=list)


class InventoryUpsertRequest(BaseModel):
    data: list[dict[str, Any]] = Field(default_factory=list)
    mapping: dict[str, str] = Field(default_factory=dict)


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_property_name(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalize_price(value: Any, header_name: str) -> str | None:
    if value is None:
        return None
    raw = str(value).lower().replace(",", "").strip()
    if not raw:
        return None

    multiplier = 1
    if "cr" in raw or "crore" in raw:
        multiplier = 10000000
    elif "l" in raw or "lac" in raw or "lakh" in raw:
        multiplier = 100000
    else:
        header = (header_name or "").lower()
        if "cr" in header or "crore" in header:
            multiplier = 10000000
        elif "lakh" in header or "lac" in header:
            multiplier = 100000

    digits = "".join(char for char in raw if char.isdigit() or char == ".")
    if not digits:
        return None
    return str(int(round(float(digits) * multiplier)))


def _build_unit_key(item: dict[str, Any]) -> str:
    """Stable unique key for batch + DB dedup.

        Enterprise isolation layer - mandatory for SaaS scalability.
    Includes ``org_id`` so two tenants can have a "Tower-A / Floor-3 /
    2BHK" without colliding inside this batch hash.
    """
    return "|".join(
        [
            str(item.get("org_id") or ""),
            str(item.get("project_id") or ""),
            str(item.get("unit_name") or "").strip().lower(),
            str(item.get("floor_no") or "").strip().lower(),
            str(item.get("configuration") or "").strip().lower(),
        ]
    )


@router.post("/inventory/map")
def map_inventory_headers(payload: InventoryMapRequest, tenant: TenantDep) -> dict[str, Any]:
    settings = Settings.load()
    if not payload.headers:
        raise HTTPException(status_code=400, detail="headers are required.")

    # tenant.org_id is intentionally not used in the prompt; OpenAI sees only
    # the column header strings.  We still depend on TenantDep so unauthenticated
    # callers cannot probe our LLM.
    _ = tenant

    system_prompt = f"""You are a real estate data expert.
Map the "User Headers" to our "System Columns" or "metadata".

CORE SYSTEM COLUMNS:
- unit_name (Flat/Shop No)
- floor_no
- configuration (1BHK, 2BHK, etc.)
- carpet_area
- price
- status

USER HEADERS: {", ".join(payload.headers)}

RULES:
1. If you find a column like "Building", "Project", or "Property", map it to "project_name".
2. If a header matches a CORE column, map it (example: {{"Unit": "unit_name"}}).
3. If a header is extra but useful (for example: Facing, Balcony, Parking, PLC), map it to "metadata".
4. Return ONLY a valid JSON object.
5. Format: {{"ExcelHeader": "MappedColumn"}}"""

    client = OpenAI(api_key=settings.openai_api_key)
    response = client.chat.completions.create(
        model=settings.openai_model,
        messages=[{"role": "system", "content": system_prompt}],
        response_format={"type": "json_object"},
        temperature=0,
    )
    message = response.choices[0].message.content or "{}"
    try:
        mapping = json.loads(message)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Invalid map output: {exc}") from exc

    return {"success": True, "mapping": mapping}


@router.post("/inventory/upsert")
def upsert_inventory(payload: InventoryUpsertRequest, tenant: TenantDep) -> dict[str, Any]:
    """Tenant-scoped inventory upsert.

        Enterprise isolation layer - mandatory for SaaS scalability.
    Properties lookup, batch dedup, DB existence check, and the final
    INSERT are ALL filtered by ``tenant.org_id``.  A row landing under
    the wrong tenant would silently corrupt RAG answers, so every step
    here is defensive.
    """
    settings = Settings.load()
    supabase = get_supabase_client()
    embedder = Embedder(settings)
    org_id = tenant.org_id

    if not payload.data:
        raise HTTPException(status_code=400, detail="data is required.")
    if not payload.mapping:
        raise HTTPException(status_code=400, detail="mapping is required.")

    properties_response = (
        supabase.table("properties")
        .select("id,name")
        .eq("org_id", org_id)
        .execute()
    )
    properties = properties_response.data or []

    transformed_rows: list[dict[str, Any]] = []
    price_header = next((k for k, v in payload.mapping.items() if v == "price"), "")
    for row in payload.data:
        project_header = next((k for k, v in payload.mapping.items() if v == "project_name"), "")
        excel_project_name = _normalize_text(row.get(project_header)) if project_header else ""
        normalized_excel_project_name = _normalize_property_name(excel_project_name)

        matched_property = None
        if normalized_excel_project_name:
            for prop in properties:
                normalized_db = _normalize_property_name(prop.get("name"))
                if normalized_db and normalized_db == normalized_excel_project_name:
                    matched_property = prop
                    break
            if not matched_property:
                # Data integrity is priority #1 for this AI Lead Engine; strict
                # matching prevents cross-project AND cross-tenant data leaks.
                raise HTTPException(
                    status_code=400,
                    detail="Property Name Mismatch: Please use the exact name from your Properties list.",
                )

        unit_name = _normalize_text(
            row.get(next((k for k, v in payload.mapping.items() if v == "unit_name"), ""))
        ) or None
        floor_no = _normalize_text(
            row.get(next((k for k, v in payload.mapping.items() if v == "floor_no"), ""))
        ) or None
        configuration = _normalize_text(
            row.get(next((k for k, v in payload.mapping.items() if v == "configuration"), ""))
        ) or None
        carpet_area = _normalize_text(
            row.get(next((k for k, v in payload.mapping.items() if v == "carpet_area"), ""))
        ) or None
        status = _normalize_text(
            row.get(next((k for k, v in payload.mapping.items() if v == "status"), ""))
        ) or "Available"
        price = _normalize_price(row.get(price_header), price_header)

        summary = (
            f"Project: {_normalize_text(matched_property.get('name') if matched_property else excel_project_name) or 'Unknown'}. "
            f"Unit: {unit_name or 'Unknown'}. Price: INR {price or 'NA'}. "
            f"Config: {configuration or 'NA'}. Area: {carpet_area or 'NA'} sqft. "
            f"Floor: {floor_no or 'NA'}. Status: {status}."
        )

        transformed_rows.append(
            {
                "org_id": org_id,
                "project_id": matched_property.get("id") if matched_property else None,
                "unit_name": unit_name,
                "floor_no": floor_no,
                "configuration": configuration,
                "carpet_area": carpet_area,
                "price": price,
                "status": status,
                "ai_summary": summary,
                "metadata": {
                    "listing_type": "Project Based" if excel_project_name else "Individual Listing",
                    "original_project_name": excel_project_name or "Unknown",
                },
            }
        )

    # Duplicate guard 1: dedupe within incoming batch before hitting database.
    unique_rows: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for item in transformed_rows:
        key = _build_unit_key(item)
        if key not in seen_keys:
            seen_keys.add(key)
            unique_rows.append(item)

    # Duplicate guard 2: skip rows that already exist in database for same unique
    # key fields under the SAME tenant.
    project_ids = list({row["project_id"] for row in unique_rows if row.get("project_id")})
    existing_keys: set[str] = set()
    if project_ids:
        existing_rows = (
            supabase.table("unit_inventory")
            .select("org_id,project_id,unit_name,floor_no,configuration")
            .eq("org_id", org_id)
            .in_("project_id", project_ids)
            .execute()
            .data
            or []
        )
        existing_keys = {_build_unit_key(existing) for existing in existing_rows}

    insert_rows = [row for row in unique_rows if _build_unit_key(row) not in existing_keys]
    if not insert_rows:
        return {"success": True, "org_id": org_id, "inserted_count": 0, "message": "No new unique units to insert."}

    summaries = [row["ai_summary"] for row in insert_rows]
    embeddings = embedder.embed_texts(summaries)
    if len(embeddings) != len(insert_rows):
        raise HTTPException(status_code=500, detail="Embedding generation mismatch for inventory.")

    for index, item in enumerate(insert_rows):
        item["embedding"] = embeddings[index]

    if any(not row.get("embedding") for row in insert_rows):
        raise HTTPException(status_code=500, detail="Missing embeddings for one or more inventory rows.")

    supabase.table("unit_inventory").insert(insert_rows).execute()
    return {"success": True, "org_id": org_id, "inserted_count": len(insert_rows)}
