"""Pydantic models for lead ingestion APIs (shared by routes + services)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field


class LeadCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=1, max_length=120)
    email: EmailStr | None = None
    source: str = Field(default="Direct API", max_length=100)
    property_name: str | None = Field(default=None, max_length=160)
    status: str = Field(default="New", max_length=50)
    ai_summary: str | None = None
    needs_attention: bool = False
    configuration: str | None = Field(
        default=None,
        max_length=80,
        description=(
            "Inventory configuration hint for unit linking: BHK (e.g. 2 BHK), product type "
            "(Shop, Office, Flat, Villa, Plot, …). Must appear in ``unit_inventory.configuration``."
        ),
    )
    budget: str | None = Field(
        default=None,
        max_length=120,
        description="Budget or price hint for proximity ranking (e.g. 1.25 Cr, 85 Lakh, 9500000).",
    )


class ExternalLeadIngestRequest(BaseModel):
    platform: str = Field(default="unknown", max_length=60)
    source: str = Field(default="External", max_length=100)
    external_lead_id: str | None = Field(default=None, max_length=120)
    listing_external_id: str | None = Field(default=None, max_length=120)
    campaign_id: str | None = Field(default=None, max_length=120)
    ad_id: str | None = Field(default=None, max_length=120)
    adgroup_id: str | None = Field(default=None, max_length=120)
    form_id: str | None = Field(default=None, max_length=120)
    gcl_id: str | None = Field(default=None, max_length=120)
    lead_submit_time: datetime | None = None
    is_test: bool = False
    name: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=30)
    email: EmailStr | None = None
    property_name: str | None = Field(default=None, max_length=160)
    message: str | None = Field(default=None, max_length=2000)
    user_column_data: list[dict[str, Any]] = Field(default_factory=list)
    field_data: list[dict[str, Any]] = Field(default_factory=list)
    raw_payload: dict[str, Any] | None = None
    configuration: str | None = Field(default=None, max_length=80)
    budget: str | None = Field(default=None, max_length=120)
