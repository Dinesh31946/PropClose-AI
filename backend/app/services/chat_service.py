import json
import logging
from typing import Any, Dict, List

from app.core.config import Settings
from app.db.repositories.chat_repository import ChatRepository
from app.db.supabase_client import get_supabase_client
from app.policies.listing_scope import (
    is_affirmative_consent,
    is_broader_search_inquiry,
    redirect_template,
    strip_redirect_marker,
    was_last_turn_a_redirect,
)
from app.policies.conversation_intent import (
    append_intent_tag,
    emergency_callback_requested,
    format_display_phone,
    human_call_back_requested,
    sanitize_history_for_llm,
)
from app.policies.sales_closer_policy import (
    enforce_sales_closer_policy,
    fallback_no_evidence_response,
    handoff_response_for_exact_pricing,
)
from app.rag.context_builder import build_context
from app.rag.embedder import Embedder
from app.rag.grounded_generator import GroundedGenerator
from app.rag.retriever import Retriever
from app.rag.validators import (
    has_confident_evidence,
    has_enough_evidence,
    is_high_risk_price_query,
    requires_handoff_for_price_accuracy,
    should_prioritize_inventory_fallback,
)
from app.schemas.chat import ChatRequest, ChatResponse, EvidenceItem
from app.services.profiling_service import ProfilingService

logger = logging.getLogger(__name__)


def _normalize_profiling_data(raw: Any) -> Dict[str, Any]:
    """Coerce DB ``profiling_data`` (jsonb or legacy string) to a mutable dict."""

    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


class ChatService:
    """Tenant-aware chat orchestrator.

        Enterprise isolation layer - mandatory for SaaS scalability.
    The service is a singleton (created once at module load), but every
    public method takes ``org_id`` as an explicit argument so the same
    instance can serve thousands of concurrent tenants without state
    leakage.  No org-scoped attribute is ever cached on ``self``.
    """

    def __init__(self) -> None:
        self.settings = Settings.load()
        self.supabase = get_supabase_client()
        self.repo = ChatRepository(self.supabase)
        self.embedder = Embedder(self.settings)
        self.retriever = Retriever(self.supabase, self.settings)
        self.generator = GroundedGenerator(self.settings)
        self.profiling = ProfilingService(self.settings)

    def handle_chat(
        self,
        payload: ChatRequest,
        org_id: str,
        *,
        min_similarity: float | None = None,
        persona_override: str | None = None,
        whatsapp_channel: bool = False,
    ) -> ChatResponse:
        """Run the RAG pipeline for one user message.

            Enterprise isolation layer - mandatory for SaaS scalability.

        ``min_similarity`` and ``persona_override`` are channel-level
        knobs.  The dashboard chat passes neither and gets the default
        threshold (``settings.rag_similarity_threshold``, ~0.35) and
        the sales-closer persona.  WhatsApp passes a stricter threshold
        (``settings.whatsapp_confidence_threshold``, ~0.7), the
        polite Real-Estate-Consultant persona, and ``whatsapp_channel=True``
        so prompts never ask for a phone number already implied by the channel.
        """
        if not org_id:
            # Should be unreachable: TenantDep raises 401 before we get here.
            raise ValueError("ChatService.handle_chat requires a non-empty org_id.")

        effective_threshold = (
            float(min_similarity)
            if min_similarity is not None
            else float(self.settings.rag_similarity_threshold)
        )

        lead = self.repo.get_lead(payload.lead_id, org_id) or {}
        history = self.repo.get_recent_history(payload.lead_id, org_id)

        existing_profile = _normalize_profiling_data(lead.get("profiling_data"))
        try:
            extracted = self.profiling.extract_signals(
                message=payload.message,
                history=history,
            )
        except Exception:
            logger.exception(
                "[profiling] extract_signals failed lead_id=%s org_id=%s",
                payload.lead_id,
                org_id,
            )
            extracted = {}
        merged_profile = self.profiling.merge_into_profile(existing_profile, extracted)
        if merged_profile != existing_profile:
            try:
                self.repo.update_lead_profiling_data(
                    payload.lead_id, org_id, merged_profile
                )
            except Exception:
                logger.exception(
                    "[profiling] update_lead_profiling_data failed lead_id=%s org_id=%s",
                    payload.lead_id,
                    org_id,
                )
        lead = {**lead, "profiling_data": merged_profile}

        payload_pid = (payload.property_id or "").strip()
        lead_pid_raw = lead.get("property_id")
        lead_pid = str(lead_pid_raw).strip() if lead_pid_raw else ""
        candidate_pid = payload_pid or lead_pid

        property_data: Dict[str, Any] = {}
        if candidate_pid:
            property_data = self.repo.get_property(candidate_pid, org_id) or {}

        if not property_data and lead.get("property_name"):
            resolved = self.repo.lookup_property_id_by_name(
                org_id,
                str(lead.get("property_name") or "").strip() or None,
            )
            if resolved:
                candidate_pid = resolved
                property_data = self.repo.get_property(resolved, org_id) or {}
                logger.info(
                    "[RAG] org_id=%s lead_id=%s resolved property_id=%s from lead.property_name=%r",
                    org_id,
                    payload.lead_id,
                    resolved,
                    lead.get("property_name"),
                )

        scoped_property_id: str | None = candidate_pid if property_data else None
        urgent_escalation = emergency_callback_requested(payload.message)
        human_callback = human_call_back_requested(payload.message)
        display_phone = (
            format_display_phone(lead.get("phone")) if whatsapp_channel else None
        )
        expert_bridge_seed = f"{payload.lead_id}:{payload.message}"
        if urgent_escalation:
            self.repo.mark_needs_attention(payload.lead_id, org_id)

        if requires_handoff_for_price_accuracy(payload.message):
            body = enforce_sales_closer_policy(handoff_response_for_exact_pricing())
            hi, urg = (
                ("CALL_BACK", "HIGH") if urgent_escalation else ("HANDOFF_PRICE", "NORMAL")
            )
            reply = append_intent_tag(body, intent=hi, urgency=urg)
            self.repo.mark_needs_attention(payload.lead_id, org_id)
            self.repo.save_messages(payload.lead_id, org_id, payload.message, reply)
            logger.info(
                "[RAG] org_id=%s lead_id=%s decision=handoff_exact_pricing",
                org_id,
                payload.lead_id,
            )
            return ChatResponse(
                success=True, reply=reply, needs_attention=True, top_similarity=None
            )

        # ----- Specific-Listing-Lock gate (Anti-Distraction Rule) -----------
        #     Enterprise isolation layer - mandatory for SaaS scalability.
        # We move through three states based on chat_history alone -- no
        # extra DB column, so the gate is correct even after a worker
        # restart mid-conversation:
        #
        #   LOCKED            -> retrieve only lead.property_id
        #   AWAITING_CONSENT  -> return redirect template, no LLM call
        #   UNLOCKED          -> retrieve org-wide (still tenant-scoped)
        property_name = (
            (property_data.get("name") if property_data else None)
            or (str(lead.get("property_name") or "").strip())
            or "this project"
        )
        retrieve_property_id: str | None = scoped_property_id

        prior_redirect = was_last_turn_a_redirect(history)
        broader_intent = is_broader_search_inquiry(payload.message)
        consent_unlock = prior_redirect and is_affirmative_consent(payload.message)

        if consent_unlock:
            # UNLOCKED: lead opted in to a broader search.  Drop the
            # property_id filter for THIS turn only; subsequent turns
            # will revert to LOCKED unless the lead opts in again.
            retrieve_property_id = None
            logger.info(
                "[RAG] org_id=%s lead_id=%s listing_lock=unlocked (consent received)",
                org_id,
                payload.lead_id,
            )
        elif broader_intent and not prior_redirect:
            # AWAITING_CONSENT: the user asked about other listings
            # without prior consent.  We send the canned redirect
            # prompt WITHOUT running RAG.  Two copies of the reply:
            #   * stored_reply  -- includes REDIRECT_MARKER so the
            #     NEXT turn's was_last_turn_a_redirect() can detect
            #     the awaiting-consent state from chat_history alone
            #     (no extra DB column, restart-safe).
            #   * visible_reply -- marker stripped, sent to the customer.
            redirect = redirect_template(property_name)
            stored_reply = enforce_sales_closer_policy(redirect)
            visible_reply = strip_redirect_marker(stored_reply)
            hi, urg = (
                ("CALL_BACK", "HIGH") if urgent_escalation else ("GENERAL", "NORMAL")
            )
            stored_tagged = append_intent_tag(stored_reply.strip(), intent=hi, urgency=urg)
            visible_tagged = append_intent_tag(visible_reply.strip(), intent=hi, urgency=urg)
            self.repo.save_messages(
                payload.lead_id, org_id, payload.message, stored_tagged
            )
            logger.info(
                "[RAG] org_id=%s lead_id=%s listing_lock=awaiting_consent property=%s",
                org_id,
                payload.lead_id,
                retrieve_property_id,
            )
            return ChatResponse(
                success=True,
                reply=visible_tagged,
                needs_attention=False,
                top_similarity=None,
                evidence=[],
            )

        # Without a scoped listing we cannot run locked RAG safely (retrieve(None) is org-wide).
        # Exception: broader-search redirect/consent flows handled above — `consent_unlock` allows widened retrieval.
        if retrieve_property_id is None and not consent_unlock:
            logger.warning(
                "[RAG] org_id=%s lead_id=%s no_property_scope (set property_id or lead.property_name) — bridge",
                org_id,
                payload.lead_id,
            )
            body = enforce_sales_closer_policy(
                fallback_no_evidence_response(expert_bridge_seed)
            )
            hi, urg = (
                ("CALL_BACK", "HIGH") if urgent_escalation else ("EXPERT_BRIDGE", "NORMAL")
            )
            reply = append_intent_tag(body, intent=hi, urgency=urg)
            self.repo.mark_needs_attention(payload.lead_id, org_id)
            self.repo.save_messages(payload.lead_id, org_id, payload.message, reply)
            return ChatResponse(
                success=True,
                reply=reply,
                needs_attention=True,
                top_similarity=None,
                evidence=[],
            )

        query_embedding = self.embedder.embed_query(payload.message)
        results = self.retriever.retrieve(
            query_embedding,
            retrieve_property_id,
            org_id=org_id,
            query_text=payload.message,
        )
        units = results["units"]
        chunks = results["chunks"]

        matched_unit_lock = False
        matched_uid_raw = lead.get("matched_unit_id")
        if matched_uid_raw and retrieve_property_id:
            pinned = self.repo.get_inventory_unit(str(matched_uid_raw), org_id)
            if pinned and str(pinned.get("project_id")) == str(retrieve_property_id):
                pinned_row = dict(pinned)
                pinned_row["similarity"] = 1.0
                units = [pinned_row]
                matched_unit_lock = True
                logger.info(
                    "[RAG] org_id=%s lead_id=%s matched_unit_pin id=%s config=%s",
                    org_id,
                    payload.lead_id,
                    pinned_row.get("id"),
                    pinned_row.get("configuration"),
                )

        evidence_items, top_similarity = _build_evidence_summary(units, chunks, org_id)

        # Tenant-scoped audit log of the gate decision.  Cannot leak
        # cross-org because it only references the current ``org_id``.
        logger.info(
            "[RAG] org_id=%s lead_id=%s property_id=%s top_similarity=%s "
            "threshold=%.4f n_units=%d n_chunks=%d",
            org_id,
            payload.lead_id,
            retrieve_property_id,
            f"{top_similarity:.4f}" if top_similarity is not None else "none",
            effective_threshold,
            len(units),
            len(chunks),
        )

        if not has_enough_evidence(units, chunks) or not has_confident_evidence(
            units, chunks, effective_threshold
        ):
            body = enforce_sales_closer_policy(
                fallback_no_evidence_response(expert_bridge_seed)
            )
            hi, urg = (
                ("CALL_BACK", "HIGH") if urgent_escalation else ("EXPERT_BRIDGE", "NORMAL")
            )
            reply = append_intent_tag(body, intent=hi, urgency=urg)
            if is_high_risk_price_query(payload.message):
                self.repo.mark_needs_attention(payload.lead_id, org_id)
                needs_attention = True
            else:
                needs_attention = False
            self.repo.save_messages(payload.lead_id, org_id, payload.message, reply)
            return ChatResponse(
                success=True,
                reply=reply,
                needs_attention=needs_attention,
                top_similarity=top_similarity,
                evidence=evidence_items,
            )

        inventory_first = (
            matched_unit_lock
            or (
                retrieve_property_id is not None
                and should_prioritize_inventory_fallback(payload.message)
            )
        )
        context = build_context(
            units,
            chunks,
            prioritize_inventory=inventory_first,
            matched_unit_fact_lock=matched_unit_lock,
        )
        history_for_llm = sanitize_history_for_llm(history)
        profile_gap_key: str | None = self.profiling.select_next_missing_key(merged_profile)
        if urgent_escalation or human_callback:
            profile_gap_key = None

        generated_reply = self.generator.generate(
            property_name=property_name,
            lead_name=lead.get("name", "Customer"),
            interested_in=payload.interested_in or "",
            user_message=payload.message,
            chat_history=history_for_llm,
            context=context,
            persona_override=persona_override,
            listing_locked=(retrieve_property_id is not None),
            prioritize_inventory_evidence=inventory_first,
            matched_unit_fact_lock=matched_unit_lock,
            emergency_callback=urgent_escalation,
            whatsapp_channel=whatsapp_channel,
            display_phone_e164=display_phone,
            human_callback_signal=human_callback,
            profile_gap_key=profile_gap_key,
        )
        final_reply = enforce_sales_closer_policy(generated_reply)

        site_visit_confirmed = "[SITE_VISIT_CONFIRMED]" in final_reply
        body = final_reply.replace("[SITE_VISIT_CONFIRMED]", "").strip()
        if site_visit_confirmed:
            self.repo.mark_site_visit_scheduled(payload.lead_id, org_id)

        hi, urg = ("CALL_BACK", "HIGH") if urgent_escalation else ("GENERAL", "NORMAL")
        clean_reply = append_intent_tag(body, intent=hi, urgency=urg)

        self.repo.save_messages(payload.lead_id, org_id, payload.message, clean_reply)
        return ChatResponse(
            success=True,
            reply=clean_reply,
            site_visit_confirmed=site_visit_confirmed,
            needs_attention=urgent_escalation,
            top_similarity=top_similarity,
            evidence=evidence_items,
        )


def _build_evidence_summary(
    units: List[Dict[str, Any]],
    chunks: List[Dict[str, Any]],
    org_id: str,
) -> tuple[list[EvidenceItem], float | None]:
    """Flatten retrieval results into the structured ``evidence`` field
    AND compute the channel-gating top similarity in one pass.

        Enterprise isolation layer - mandatory for SaaS scalability.
    Every ``EvidenceItem.org_id`` is hard-pinned to the request's
    ``org_id`` -- never derived from the row -- so a defensive copy of
    a misconfigured RPC result cannot leak another tenant's id into
    a downstream observer.
    """
    evidence: list[EvidenceItem] = []
    top: float | None = None
    for u in units:
        score = u.get("similarity")
        score_val = float(score) if isinstance(score, (int, float)) else None
        if score_val is not None and (top is None or score_val > top):
            top = score_val
        evidence.append(
            EvidenceItem(
                source="units",
                org_id=org_id,
                property_id=str(u.get("project_id") or u.get("property_id") or "") or None,
                chunk_id=str(u.get("id") or u.get("unit_id") or "") or None,
                similarity=score_val,
            )
        )
    for c in chunks:
        score = c.get("similarity")
        score_val = float(score) if isinstance(score, (int, float)) else None
        if score_val is not None and (top is None or score_val > top):
            top = score_val
        evidence.append(
            EvidenceItem(
                source="chunks",
                org_id=org_id,
                property_id=str(c.get("property_id") or "") or None,
                chunk_id=str(c.get("id") or c.get("chunk_id") or "") or None,
                similarity=score_val,
            )
        )
    return evidence, top
