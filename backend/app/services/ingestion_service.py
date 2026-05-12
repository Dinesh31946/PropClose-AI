"""Lead ingestion: property resolution and CRM writes.

    Enterprise isolation layer - mandatory for SaaS scalability.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import BackgroundTasks, HTTPException
from postgrest.exceptions import APIError

from app.db.supabase_client import get_supabase_client
from app.schemas.leads import ExternalLeadIngestRequest, LeadCreateRequest
from app.services.automation_service import AutomationService

logger = logging.getLogger(__name__)

PHONE_REQUIRED_DETAIL = (
    "A valid phone number is required. Our leads table stores phone as NOT NULL for CRM reliability; "
    "email-only leads must be enriched with phone upstream (portal webhook, Meta/Google field mapping)."
)


def normalize_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_property_name(value: Any) -> str:
    return str(value or "").strip().lower()


def normalize_phone(phone: str) -> str:
    normalized = re.sub(r"[\s\-\(\)]", "", str(phone or ""))
    return normalized.strip()


def collapse_bhk_token(text: str) -> str:
    """Normalize BHK-ish strings for comparison (2 BHK ≈ 2bhk)."""
    raw = normalize_text(text).lower()
    return re.sub(r"\s+", "", raw)


# Product-type cues (commercial property vs residential — keep in sync with field ops vocabulary).
_CONFIGURATION_TYPE_TERMS = (
    "commercial",
    "warehouse",
    "showroom",
    "apartment",
    "bungalow",
    "penthouse",
    "retail",
    "office",
    "villa",
    "studio",
    "plot",
    "flat",
    "shop",
)


def category_keywords_present_in_hint(hint_lower: str) -> list[str]:
    """Whole-word-ish detection of Flat / Shop / Office / Plot / etc."""
    hl = normalize_text(hint_lower).lower()
    if not hl:
        return []
    found: list[str] = []
    for kw in _CONFIGURATION_TYPE_TERMS:
        if re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", hl):
            found.append(kw)
    seen: set[str] = set()
    out = []
    for k in sorted(found, key=len, reverse=True):
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def bhk_hints_from_configuration_text(hint: str) -> list[str]:
    """Collapsed tokens such as ``3bhk`` when the enquiry mentions BR / BHK."""
    hl = normalize_text(hint).lower()
    hints: list[str] = []
    for m in re.finditer(r"\b(\d{1,2})\s*(?:bhk|br|bedroom|bedrms?)\b", hl, flags=re.IGNORECASE):
        hints.append(collapse_bhk_token(f"{m.group(1)} BHK"))
    seen: set[str] = set()
    out = []
    for h in hints:
        if h and h not in seen:
            seen.add(h)
            out.append(h)
    return out


def row_matches_configuration_filter(unit_configuration: Any, enquiry_hint: str) -> bool:
    """Flexible configuration gate: Shop / Flat / Office / BHK phrases from the enquiry.

    * If the lead names a property type keyword (Shop, Office, Flat, …), ``unit_inventory.configuration``
      must contain that keyword (substring, case-insensitive).
    * If the lead names a ``N BHK`` / ``N BR`` pattern, it must align with that unit's ``configuration``.
    * If the hint is empty, no configuration filter applies.
    """
    q = normalize_text(enquiry_hint)
    if not q:
        return True

    ql = q.lower()
    cfg_raw = normalize_text(str(unit_configuration or ""))
    if not cfg_raw:
        return False
    cfg_l = cfg_raw.lower()

    keywords = category_keywords_present_in_hint(ql)
    bhk_hints = bhk_hints_from_configuration_text(q)

    if not keywords and not bhk_hints:
        return True

    for kw in keywords:
        if kw not in cfg_l:
            return False

    cfg_c = collapse_bhk_token(cfg_raw)
    for collapsed in bhk_hints:
        if collapsed not in cfg_c:
            return False

    return True


def row_matches_bhk_filter(unit_configuration: Any, bhk_query: str) -> bool:
    """Backward-compat alias — use ``row_matches_configuration_filter``."""
    return row_matches_configuration_filter(unit_configuration, bhk_query)


def parse_price_to_inr_rupees(value: Any) -> float | None:
    """Best-effort single-number INR amount for ranking (same semantics as inventory CSV price)."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        return v if v > 0 else None
    raw = str(value).lower().replace(",", "").strip()
    if not raw:
        return None

    multiplier = 1.0
    if "cr" in raw or "crore" in raw:
        multiplier = 10_000_000.0
    elif "l" in raw or "lac" in raw or "lakh" in raw:
        multiplier = 100_000.0

    digits = "".join(char for char in raw if char.isdigit() or char == ".")
    if not digits:
        return None
    try:
        amt = float(digits) * multiplier
    except ValueError:
        return None
    return amt if amt > 0 else None


def price_match_confidence(unit_inr: float, target_inr: float) -> float:
    """Score in [0, 1]: 1.0 = exact (or near-exact), lower = further from budget."""
    if unit_inr <= 0 or target_inr <= 0:
        return 0.5
    diff = abs(unit_inr - target_inr)
    if diff < max(100.0, target_inr * 0.0001):
        return 1.0
    rel = diff / max(target_inr, 1.0)
    return max(0.35, min(0.99, 1.0 - min(rel, 1.0)))


def sanitize_external_db_fields(extra_db_fields: dict[str, Any] | None) -> dict[str, Any]:
    if not extra_db_fields:
        return {}
    sanitized: dict[str, Any] = {}
    for key, value in extra_db_fields.items():
        if value is None:
            continue
        if isinstance(value, str):
            normalized = normalize_text(value)
            if normalized:
                sanitized[key] = normalized
        else:
            sanitized[key] = value
    return sanitized


def map_lead_db_error_to_http(exc: APIError) -> None:
    code = getattr(exc, "code", None) or (
        exc.json().get("code") if hasattr(exc, "json") else None
    )
    message = f"{getattr(exc, 'message', '') or ''} {getattr(exc, 'details', '') or ''}"
    if code == "23502" and "phone" in message.lower():
        raise HTTPException(status_code=400, detail=PHONE_REQUIRED_DETAIL) from exc
    raise exc


def build_external_summary(payload: ExternalLeadIngestRequest) -> str:
    lines = [
        f"platform={normalize_text(payload.platform) or 'unknown'}",
        f"external_lead_id={normalize_text(payload.external_lead_id) or 'na'}",
        f"listing_external_id={normalize_text(payload.listing_external_id) or 'na'}",
        f"campaign_id={normalize_text(payload.campaign_id) or 'na'}",
        f"ad_id={normalize_text(payload.ad_id) or 'na'}",
        f"adgroup_id={normalize_text(payload.adgroup_id) or 'na'}",
        f"form_id={normalize_text(payload.form_id) or 'na'}",
        f"gcl_id={normalize_text(payload.gcl_id) or 'na'}",
        f"is_test={payload.is_test}",
    ]
    return "ExternalLeadContext: " + " | ".join(lines)


class LeadIngestionService:
    """Tenant-scoped lead create / upsert + strict property name resolution."""

    def resolve_property_id(self, property_name: str | None, org_id: str) -> str | None:
        """Strict, tenant-scoped property name → properties.id.

        The same project name can exist in two different organizations; we
        must NEVER let Broker A's lead get linked to Broker B's project.
        """
        if not property_name:
            return None

        name = normalize_text(property_name)
        if not name:
            return None

        supabase = get_supabase_client()
        response = (
            supabase.table("properties")
            .select("id,name")
            .eq("org_id", org_id)
            .execute()
        )
        rows: list[dict[str, Any]] = response.data or []
        normalized_input = normalize_property_name(name)
        matches = [row for row in rows if normalize_property_name(row.get("name")) == normalized_input]
        if not matches:
            logger.warning("Property strict match failed for org_id=%s property_name=%s", org_id, name)
            raise HTTPException(
                status_code=400,
                detail="Property Name Mismatch: Please use the exact name from your Properties list.",
            )

        return str(matches[0].get("id"))

    def _find_matching_unit(
        self,
        org_id: str,
        property_id: str | None,
        configuration_hint: str | None,
        price_value: str | float | None,
    ) -> tuple[str | None, dict[str, Any]]:
        """Pick ONE ``unit_inventory`` row using project → configuration → price.

        Hierarchy: (1) ``project_id`` scope, (2) ``configuration`` keyword match — Shop / Flat /
        Office / BHK / … — (3) nearest price to budget.

        Returns ``(unit_uuid, match_metadata)``.
        """
        meta_base: dict[str, Any] = {
            "matched": False,
            "method": "project_configuration_price",
        }
        if not property_id:
            meta_base["reason"] = "no_property_id"
            return None, meta_base

        supabase = get_supabase_client()
        try:
            response = (
                supabase.table("unit_inventory")
                .select("id,configuration,price,unit_name")
                .eq("org_id", org_id)
                .eq("project_id", property_id)
                .execute()
            )
        except Exception as exc:
            logger.exception("[ingestion] unit_inventory load failed org_id=%s property_id=%s", org_id, property_id)
            meta_base["reason"] = "query_error"
            meta_base["error"] = str(exc)[:200]
            return None, meta_base

        rows: list[dict[str, Any]] = list(response.data or [])
        meta_base["candidates_inventory"] = len(rows)

        cfg_q = normalize_text(configuration_hint or "")
        filtered = (
            rows
            if not cfg_q
            else [r for r in rows if row_matches_configuration_filter(r.get("configuration"), cfg_q)]
        )
        meta_base["configuration_filter_applied"] = bool(cfg_q)
        meta_base["configuration_query"] = cfg_q or None
        hinted_types = category_keywords_present_in_hint(cfg_q.lower())
        hinted_bhk = bhk_hints_from_configuration_text(cfg_q)
        meta_base["configuration_hints"] = {"product_types": hinted_types, "bhk_tokens": hinted_bhk}

        if not filtered:
            meta_base["reason"] = (
                "no_units_after_configuration_filter"
                if cfg_q
                else "no_units_for_property"
            )
            meta_base["candidates_after_configuration"] = 0
            return None, meta_base

        meta_base["candidates_after_configuration"] = len(filtered)
        target_inr = parse_price_to_inr_rupees(price_value)

        scored: list[tuple[dict[str, Any], float, Any]] = []
        for row in filtered:
            u_inr = parse_price_to_inr_rupees(row.get("price"))
            if target_inr is not None and u_inr is not None:
                conf = price_match_confidence(u_inr, target_inr)
                # Prefer exact numeric match, then higher confidence
                exact = 1 if abs(u_inr - target_inr) < max(100.0, target_inr * 0.0001) else 0
                sort_key = (-exact, -conf, str(row.get("id") or ""))
            elif target_inr is not None and u_inr is None:
                conf = 0.55
                sort_key = (0, -conf, str(row.get("id") or ""))
            else:
                # No budget: still pin a row by BHK with neutral confidence
                conf = 0.75
                sort_key = (-conf, str(row.get("id") or ""))

            scored.append((row, conf, sort_key))

        scored.sort(key=lambda x: x[2])
        best_row, best_conf, _ = scored[0]
        uid = str(best_row.get("id") or "") or None
        if not uid:
            meta_base["reason"] = "invalid_unit_row"
            return None, meta_base

        u_inr = parse_price_to_inr_rupees(best_row.get("price"))
        out_meta = {
            **meta_base,
            "matched": True,
            "confidence": round(float(best_conf), 4),
            "selected_unit_id": uid,
            "selected_configuration": best_row.get("configuration"),
            "selected_price_raw": best_row.get("price"),
            "lead_budget_inr": target_inr,
            "unit_price_inr": u_inr,
        }
        return uid, out_meta

    def create_lead_impl(
        self,
        payload: LeadCreateRequest,
        background_tasks: BackgroundTasks,
        org_id: str,
        extra_db_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Tenant-scoped lead create / upsert."""
        supabase = get_supabase_client()
        automation_service = AutomationService()
        normalized_extra_fields = sanitize_external_db_fields(extra_db_fields)

        normalized_name = normalize_text(payload.name)
        normalized_phone = normalize_phone(payload.phone)
        normalized_email = normalize_text(payload.email).lower() or None
        if not normalized_phone:
            raise HTTPException(status_code=400, detail=PHONE_REQUIRED_DETAIL)

        property_id = self.resolve_property_id(payload.property_name, org_id=org_id)
        matched_unit_id, match_metadata = self._find_matching_unit(
            org_id,
            property_id,
            payload.configuration,
            payload.budget,
        )
        now_iso = datetime.now(timezone.utc).isoformat()

        insert_payload = {
            "org_id": org_id,
            "name": normalized_name,
            "phone": normalized_phone,
            "email": normalized_email,
            "source": normalize_text(payload.source) or "Direct API",
            "property_id": property_id,
            "status": normalize_text(payload.status) or "New",
            "ai_summary": normalize_text(payload.ai_summary) or None,
            "needs_attention": payload.needs_attention,
            "created_at": now_iso,
            "matched_unit_id": matched_unit_id,
            "match_metadata": match_metadata,
            **normalized_extra_fields,
        }

        if property_id:
            existing_before = (
                supabase.table("leads")
                .select("id")
                .eq("org_id", org_id)
                .eq("phone", normalized_phone)
                .eq("property_id", property_id)
                .limit(1)
                .execute()
                .data
                or []
            )
            upsert_rows: list[dict[str, Any]] = []
            try:
                upsert_response = (
                    supabase.table("leads")
                    .upsert(insert_payload, on_conflict="org_id,phone,property_id")
                    .execute()
                )
                upsert_rows = upsert_response.data or []
                if not upsert_rows:
                    raise HTTPException(status_code=500, detail="Failed to create or update lead.")
            except APIError as exc:
                error_code = getattr(exc, "code", None) or (
                    exc.json().get("code") if hasattr(exc, "json") else None
                )
                if error_code != "42P10":
                    map_lead_db_error_to_http(exc)
                logger.warning(
                    "Upsert fallback activated: missing unique constraint on (org_id, phone, property_id). "
                    "Apply docs/migrations/001_multitenant.sql for full concurrency safety."
                )
            else:
                lead_id = str(upsert_rows[0]["id"])
                is_duplicate = bool(existing_before)

                background_tasks.add_task(
                    automation_service.send_welcome_message, lead_id, org_id
                )
                return {
                    "success": True,
                    "lead_id": lead_id,
                    "org_id": org_id,
                    "property_id": property_id,
                    "matched_unit_id": matched_unit_id,
                    "match_metadata": match_metadata,
                    "duplicate": is_duplicate,
                    "message": (
                        "Lead created/updated successfully using concurrency-safe upsert."
                        if not is_duplicate
                        else "Existing lead updated with latest created_at timestamp."
                    ),
                }

        duplicate_query = (
            supabase.table("leads")
            .select("id,created_at")
            .eq("org_id", org_id)
            .limit(1)
        )
        duplicate_query = duplicate_query.eq("phone", normalized_phone)
        if property_id:
            duplicate_query = duplicate_query.eq("property_id", property_id)
        else:
            duplicate_query = duplicate_query.is_("property_id", None)

        duplicate_rows: list[dict[str, Any]] = (duplicate_query.execute().data or [])

        if duplicate_rows:
            existing_id = str(duplicate_rows[0]["id"])
            update_payload = {
                "created_at": now_iso,
                "matched_unit_id": matched_unit_id,
                "match_metadata": match_metadata,
                **normalized_extra_fields,
            }
            try:
                update_response = (
                    supabase.table("leads")
                    .update(update_payload)
                    .eq("org_id", org_id)
                    .eq("id", existing_id)
                    .execute()
                )
            except APIError as exc:
                map_lead_db_error_to_http(exc)
            if update_response.data is None:
                raise HTTPException(status_code=500, detail="Failed to refresh duplicate lead timestamp.")

            background_tasks.add_task(
                automation_service.send_welcome_message, existing_id, org_id
            )
            return {
                "success": True,
                "lead_id": existing_id,
                "org_id": org_id,
                "property_id": property_id,
                "matched_unit_id": matched_unit_id,
                "match_metadata": match_metadata,
                "duplicate": True,
                "message": "Existing lead found. created_at refreshed.",
            }

        try:
            insert_response = supabase.table("leads").insert(insert_payload).execute()
        except APIError as exc:
            map_lead_db_error_to_http(exc)
        inserted_rows: list[dict[str, Any]] = insert_response.data or []
        if not inserted_rows:
            raise HTTPException(status_code=500, detail="Failed to create lead.")

        lead_id = str(inserted_rows[0]["id"])
        background_tasks.add_task(
            automation_service.send_welcome_message, lead_id, org_id
        )

        return {
            "success": True,
            "lead_id": lead_id,
            "org_id": org_id,
            "property_id": property_id,
            "matched_unit_id": matched_unit_id,
            "match_metadata": match_metadata,
            "duplicate": False,
            "message": "Lead created successfully.",
        }


def get_lead_ingestion_service() -> LeadIngestionService:
    return LeadIngestionService()
