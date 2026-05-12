import logging
import re
from typing import Any

from app.core.config import Settings
from app.db.supabase_client import get_supabase_client
from app.services.whatsapp_service import WhatsAppClient

logger = logging.getLogger(__name__)


def _digits_phone(value: str) -> str:
    """Match ``leads.phone`` shape: digits only (no spaces, ``+``, dashes)."""
    return re.sub(r"[\s\-\(\)\+]", "", str(value or "")).strip()


class AutomationService:
    """Lead automation hooks.

        Enterprise isolation layer - mandatory for SaaS scalability.
    After a successful lead write, ``send_welcome_message`` resolves the tenant,
    resolves the outbound WhatsApp recipient from ``leads.phone``, and —
    only when WhatsApp credentials are configured and **not** ``WHATSAPP_DRY_RUN``
    sends a templated onboarding text via the Meta Graph ``messages`` API.
    Until then the method remains a structured ``logger.info`` hook so CI and
    local dev never require Meta keys.
    """

    def __init__(self) -> None:
        self.supabase = get_supabase_client()

    def send_welcome_message(self, lead_id: str, org_id: str) -> None:
        """Deliver (or simulate) WhatsApp onboarding for ``lead_id``.

        Loads the canonical lead row + property name strictly under ``org_id``.
        On mis-configured outbound transport we LOG + return — never raising
        so API background tasks remain safe.
        """
        if not org_id:
            logger.warning("Automation skipped: missing org_id for lead_id=%s", lead_id)
            return

        lead_response = (
            self.supabase.table("leads")
            .select("id,name,property_id,org_id,phone")
            .eq("org_id", org_id)
            .eq("id", lead_id)
            .limit(1)
            .execute()
        )
        lead_rows: list[dict[str, Any]] = lead_response.data or []
        if not lead_rows:
            logger.warning(
                "Automation skipped: lead not found for org_id=%s lead_id=%s",
                org_id,
                lead_id,
            )
            return

        lead = lead_rows[0]
        lead_name = str(lead.get("name") or "there")
        to_phone = _digits_phone(str(lead.get("phone") or ""))
        property_id = lead.get("property_id")

        property_name = "your property"
        if property_id:
            property_response = (
                self.supabase.table("properties")
                .select("id,name,org_id")
                .eq("org_id", org_id)
                .eq("id", property_id)
                .limit(1)
                .execute()
            )
            property_rows: list[dict[str, Any]] = property_response.data or []
            if property_rows:
                property_name = str(property_rows[0].get("name") or property_name)

        body = (
            f"Namaste {lead_name}, thanks for your interest in «{property_name}». "
            f"You can reply here anytime — we'll share accurate pricing and details from our brochure & inventory.\n\n"
            f"(PropClose automated welcome)"
        )

        logger.info(
            "Automation WhatsApp welcome | org=%s | Lead %s (%s) | Property %s",
            org_id,
            lead_name,
            to_phone or "no-phone",
            property_name,
        )

        settings = Settings.load()
        if settings.whatsapp_dry_run:
            logger.info(
                "WhatsApp outbound skipped: WHATSAPP_DRY_RUN is enabled (welcome would go to=%s)",
                to_phone,
            )
            return

        if not to_phone:
            logger.warning(
                "WhatsApp outbound skipped: lead row has empty phone lead_id=%s",
                lead_id,
            )
            return

        client = WhatsAppClient(settings)
        if not client.configured:
            logger.warning(
                "WhatsApp outbound skipped: access_token or phone_number_id not configured; "
                "set WHATSAPP_ACCESS_TOKEN / WHATSAPP_PHONE_NUMBER_ID for live sends."
            )
            return

        result = client.send_text(to_phone, body)
        if result.success:
            logger.info(
                "WhatsApp welcome sent lead_id=%s to=%s wamid=%s",
                lead_id,
                to_phone,
                result.wamid,
            )
        else:
            logger.warning(
                "WhatsApp welcome failed lead_id=%s to=%s status=%s err=%s",
                lead_id,
                to_phone,
                result.status_code,
                result.error,
            )

    def process_new_lead(self, lead_id: str, org_id: str) -> None:
        """Public alias invoked after CRM insertupsert pathways need the same automation."""
        self.send_welcome_message(lead_id, org_id)


__all__ = ["AutomationService"]
