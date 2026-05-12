from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.core.tenancy import TenantDep
from app.schemas.leads import ExternalLeadIngestRequest, LeadCreateRequest
from app.services.ingestion_service import (
    PHONE_REQUIRED_DETAIL,
    LeadIngestionService,
    build_external_summary,
    normalize_phone,
    normalize_text,
)

router = APIRouter()

# Re-export for tests / callers that imported from routes before.
__all__ = [
    "LeadCreateRequest",
    "ExternalLeadIngestRequest",
    "PHONE_REQUIRED_DETAIL",
    "router",
    "create_lead",
    "ingest_external_lead",
]

_lead_ingestion = LeadIngestionService()


def _extract_google_fields(rows: list[dict[str, Any]]) -> dict[str, str]:
    extracted: dict[str, str] = {}
    for item in rows:
        column_id = normalize_text(item.get("column_id")).upper()
        string_value = normalize_text(item.get("string_value"))
        if not string_value:
            continue
        extracted[column_id] = string_value
    return extracted


def _extract_meta_fields(rows: list[dict[str, Any]]) -> dict[str, str]:
    extracted: dict[str, str] = {}
    for item in rows:
        name = normalize_text(item.get("name")).lower()
        values = item.get("values") or []
        if not name or not isinstance(values, list) or not values:
            continue
        first = normalize_text(values[0])
        if first:
            extracted[name] = first
    return extracted


def _first_non_empty(*values: Any) -> str | None:
    for value in values:
        normalized = normalize_text(value)
        if normalized:
            return normalized
    return None


@router.post("/leads")
def create_lead(
    payload: LeadCreateRequest,
    background_tasks: BackgroundTasks,
    tenant: TenantDep,
) -> dict[str, Any]:
    return _lead_ingestion.create_lead_impl(
        payload, background_tasks, org_id=tenant.org_id
    )


@router.post("/leads/external")
def ingest_external_lead(
    payload: ExternalLeadIngestRequest,
    background_tasks: BackgroundTasks,
    tenant: TenantDep,
) -> dict[str, Any]:
    google_fields = _extract_google_fields(payload.user_column_data)
    meta_fields = _extract_meta_fields(payload.field_data)

    name = _first_non_empty(
        payload.name,
        google_fields.get("FULL_NAME"),
        meta_fields.get("full_name"),
        meta_fields.get("name"),
    )
    phone = _first_non_empty(
        payload.phone,
        google_fields.get("PHONE_NUMBER"),
        meta_fields.get("phone_number"),
    )
    email = _first_non_empty(
        payload.email,
        google_fields.get("EMAIL"),
        meta_fields.get("email"),
    )
    property_name = _first_non_empty(
        payload.property_name,
        google_fields.get("PROPERTY_NAME"),
        meta_fields.get("property_name"),
        meta_fields.get("project_name"),
    )

    phone_normalized = normalize_phone(phone or "")
    if not phone_normalized:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{PHONE_REQUIRED_DETAIL} "
                "External: include phone / PHONE_NUMBER / phone_number in payload or fetch lead by "
                "external_lead_id from the provider API before calling this endpoint."
            ),
        )

    configuration = _first_non_empty(
        payload.configuration,
        google_fields.get("CONFIGURATION"),
        google_fields.get("BHK"),
        meta_fields.get("configuration"),
        meta_fields.get("bhk"),
    )
    budget = _first_non_empty(
        payload.budget,
        google_fields.get("BUDGET"),
        meta_fields.get("budget"),
        meta_fields.get("expected_price"),
        meta_fields.get("price"),
    )

    normalized = LeadCreateRequest(
        name=name or "Unknown Lead",
        phone=phone_normalized,
        email=email,
        source=_first_non_empty(payload.source, payload.platform) or "External",
        property_name=property_name,
        status="New",
        ai_summary=_first_non_empty(payload.message, build_external_summary(payload)),
        needs_attention=False,
        configuration=configuration,
        budget=budget,
    )
    external_db_fields = {
        "platform": normalize_text(payload.platform) or "unknown",
        "external_lead_id": normalize_text(payload.external_lead_id) or None,
        "listing_external_id": normalize_text(payload.listing_external_id) or None,
        "campaign_id": normalize_text(payload.campaign_id) or None,
        "ad_id": normalize_text(payload.ad_id) or None,
        "adgroup_id": normalize_text(payload.adgroup_id) or None,
        "form_id": normalize_text(payload.form_id) or None,
        "gcl_id": normalize_text(payload.gcl_id) or None,
        "is_test": payload.is_test,
        "lead_submit_time": payload.lead_submit_time.isoformat() if payload.lead_submit_time else None,
        "raw_payload": payload.raw_payload or None,
    }
    result = _lead_ingestion.create_lead_impl(
        normalized, background_tasks, org_id=tenant.org_id, extra_db_fields=external_db_fields
    )
    return {
        **result,
        "platform": external_db_fields["platform"],
        "external_lead_id": external_db_fields["external_lead_id"],
        "listing_external_id": external_db_fields["listing_external_id"],
        "campaign_id": external_db_fields["campaign_id"],
        "ad_id": external_db_fields["ad_id"],
        "adgroup_id": external_db_fields["adgroup_id"],
        "form_id": external_db_fields["form_id"],
        "gcl_id": external_db_fields["gcl_id"],
        "is_test": payload.is_test,
    }
