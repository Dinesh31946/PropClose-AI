import logging
from typing import Any, Dict, List

from supabase import Client

from app.services.ingestion_service import normalize_property_name, normalize_text

logger = logging.getLogger(__name__)


class ChatRepository:
    """Tenant-scoped data access for the chat service.

        Enterprise isolation layer - mandatory for SaaS scalability.
    Every query in this class takes ``org_id`` as a required argument.
    The composite index ``(org_id, id)`` (and ``(org_id, lead_id, created_at)``
    for chat_history) keeps these lookups sub-millisecond at scale.
    """

    def __init__(self, client: Client) -> None:
        self.client = client

    def get_lead(self, lead_id: str, org_id: str) -> Dict[str, Any] | None:
        response = (
            self.client.table("leads")
            .select("*")
            .eq("org_id", org_id)
            .eq("id", lead_id)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        return rows[0] if rows else None

    def get_inventory_unit(self, unit_id: str, org_id: str) -> Dict[str, Any] | None:
        """Single catalogue row pinned on the lead (``leads.matched_unit_id``)."""
        response = (
            self.client.table("unit_inventory")
            .select(
                "id,org_id,project_id,unit_name,configuration,floor_no,carpet_area,price,"
                "status,ai_summary"
            )
            .eq("org_id", org_id)
            .eq("id", unit_id)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        return rows[0] if rows else None

    def get_property(self, property_id: str, org_id: str) -> Dict[str, Any] | None:
        response = (
            self.client.table("properties")
            .select("*")
            .eq("org_id", org_id)
            .eq("id", property_id)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        return rows[0] if rows else None

    def lookup_property_id_by_name(
        self, org_id: str, property_name: str | None
    ) -> str | None:
        """Resolve ``properties.id`` from a human project name (same rules as ingestion).

        Returns ``None`` on no match — never raises (chat path must stay soft).
        """
        name = normalize_text(property_name or "")
        if not name:
            return None
        response = (
            self.client.table("properties")
            .select("id,name")
            .eq("org_id", org_id)
            .execute()
        )
        rows: list[dict[str, Any]] = response.data or []
        normalized_input = normalize_property_name(name)
        for row in rows:
            if normalize_property_name(row.get("name")) == normalized_input:
                return str(row.get("id"))
        logger.warning(
            "[RAG] property name lookup miss org_id=%s property_name=%r",
            org_id,
            name,
        )
        return None

    def get_recent_history(
        self, lead_id: str, org_id: str, limit: int = 6
    ) -> List[Dict[str, Any]]:
        response = (
            self.client.table("chat_history")
            .select("role,content,created_at")
            .eq("org_id", org_id)
            .eq("lead_id", lead_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        history = response.data or []
        history.reverse()
        return history

    def save_messages(
        self,
        lead_id: str,
        org_id: str,
        user_message: str,
        assistant_message: str,
    ) -> None:
        self.client.table("chat_history").insert(
            [
                {"org_id": org_id, "lead_id": lead_id, "role": "user", "content": user_message},
                {"org_id": org_id, "lead_id": lead_id, "role": "assistant", "content": assistant_message},
            ]
        ).execute()

    def mark_site_visit_scheduled(self, lead_id: str, org_id: str) -> None:
        self.client.table("leads").update(
            {"status": "Site Visit Scheduled", "needs_attention": True}
        ).eq("org_id", org_id).eq("id", lead_id).execute()

    def mark_needs_attention(self, lead_id: str, org_id: str) -> None:
        self.client.table("leads").update({"needs_attention": True}).eq(
            "org_id", org_id
        ).eq("id", lead_id).execute()
