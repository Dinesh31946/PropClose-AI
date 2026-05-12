import logging
import uuid as uuid_mod
from typing import Any, Dict, List

from supabase import Client

from app.core.config import Settings
from app.rag.validators import should_prioritize_inventory_fallback

logger = logging.getLogger(__name__)

# Cosine similarity injected for rows loaded without ANN (deterministic scope).
# Must pass strict channel gates (e.g. WhatsApp ~0.7) while staying below 1.0
# so real near-duplicate vector hits are not mis-ranked.
_DETERMINISTIC_UNIT_SIMILARITY = 0.95
_DETERMINISTIC_CHUNK_SIMILARITY = 0.88


def _normalize_uuid(value: str | None) -> str | None:
    """Canonical UUID string for PostgREST RPC args and defensive Python filters.

    Handles whitespace, hyphenated vs compact forms, and rejects invalid tokens
    so mis-parsed lead UUIDs fail closed in Python instead of returning zero
    rows from Postgres with no explanation.
    """
    if value is None:
        return None
    raw = str(value).strip().lower()
    if not raw:
        return None
    try:
        parsed = uuid_mod.UUID(raw)
    except (ValueError, AttributeError):
        logger.warning("[RAG] invalid uuid string for scoping: %r", value)
        return None
    return str(parsed)


def _uuid_matches_left(a: Any, b_norm: str | None) -> bool:
    if b_norm is None:
        return True
    other = _normalize_uuid(str(a) if a is not None else "")
    return other == b_norm


class Retriever:
    """Tenant-scoped vector retriever.

        Enterprise isolation layer - mandatory for SaaS scalability.
    The ``org_id`` is filtered IN SQL via ``match_units`` / ``match_chunks``
    (see ``docs/migrations/001_multitenant.sql``) BEFORE the ANN scan
    runs.  We additionally filter again in Python as a defense-in-depth
    check — even if a misconfigured RPC ever returned cross-tenant rows,
    they would be discarded here before reaching the LLM prompt.

    Every log line emitted by this class carries the tenant ``org_id``,
    so a broker reading their own log stream can never see chunk_ids
    or similarity scores that belong to a different organisation.
    """

    def __init__(self, client: Client, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def _safe_rpc(self, name: str, args: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Call a Supabase RPC; log and return [] on failure (never crash RAG)."""
        try:
            resp = self.client.rpc(name, args).execute()
        except Exception:
            logger.exception("[RAG] RPC %s failed (args keys=%s)", name, sorted(args.keys()))
            return []
        data = getattr(resp, "data", None)
        if data is None:
            logger.warning("[RAG] RPC %s returned no data (check migration 005 signatures)", name)
            return []
        return list(data) if isinstance(data, list) else []

    def _list_units_scoped(
        self, org_uuid: str | None, property_uuid: str | None
    ) -> List[Dict[str, Any]]:
        """Bypass ANN — all units for this listing (org + project/property id)."""
        if not org_uuid or not property_uuid:
            return []
        try:
            resp = (
                self.client.table("unit_inventory")
                .select(
                    "id, org_id, project_id, unit_name, configuration, "
                    "floor_no, carpet_area, price, status, ai_summary"
                )
                .eq("org_id", org_uuid)
                .eq("project_id", property_uuid)
                .limit(max(self.settings.rag_top_k_units * 4, 8))
                .execute()
            )
        except Exception:
            logger.exception(
                "[RAG] unit_inventory scope query failed org_id=%s property_id=%s",
                org_uuid,
                property_uuid,
            )
            return []
        rows_raw = getattr(resp, "data", None)
        rows = rows_raw if isinstance(rows_raw, list) else []
        out: List[Dict[str, Any]] = []
        for u in rows or []:
            row = dict(u)
            row["similarity"] = _DETERMINISTIC_UNIT_SIMILARITY
            out.append(row)
        return out

    def _list_chunks_scoped(self, org_uuid: str | None, property_uuid: str | None) -> List[Dict[str, Any]]:
        """Bypass ANN — brochure rows for this listing (amenities / project copy)."""
        if not org_uuid or not property_uuid:
            return []
        try:
            resp = (
                self.client.table("brochure_chunks")
                .select("id, org_id, property_id, content")
                .eq("org_id", org_uuid)
                .eq("property_id", property_uuid)
                .limit(max(self.settings.rag_top_k_chunks * 4, 8))
                .execute()
            )
        except Exception:
            logger.exception(
                "[RAG] brochure_chunks scope query failed org_id=%s property_id=%s",
                org_uuid,
                property_uuid,
            )
            return []
        rows_raw = getattr(resp, "data", None)
        rows = rows_raw if isinstance(rows_raw, list) else []
        out: List[Dict[str, Any]] = []
        for c in rows or []:
            row = dict(c)
            row["similarity"] = _DETERMINISTIC_CHUNK_SIMILARITY
            out.append(row)
        return out

    @staticmethod
    def _merge_units_prefer_deterministic(
        deterministic: List[Dict[str, Any]], vector_hits: List[Dict[str, Any]], top_k: int
    ) -> List[Dict[str, Any]]:
        seen: set[Any] = set()
        merged: List[Dict[str, Any]] = []
        for u in deterministic:
            uid = u.get("id")
            if uid in seen:
                continue
            seen.add(uid)
            merged.append(u)
        for u in vector_hits:
            uid = u.get("id")
            if uid in seen:
                continue
            seen.add(uid)
            merged.append(u)
        return merged[:top_k]

    @staticmethod
    def _merge_chunks_vector_then_scoped(
        vector_hits: List[Dict[str, Any]],
        scoped_rows: List[Dict[str, Any]],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        """Prefer query-aligned vector chunks, then pad with scoped brochure rows.

        Listing-lock chats need stable **project-level** context (amenities, developer,
        location) even when ANN similarity for brochure text is weak—e.g. the model
        asks about amenities while the pinned unit row only shows economics/status.
        """
        seen: set[Any] = set()
        merged: List[Dict[str, Any]] = []
        for c in vector_hits:
            cid = c.get("id")
            if cid in seen:
                continue
            seen.add(cid)
            merged.append(c)
            if len(merged) >= top_k:
                return merged
        for c in scoped_rows:
            cid = c.get("id")
            if cid in seen:
                continue
            seen.add(cid)
            merged.append(c)
            if len(merged) >= top_k:
                break
        return merged

    def retrieve(
        self,
        query_embedding: list[float],
        property_id: str | None,
        org_id: str,
        *,
        query_text: str | None = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Run the vector ANN, scoped by org + (optional) property.

            Enterprise isolation layer - mandatory for SaaS scalability.

        ``property_id`` rules:
          * **str**  — "Specific Listing Lock": the SQL planner uses
            ``unit_inventory_org_project_idx`` and
            ``brochure_chunks_org_property_idx`` to prune to a single
            project before the cosine-distance computation.  This is
            the default path for every customer chat -- the AI may
            ONLY discuss the property the lead originally enquired about.
          * **None** — org-wide search.  ONLY used after the lead
            explicitly opts in via the redirect-and-confirm flow in
            ``policies.listing_scope``.

        ``query_text`` enables a deterministic ``unit_inventory`` fetch for
        price-style questions so answers do not depend on embedding similarity
        alone (WhatsApp uses a stricter confidence gate than ``match_threshold``).
        """
        if not org_id:
            # Fail closed: a missing tenant context must never widen the
            # search to all rows.  The route layer should never reach this
            # branch because TenantDep raises 401 first.
            raise ValueError("Retriever requires a non-empty org_id.")

        org_uuid = _normalize_uuid(org_id)
        property_uuid = _normalize_uuid(property_id) if property_id else None
        if org_uuid is None:
            raise ValueError("Retriever requires a valid org_id UUID.")

        units_rpc_args: Dict[str, Any] = {
            "query_embedding": query_embedding,
            "match_threshold": self.settings.rag_similarity_threshold,
            "match_count": self.settings.rag_top_k_units * 3,
            "match_org_id": org_uuid,
        }
        chunks_rpc_args: Dict[str, Any] = {
            "query_embedding": query_embedding,
            "match_threshold": self.settings.rag_similarity_threshold,
            "match_count": self.settings.rag_top_k_chunks * 3,
            "match_org_id": org_uuid,
        }
        if property_uuid:
            units_rpc_args["match_property_id"] = property_uuid
            chunks_rpc_args["match_property_id"] = property_uuid

        units_raw_list = self._safe_rpc("match_units", units_rpc_args)
        chunks_raw_list = self._safe_rpc("match_chunks", chunks_rpc_args)

        # Defense in depth: re-assert org_id and property_id scoping in
        # Python AFTER the SQL prune.  This protects against:
        #   * a stale RPC that hasn't yet picked up migration 005
        #   * a hypothetical RPC bug returning sibling-project rows
        scoped_units = [
            u
            for u in units_raw_list
            if _uuid_matches_left(u.get("org_id"), org_uuid)
            and (property_uuid is None or _uuid_matches_left(u.get("project_id"), property_uuid))
        ]
        scoped_chunks = [
            c
            for c in chunks_raw_list
            if _uuid_matches_left(c.get("org_id"), org_uuid)
            and (property_uuid is None or _uuid_matches_left(c.get("property_id"), property_uuid))
        ]

        threshold = float(self.settings.rag_similarity_threshold)
        vector_units = [
            u
            for u in scoped_units
            if isinstance(u.get("similarity"), (int, float)) and float(u["similarity"]) >= threshold
        ][: self.settings.rag_top_k_units]
        vector_chunks = [
            c
            for c in scoped_chunks
            if isinstance(c.get("similarity"), (int, float)) and float(c["similarity"]) >= threshold
        ][: self.settings.rag_top_k_chunks]

        deterministic_units: List[Dict[str, Any]] = []
        if property_uuid and query_text and should_prioritize_inventory_fallback(query_text):
            deterministic_units = self._list_units_scoped(org_uuid, property_uuid)

        units = self._merge_units_prefer_deterministic(
            deterministic_units, vector_units, self.settings.rag_top_k_units
        )

        scoped_brochure: List[Dict[str, Any]] = []
        if property_uuid:
            scoped_brochure = self._list_chunks_scoped(org_uuid, property_uuid)
        chunks = self._merge_chunks_vector_then_scoped(
            vector_chunks, scoped_brochure, self.settings.rag_top_k_chunks
        )

        # ----- Observability -------------------------------------------------
        self._log_retrieval(
            org_id=org_id,
            property_id=property_id,
            units=units,
            chunks=chunks,
            unit_floor=len(scoped_units),
            chunk_floor=len(scoped_chunks),
        )

        return {"units": units, "chunks": chunks}

    @staticmethod
    def _log_retrieval(
        *,
        org_id: str,
        property_id: str | None,
        units: List[Dict[str, Any]],
        chunks: List[Dict[str, Any]],
        unit_floor: int,
        chunk_floor: int,
    ) -> None:
        """Per-row + summary RAG audit log.

            Enterprise isolation layer - mandatory for SaaS scalability.
        Format: ``[RAG] org_id=... property_id=... source=... id=... similarity=...``
        ``property_id=org-wide`` indicates the broader-search consent path.
        """
        scope = property_id if property_id else "org-wide"
        logger.info(
            "[RAG] org_id=%s property_id=%s summary kept_units=%d kept_chunks=%d "
            "scanned_units=%d scanned_chunks=%d",
            org_id,
            scope,
            len(units),
            len(chunks),
            unit_floor,
            chunk_floor,
        )
        for u in units:
            label_bits = [
                u.get("unit_name"),
                u.get("configuration"),
                u.get("ai_summary"),
            ]
            label = next((str(x) for x in label_bits if x), "")
            logger.info(
                "[RAG] org_id=%s property_id=%s source=units id=%s similarity=%.4f label=%s",
                org_id,
                scope,
                u.get("id") or u.get("unit_id") or "n/a",
                float(u.get("similarity") or 0.0),
                str(u.get("label") or label)[:60],
            )
        for c in chunks:
            logger.info(
                "[RAG] org_id=%s property_id=%s source=chunks id=%s similarity=%.4f preview=%s",
                org_id,
                scope,
                c.get("id") or c.get("chunk_id") or "n/a",
                float(c.get("similarity") or 0.0),
                str(c.get("content") or "")[:80].replace("\n", " "),
            )
