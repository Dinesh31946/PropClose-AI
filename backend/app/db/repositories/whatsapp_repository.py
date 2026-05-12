"""Tenant-scoped data access for the WhatsApp webhook pipeline.

    Enterprise isolation layer - mandatory for SaaS scalability.
Every method takes ``org_id`` and uses it on EVERY query.  The composite
indexes ``(org_id, direction, message_id)`` (uniq) and
``(org_id, lead_id, created_at)`` keep these calls sub-millisecond.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from postgrest.exceptions import APIError
from supabase import Client

logger = logging.getLogger(__name__)


# Postgres error code for a unique-constraint violation: this is how we
# detect "this message_id has already been claimed in another worker".
PG_UNIQUE_VIOLATION = "23505"


def _digits_only(value: str | None) -> str:
    """Strip EVERY non-digit character.

        Enterprise isolation layer - mandatory for SaaS scalability.
    Phones reach us in three different shapes depending on the source:
    Google ad forms send ``+91 98765 43210``, Meta WhatsApp sends
    ``919876543210``, manual broker entry is a coin flip.  This helper
    is the single canonical reduction we use for *comparison*.  The
    raw value is still stored in the DB; we just widen the lookup so
    `+`-prefixed and bare-digit forms can match each other.
    """
    if not value:
        return ""
    return re.sub(r"\D+", "", str(value))


def _phone_query_candidates(phone: str | None) -> list[str]:
    """Return all wire-form variants to try against ``leads.phone``.

        Enterprise isolation layer - mandatory for SaaS scalability.
    Order is most-specific → most-permissive so the first index hit wins:

        1. Original wire form          (caller's input as-is, e.g. ``+91 98765 43210``)
        2. Digits-only canonical       (``919876543210``)
        3. Digits-only with ``+``       (``+919876543210``, E.164-ish)

    Every candidate uses indexed equality on ``(org_id, phone)``, so
    the worst case is 3 sub-millisecond lookups instead of 1 — still
    well within the WhatsApp BackgroundTask budget.
    """
    if not phone:
        return []
    raw = str(phone).strip()
    digits = _digits_only(raw)
    if not digits:
        return []

    candidates: list[str] = []
    if raw and raw not in candidates:
        candidates.append(raw)
    if digits not in candidates:
        candidates.append(digits)
    plus_form = "+" + digits
    if plus_form not in candidates:
        candidates.append(plus_form)
    return candidates


class WhatsAppRepository:
    def __init__(self, client: Client) -> None:
        self.client = client

    # --------------------------------------------------------------
    # Idempotency
    # --------------------------------------------------------------

    def claim_inbound(
        self,
        *,
        org_id: str,
        message_id: str,
        from_phone: str,
        to_phone: str,
        body: str,
        raw_payload: dict[str, Any],
    ) -> bool:
        """Atomically reserve ``(org_id, 'inbound', message_id)``.

        Returns True when this worker is the first to see this message
        (and therefore should run the RAG pipeline), False when another
        retry already claimed it.  Race-free thanks to the partial
        unique index ``whatsapp_messages_org_dir_msgid_uniq``.
        """
        try:
            self.client.table("whatsapp_messages").insert(
                {
                    "org_id": org_id,
                    "message_id": message_id,
                    "direction": "inbound",
                    "from_phone": from_phone,
                    "to_phone": to_phone,
                    "body": body,
                    "status": "received",
                    "raw_payload": raw_payload,
                }
            ).execute()
            return True
        except APIError as exc:
            code = getattr(exc, "code", None)
            if code == PG_UNIQUE_VIOLATION:
                logger.info(
                    "WhatsApp dedup: message_id=%s already claimed for org=%s",
                    message_id,
                    org_id,
                )
                return False
            raise

    def mark_inbound_processed(
        self,
        *,
        org_id: str,
        message_id: str,
        lead_id: str | None,
        property_id: str | None,
        error: str | None = None,
    ) -> None:
        update = {
            "status": "processed" if not error else "failed",
            "lead_id": lead_id,
            "property_id": property_id,
        }
        if error:
            update["error_detail"] = error[:500]
        try:
            (
                self.client.table("whatsapp_messages")
                .update(update)
                .eq("org_id", org_id)
                .eq("direction", "inbound")
                .eq("message_id", message_id)
                .execute()
            )
        except APIError as exc:
            logger.warning(
                "Could not mark inbound %s processed for org=%s: %s",
                message_id,
                org_id,
                exc,
            )

    def log_outbound(
        self,
        *,
        org_id: str,
        wamid: str | None,
        to_phone: str,
        from_phone_number_id: str,
        body: str,
        lead_id: str | None,
        property_id: str | None,
        success: bool,
        error: str | None,
    ) -> None:
        # Synthetic id when Meta didn't return one (dry-run, failure):
        # we still want a row in the message log for the broker UI.
        message_id = wamid or f"local-{org_id}-{int(__import__('time').time() * 1000)}"
        try:
            self.client.table("whatsapp_messages").insert(
                {
                    "org_id": org_id,
                    "message_id": message_id,
                    "direction": "outbound",
                    "from_phone": from_phone_number_id,
                    "to_phone": to_phone,
                    "body": body,
                    "status": "sent" if success else "failed",
                    "error_detail": (error or None) and str(error)[:500],
                    "lead_id": lead_id,
                    "property_id": property_id,
                }
            ).execute()
        except APIError as exc:
            logger.warning(
                "Could not write outbound log for org=%s wamid=%s: %s",
                org_id,
                wamid,
                exc,
            )

    # --------------------------------------------------------------
    # Lookups
    # --------------------------------------------------------------

    def get_org_by_slug(self, slug: str) -> dict[str, Any] | None:
        if not slug:
            return None
        rows = (
            self.client.table("organizations")
            .select("id,slug,subscription_tier,name")
            .eq("slug", slug)
            .limit(1)
            .execute()
            .data
            or []
        )
        return rows[0] if rows else None

    def find_lead_by_phone(
        self, *, org_id: str, phone: str
    ) -> dict[str, Any] | None:
        """Locate a lead by phone, scoped to ``org_id``, prefix-agnostic.

            Enterprise isolation layer - mandatory for SaaS scalability.

        Production reality: the same lead can be stored as ``+919876543210``
        when ingested via Google forms but Meta WhatsApp delivers it as
        ``919876543210``.  Rather than backfill the table, we widen the
        lookup so EITHER stored form is found from EITHER input form.

        If the broker deletes an old row and inserts a fresh lead with the
        same phone, ordering by ``created_at desc`` always surfaces the newest
        record — nothing is keyed off ephemeral session state outside this row.

        Implementation: query ``(org_id, phone)`` against each candidate
        in ``_phone_query_candidates`` and return the first hit.  All
        queries are tenant-scoped — a phone living under a different
        org will NEVER cross-match into this org's results.
        """
        for candidate in _phone_query_candidates(phone):
            rows = (
                self.client.table("leads")
                .select("id,phone,property_id,name,status")
                .eq("org_id", org_id)
                .eq("phone", candidate)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
                .data
                or []
            )
            if rows:
                return rows[0]
        return None
