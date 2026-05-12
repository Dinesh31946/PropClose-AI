"""WhatsApp Business Cloud API webhook.

    Enterprise isolation layer - mandatory for SaaS scalability.

The "Ear" of the PropClose pipeline.  Wired in at
``/api/v1/webhook/whatsapp/{org_slug}`` so each broker configures their
own Meta App with their own URL, which gives us **URL-level tenant
resolution** for an integration where Meta cannot send our usual
``X-Org-Id`` / ``Authorization`` header.

Contract (from the Blueprint):
    1. Meta -> us:  GET handshake (verify_token) and POST messages.
    2. We must verify ``X-Hub-Signature-256`` over the RAW request body
       to prove the message came from Meta.
    3. We must answer 200 OK in <1s; anything else and Meta retries
       every few seconds for ~24h.  RAG + Graph send happens inside a
       BackgroundTask.
    4. ``messages[].id`` is the idempotency key.
    5. Phone-Required (Option B) is honoured: messages from numbers we
       have no lead for are logged but never auto-create a lead — we
       respect the strict-matching contract.
    6. Cross-tenant isolation: ``org_id`` is resolved from the URL slug
       and forwarded into every DB / RAG / send call.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status
from fastapi.responses import PlainTextResponse, Response

from app.core.config import Settings
from app.db.repositories.chat_repository import ChatRepository
from app.db.repositories.whatsapp_repository import WhatsAppRepository
from app.db.supabase_client import get_supabase_client
from app.policies.conversation_intent import (
    append_intent_tag,
    format_display_phone,
    strip_trailing_intent_tag,
)
from app.policies.sales_closer_policy import pick_whatsapp_low_confidence_message
from app.schemas.chat import ChatRequest
from app.services.chat_service import ChatService
from app.services.whatsapp_service import (
    InboundMessage,
    WhatsAppClient,
    get_whatsapp_client,
    parse_inbound,
    verify_signature,
)

router = APIRouter()
logger = logging.getLogger(__name__)

# A single ChatService is fine because every public method takes
# ``org_id`` as an explicit argument; nothing tenant-scoped is cached on
# ``self``.  The webhook background worker reuses this instance.
_chat_service = ChatService()


# =========================================================================
# GET — Meta verification handshake
# =========================================================================


@router.get(
    "/webhook/whatsapp/{org_slug}",
    response_class=PlainTextResponse,
    summary="Meta webhook verification handshake",
)
def verify_webhook(
    org_slug: str,
    request: Request,
) -> PlainTextResponse:
    """Meta hits this once when the broker configures the webhook URL.

    We must echo back ``hub.challenge`` if and only if
    ``hub.verify_token`` matches the secret we configured in the Meta
    App dashboard.  Anything else -> 403.
    """
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge") or ""

    settings = Settings.load()
    expected_token = settings.whatsapp_verify_token
    if not expected_token:
        logger.error("WhatsApp verify_token not configured; rejecting handshake.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WhatsApp webhook not configured on this server.",
        )

    if mode != "subscribe" or token != expected_token:
        logger.warning(
            "WhatsApp handshake rejected for org_slug=%s mode=%s",
            org_slug,
            mode,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Verification failed.")

    # Side effect: confirm the slug is real so the broker sees a 404
    # instead of a silent success when they typo'd their slug.
    repo = WhatsAppRepository(get_supabase_client())
    if repo.get_org_by_slug(org_slug) is None:
        logger.warning("WhatsApp handshake: unknown org_slug=%s", org_slug)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown organisation slug.")

    logger.info("WhatsApp handshake accepted for org_slug=%s", org_slug)
    return PlainTextResponse(content=challenge, status_code=200)


# =========================================================================
# POST — inbound messages from Meta
# =========================================================================


@router.post(
    "/webhook/whatsapp/{org_slug}",
    summary="Receive a WhatsApp inbound message and reply via RAG",
)
async def receive_webhook(
    org_slug: str,
    request: Request,
    background: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
) -> Response:
    """Single entry point for every WhatsApp inbound event.

    Order of operations is deliberate:
      1. Read raw bytes (HMAC needs them BEFORE JSON parsing).
      2. Verify ``X-Hub-Signature-256``; reject with 403 on mismatch.
      3. Parse JSON; if malformed return 200 anyway so Meta stops
         retrying (we logged the issue) — bad payloads will not heal
         themselves, retries just waste budget.
      4. Resolve org from URL slug.  Unknown slug -> 404 (different from
         200 so the broker notices their misconfiguration in Meta).
      5. Schedule each well-formed message into a BackgroundTask and
         return 200 OK <1s.
    """
    raw_body = await request.body()

    settings = Settings.load()
    if not verify_signature(raw_body, x_hub_signature_256, settings.whatsapp_app_secret):
        logger.warning(
            "WhatsApp signature mismatch for org_slug=%s len=%d",
            org_slug,
            len(raw_body),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid X-Hub-Signature-256.",
        )

    try:
        payload: dict[str, Any] = json.loads(raw_body.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.exception("WhatsApp payload was not valid JSON: %s", exc)
        # Meta retries on non-2xx, and a malformed body will not fix
        # itself.  Return 200 to drain the retry queue.
        return Response(status_code=200)

    repo = WhatsAppRepository(get_supabase_client())
    org = repo.get_org_by_slug(org_slug)
    if not org:
        logger.warning("WhatsApp POST: unknown org_slug=%s", org_slug)
        # 404 (not 200) so the broker notices the misconfiguration.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown organisation slug.")
    org_id = str(org["id"])

    inbound: list[InboundMessage] = parse_inbound(payload)
    for message in inbound:
        # Each message gets its own background task so one slow RAG
        # call cannot block another customer's reply.
        background.add_task(
            _process_inbound_message,
            org_id=org_id,
            message=message,
            raw_payload=payload,
        )

    return Response(status_code=200)


# =========================================================================
# Background worker (the "Speaker")
# =========================================================================


def _process_inbound_message(
    *,
    org_id: str,
    message: InboundMessage,
    raw_payload: dict[str, Any],
) -> None:
    """RAG-powered inbound -> outbound pipeline.

        Enterprise isolation layer - mandatory for SaaS scalability.
    All DB writes carry ``org_id``; the lead-by-phone lookup is scoped
    so two brokers can hold the SAME phone number without collision.
    Anything that goes wrong here is swallowed + logged + flagged via
    ``mark_needs_attention`` — we never crash the worker because that
    would silently drop subsequent customers.

    Decision flow:
        claim_inbound        -> dedup
        find_lead_by_phone   -> tenant-scoped, prefix-agnostic (+ full lead row via ChatRepository for optional property_name)
        mark_read_with_typing -> human-feel typing indicator
        ChatService.handle_chat(..., min_similarity=WA_THRESHOLD,
                                persona_override=WA_PERSONA)
            -> resolves listing from property_id and/or lead.property_name
        if top_similarity < WA_THRESHOLD or no evidence:
            override reply with the "let me connect you" template
            mark_needs_attention(lead_id, org_id)
        send_text(...)
        log_outbound + mark_inbound_processed
    """
    supabase = get_supabase_client()
    repo = WhatsAppRepository(supabase)
    chat_repo = ChatRepository(supabase)
    client: WhatsAppClient = get_whatsapp_client()
    settings = Settings.load()

    # 1. Idempotency claim (atomic): if another retry already booked
    #    this message_id under this org, we silently bail.
    claimed = repo.claim_inbound(
        org_id=org_id,
        message_id=message.message_id,
        from_phone=message.from_phone,
        to_phone=message.to_phone_number_id,
        body=message.body,
        raw_payload=raw_payload,
    )
    if not claimed:
        return

    # 2. Resolve lead by phone within this tenant.
    lead = repo.find_lead_by_phone(org_id=org_id, phone=message.from_phone)
    if not lead:
        # Strict matching (Option B): we DO NOT auto-create a lead from
        # an unrecognised WhatsApp inbound, because that would let an
        # attacker / wrong-number text inject a row that bypasses the
        # broker's verified ingestion pipeline.
        logger.warning(
            "[WA] org_id=%s inbound=unknown_number phone=%s msg=%s "
            "(find_lead_by_phone returned no row — check leads.phone "
            "matches Meta 'from' digits for this org)",
            org_id,
            message.from_phone,
            message.message_id,
        )
        repo.mark_inbound_processed(
            org_id=org_id,
            message_id=message.message_id,
            lead_id=None,
            property_id=None,
            error="No lead found for phone in this org.",
        )
        # Don't bother replying; we have no lead context to ground RAG.
        return

    lead_id = str(lead["id"])
    full_lead = chat_repo.get_lead(lead_id, org_id) or dict(lead)
    property_ref = full_lead.get("property_id")
    audit_property_id = str(property_ref) if property_ref else None

    # 4. Show "...typing" to the customer immediately, BEFORE RAG runs.
    #    Failures here are logged but never block the actual reply.
    client.mark_read_with_typing(message.message_id, org_id=org_id)

    # 5. RAG with channel-specific safety knobs.  ``ChatService.handle_chat``
    #    saves user + assistant messages into ``chat_history`` with the
    #    right org_id, so we don't double-write.
    reply_text: str | None = None
    needs_attention = False
    rag_error: str | None = None
    chat_response = None
    try:
        chat_request = ChatRequest(
            lead_id=lead_id,
            property_id=audit_property_id or "",
            interested_in="",
            message=message.body,
        )
        chat_response = _chat_service.handle_chat(
            chat_request,
            org_id=org_id,
            min_similarity=settings.whatsapp_confidence_threshold,
            persona_override=settings.whatsapp_assistant_persona,
            whatsapp_channel=True,
        )
        reply_text = chat_response.reply
        needs_attention = bool(chat_response.needs_attention)
    except Exception as exc:
        logger.exception("[WA] org_id=%s rag_error lead=%s exc=%s", org_id, lead_id, exc)
        rag_error = f"{type(exc).__name__}: {exc}"
        chat_repo.mark_needs_attention(lead_id, org_id)

    # 6. Confidence-threshold guard: even if handle_chat returned a reply,
    #    the channel-specific gate kicks in when retrieval was weak (or
    #    absent).  Rather than risk a borderline answer on a 1+Cr deal,
    #    we hand off to a human and reply with the canned message.
    top_sim = chat_response.top_similarity if chat_response else None
    threshold = float(settings.whatsapp_confidence_threshold)
    low_confidence = chat_response is not None and (
        top_sim is None or top_sim < threshold
    )
    if low_confidence:
        logger.info(
            "[WA] org_id=%s lead=%s decision=low_confidence top_similarity=%s "
            "threshold=%.2f -> human_handoff",
            org_id,
            lead_id,
            f"{top_sim:.4f}" if top_sim is not None else "none",
            threshold,
        )
        wa_override = settings.whatsapp_low_confidence_reply.strip()
        if wa_override:
            low_conf_body = wa_override
        else:
            low_conf_body = pick_whatsapp_low_confidence_message(
                seed=f"{lead_id}:{message.body}",
                display_phone=format_display_phone(full_lead.get("phone")),
            )
        reply_text = append_intent_tag(
            low_conf_body,
            intent="EXPERT_BRIDGE",
            urgency="NORMAL",
        )
        chat_repo.mark_needs_attention(lead_id, org_id)
        needs_attention = True

    # 7. Send the reply (or a polite fallback if RAG fell over).
    if reply_text is None or not reply_text.strip():
        reply_text = append_intent_tag(
            (
                "Thanks for your message — I'm pulling the latest details and a member "
                "of our team will get back to you shortly."
            ).strip(),
            intent="GENERAL",
            urgency="NORMAL",
        )
        chat_repo.mark_needs_attention(lead_id, org_id)
        needs_attention = True

    customer_visible = strip_trailing_intent_tag(reply_text.strip())
    logger.info(
        "[WA] outbound org=%s lead=%s stripped_len=%s body=%s",
        org_id,
        lead_id,
        len(customer_visible),
        customer_visible[:2000] + ("..." if len(customer_visible) > 2000 else ""),
    )
    logger.info("[WA] tagged_reply_audit=%s", reply_text.strip()[:2000])
    send_result = client.send_text(message.from_phone, customer_visible)
    if not send_result.success:
        logger.error(
            "WhatsApp outbound failed org=%s lead=%s status=%s err=%s",
            org_id,
            lead_id,
            send_result.status_code,
            send_result.error,
        )
        chat_repo.mark_needs_attention(lead_id, org_id)
        needs_attention = True

    # 6. Audit-log the outbound + close out the inbound row.
    repo.log_outbound(
        org_id=org_id,
        wamid=send_result.wamid,
        to_phone=message.from_phone,
        from_phone_number_id=message.to_phone_number_id,
        body=reply_text,
        lead_id=lead_id,
        property_id=audit_property_id,
        success=send_result.success,
        error=send_result.error,
    )
    repo.mark_inbound_processed(
        org_id=org_id,
        message_id=message.message_id,
        lead_id=lead_id,
        property_id=audit_property_id,
        error=rag_error if rag_error else (
            send_result.error if not send_result.success else None
        ),
    )

    if needs_attention:
        # Already marked above on every failure path.  Logged here so
        # we can grep for "needs_attention" in production logs.
        logger.info(
            "WhatsApp loop completed with needs_attention org=%s lead=%s",
            org_id,
            lead_id,
        )


__all__ = ["router"]
