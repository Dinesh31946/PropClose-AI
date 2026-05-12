"""WhatsApp Business Cloud API integration.

    Enterprise isolation layer - mandatory for SaaS scalability.

This module is the "Translator" + "Speaker" from the Blueprint:

    * verify_signature()    — confirms a webhook POST really came from Meta
                              (HMAC SHA-256 over the raw body, comparing in
                              constant time to defeat timing attacks).
    * parse_inbound()       — turns Meta's deeply nested JSON into a flat
                              list of ``InboundMessage`` records ready for
                              RAG, dropping non-message events (statuses,
                              read receipts, system messages).
    * WhatsAppClient.send() — outbound text reply via Graph API with
                              bounded exponential-backoff retries and
                              per-message anti-spam jitter.

    Reply **language** (English vs Hinglish mirroring) is decided in
    ``app.rag.grounded_generator`` from the lead's messages—not here.

It is dependency-injected through ``get_whatsapp_service()`` so tests
can swap in a fake transport without monkeypatching ``httpx`` globally.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import random
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)


# =========================================================================
# Inbound payload parsing
# =========================================================================


@dataclass(frozen=True)
class InboundMessage:
    """One customer message extracted from a Meta webhook envelope.

    All fields are sanitised primitives so downstream code can operate
    without re-walking Meta's JSON.  ``raw`` is preserved verbatim for
    audit logging.
    """

    message_id: str            # Meta's wamid; used for idempotency
    from_phone: str            # E.164-ish, no leading +, as Meta sends
    to_phone_number_id: str    # OUR phone_number_id (the one Meta routed to)
    profile_name: str | None   # Customer's WhatsApp profile name, may be None
    body: str                  # Plain-text message body (text + button reply)
    message_type: str          # "text" | "button" | "interactive" | ...
    timestamp: int             # Unix seconds, Meta-provided
    raw: dict[str, Any]


def _normalise_phone(value: str | None) -> str:
    """Strip + / spaces / dashes / parens so we can compare against the
    same-shape phone we stored in ``leads.phone`` via the leads route.
    """
    if not value:
        return ""
    return re.sub(r"[\s\-\(\)\+]", "", str(value)).strip()


def parse_inbound(payload: dict[str, Any]) -> list[InboundMessage]:
    """Flatten a WhatsApp Cloud API webhook into ``InboundMessage`` rows.

    Meta envelope shape:

        {
          "object": "whatsapp_business_account",
          "entry": [{
            "id": "...",
            "changes": [{
              "value": {
                "messaging_product": "whatsapp",
                "metadata": {"phone_number_id": "...", "display_phone_number": "..."},
                "contacts": [{"wa_id": "...", "profile": {"name": "..."}}],
                "messages": [{
                  "from": "...",
                  "id": "wamid.xxx",
                  "timestamp": "1700000000",
                  "type": "text",
                  "text": {"body": "..."}
                }]
              },
              "field": "messages"
            }]
          }]
        }

    Status / delivery / read events arrive in the same shape but under
    ``value.statuses`` instead of ``value.messages``; we ignore those.
    """
    messages: list[InboundMessage] = []

    if not isinstance(payload, dict):
        return messages

    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value") or {}
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata") or {}
            phone_number_id = str(metadata.get("phone_number_id") or "")

            contact_index: dict[str, str] = {}
            for contact in value.get("contacts") or []:
                if not isinstance(contact, dict):
                    continue
                wa_id = str(contact.get("wa_id") or "")
                name = (contact.get("profile") or {}).get("name") if isinstance(
                    contact.get("profile"), dict
                ) else None
                if wa_id:
                    contact_index[wa_id] = str(name) if name else ""

            for raw_msg in value.get("messages") or []:
                if not isinstance(raw_msg, dict):
                    continue
                wa_message_id = str(raw_msg.get("id") or "")
                from_phone = _normalise_phone(str(raw_msg.get("from") or ""))
                if not wa_message_id or not from_phone:
                    continue

                msg_type = str(raw_msg.get("type") or "text")
                body = _extract_body(raw_msg, msg_type)
                if not body:
                    # We can only RAG over text-shaped content.  Media /
                    # location / contacts / unsupported types are logged
                    # for the broker but skipped from the AI loop.
                    body = f"[unsupported:{msg_type}]"

                try:
                    ts = int(raw_msg.get("timestamp") or 0)
                except (TypeError, ValueError):
                    ts = 0

                messages.append(
                    InboundMessage(
                        message_id=wa_message_id,
                        from_phone=from_phone,
                        to_phone_number_id=phone_number_id,
                        profile_name=contact_index.get(str(raw_msg.get("from") or "")) or None,
                        body=body,
                        message_type=msg_type,
                        timestamp=ts,
                        raw=raw_msg,
                    )
                )
    return messages


def _extract_body(raw_msg: dict[str, Any], msg_type: str) -> str:
    """Pull a usable text body out of the diverse Meta message types."""
    if msg_type == "text":
        return str(((raw_msg.get("text") or {}).get("body") or "")).strip()
    if msg_type == "button":
        return str(((raw_msg.get("button") or {}).get("text") or "")).strip()
    if msg_type == "interactive":
        interactive = raw_msg.get("interactive") or {}
        if isinstance(interactive, dict):
            br = interactive.get("button_reply") or {}
            lr = interactive.get("list_reply") or {}
            if isinstance(br, dict) and br.get("title"):
                return str(br.get("title")).strip()
            if isinstance(lr, dict) and lr.get("title"):
                return str(lr.get("title")).strip()
    return ""


# =========================================================================
# Signature verification
# =========================================================================


def verify_signature(raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    """Verify Meta's ``X-Hub-Signature-256`` header.

        Enterprise isolation layer - mandatory for SaaS scalability.
    Meta signs the raw request body with HMAC-SHA256 keyed by the
    Meta App's app_secret.  We MUST compute the digest over the EXACT
    bytes Meta sent us — any framework auto-decoding (json parsing,
    pydantic) will break the signature.
    """
    if not app_secret:
        # Mis-configured server: refuse rather than accept-by-default.
        logger.error("WhatsApp app_secret is not configured; rejecting webhook.")
        return False
    if not signature_header:
        return False

    expected = "sha256=" + hmac.new(
        app_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    # ``compare_digest`` is constant-time; equality on two ASCII strings
    # of identical length is the only way to defeat timing oracles.
    try:
        return hmac.compare_digest(expected, signature_header)
    except (TypeError, ValueError):
        return False


# =========================================================================
# Outbound client
# =========================================================================


@dataclass(frozen=True)
class SendResult:
    success: bool
    wamid: str | None
    status_code: int | None
    error: str | None


class WhatsAppClient:
    """Thin Graph API client with retry + jitter.

    For the 1-broker beta the access_token + phone_number_id are global
    settings.  For multi-tenant production we will swap ``settings`` for
    per-org credentials looked up from a ``org_whatsapp_credentials``
    table, keeping the public ``send()`` API identical.
    """

    GRAPH_HOST = "https://graph.facebook.com"

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self._settings = settings
        # ``transport`` lets tests inject httpx.MockTransport without
        # spinning a real HTTP server.
        self._transport = transport
        self._sleep = sleep

    @property
    def configured(self) -> bool:
        return bool(
            self._settings.whatsapp_access_token
            and self._settings.whatsapp_phone_number_id
        )

    def mark_read_with_typing(
        self,
        inbound_message_id: str,
        *,
        org_id: str | None = None,
    ) -> SendResult:
        """Show "...typing" to the customer while RAG runs.

            Enterprise isolation layer - mandatory for SaaS scalability.
        Meta combines marking-as-read and the typing indicator in one
        request: ``status: read`` + ``typing_indicator: {type: text}``.
        The indicator is shown for up to ~25 seconds or until our next
        outbound message lands -- whichever is sooner.  Failures here
        are non-fatal: we still attempt the actual reply afterwards.
        """
        if not inbound_message_id:
            return SendResult(False, None, None, "missing inbound_message_id")

        if (
            not self._settings.whatsapp_typing_indicator_enabled
            or self._settings.whatsapp_dry_run
            or not self.configured
        ):
            logger.info(
                "[WA] org_id=%s typing_indicator=skipped configured=%s dry_run=%s enabled=%s",
                org_id,
                self.configured,
                self._settings.whatsapp_dry_run,
                self._settings.whatsapp_typing_indicator_enabled,
            )
            return SendResult(True, None, 200, None)

        url = (
            f"{self.GRAPH_HOST}/{self._settings.whatsapp_graph_version}"
            f"/{self._settings.whatsapp_phone_number_id}/messages"
        )
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": inbound_message_id,
            "typing_indicator": {"type": "text"},
        }
        headers = {
            "Authorization": f"Bearer {self._settings.whatsapp_access_token}",
            "Content-Type": "application/json",
        }
        try:
            with self._open_client() as client:
                response = client.post(url, json=payload, headers=headers, timeout=5.0)
            if 200 <= response.status_code < 300:
                logger.info(
                    "[WA] org_id=%s typing_indicator=sent inbound=%s status=%d",
                    org_id,
                    inbound_message_id,
                    response.status_code,
                )
                return SendResult(True, None, response.status_code, None)
            if response.status_code == 401:
                logger.error(
                    "[WA] org_id=%s typing_indicator=rejected_http_401 — WhatsApp Graph token "
                    "expired or invalid; refresh WHATSAPP_ACCESS_TOKEN. inbound=%s",
                    org_id,
                    inbound_message_id,
                )
            else:
                logger.warning(
                    "[WA] org_id=%s typing_indicator=failed inbound=%s status=%d body=%s",
                    org_id,
                    inbound_message_id,
                    response.status_code,
                    response.text[:200],
                )
            return SendResult(False, None, response.status_code, response.text[:200])
        except httpx.HTTPError as exc:
            logger.warning(
                "[WA] org_id=%s typing_indicator=error inbound=%s exc=%s",
                org_id,
                inbound_message_id,
                exc,
            )
            return SendResult(False, None, None, f"{type(exc).__name__}: {exc}")

    def send_text(self, to_phone: str, body: str) -> SendResult:
        """Send a free-form text message inside the 24h customer window.

        Returns a structured result instead of raising so callers can
        decide whether to ``mark_needs_attention``.  We log everything
        but never propagate raw httpx exceptions outside this class.
        """
        if not body or not body.strip():
            return SendResult(False, None, None, "empty body refused")
        if not to_phone:
            return SendResult(False, None, None, "missing recipient phone")

        # Anti-spam jitter: human-typing delay.  Skipped when both
        # bounds are 0 (tests + dry-run).
        lo = max(0.0, float(self._settings.whatsapp_typing_jitter_min))
        hi = max(lo, float(self._settings.whatsapp_typing_jitter_max))
        if hi > 0:
            self._sleep(random.uniform(lo, hi))

        if self._settings.whatsapp_dry_run or not self.configured:
            logger.info(
                "WhatsApp DRY-RUN: would send to=%s len=%d configured=%s",
                to_phone,
                len(body),
                self.configured,
            )
            return SendResult(True, None, 200, None)

        url = (
            f"{self.GRAPH_HOST}/{self._settings.whatsapp_graph_version}"
            f"/{self._settings.whatsapp_phone_number_id}/messages"
        )
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_phone,
            "type": "text",
            "text": {"preview_url": False, "body": body[:4096]},
        }
        headers = {
            "Authorization": f"Bearer {self._settings.whatsapp_access_token}",
            "Content-Type": "application/json",
        }

        # Bounded exponential backoff: 0.5s, 1s, 2s.  Total worst-case
        # ~3.5s, well inside the BackgroundTask budget (Meta's webhook
        # ack already left the building).
        last_err: str | None = None
        last_status: int | None = None
        for attempt in range(3):
            try:
                with self._open_client() as client:
                    response = client.post(url, json=payload, headers=headers, timeout=10.0)
                last_status = response.status_code
                if 200 <= response.status_code < 300:
                    data = self._safe_json(response)
                    wamid: str | None = None
                    msgs = data.get("messages") if isinstance(data, dict) else None
                    if isinstance(msgs, list) and msgs:
                        wamid = str(msgs[0].get("id") or "") or None
                    return SendResult(True, wamid, response.status_code, None)
                # 429 / 5xx -> retry; 401 / other 4xx -> bail out (broker config bug).
                if response.status_code == 401:
                    logger.error(
                        "[WA] send_text=rejected_http_401 — WhatsApp Graph token expired or "
                        "invalid; refresh WHATSAPP_ACCESS_TOKEN. phone=%s",
                        to_phone,
                    )
                if response.status_code in {408, 429} or response.status_code >= 500:
                    last_err = f"http {response.status_code}: {response.text[:200]}"
                    self._sleep(0.5 * (2 ** attempt))
                    continue
                return SendResult(False, None, response.status_code, response.text[:500])
            except httpx.HTTPError as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                self._sleep(0.5 * (2 ** attempt))

        return SendResult(False, None, last_status, last_err or "unknown error")

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _open_client(self) -> httpx.Client:
        if self._transport is not None:
            return httpx.Client(transport=self._transport)
        return httpx.Client()

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict[str, Any]:
        try:
            return response.json()
        except Exception:
            return {}


# =========================================================================
# Module-level accessors (cached so the route doesn't re-instantiate per
# request; tests reset via ``reset_whatsapp_caches()``).
# =========================================================================


@lru_cache(maxsize=1)
def get_whatsapp_settings() -> Settings:
    return Settings.load()


@lru_cache(maxsize=1)
def get_whatsapp_client() -> WhatsAppClient:
    return WhatsAppClient(get_whatsapp_settings())


def reset_whatsapp_caches() -> None:
    """Clear cached settings + client.  Used by tests after monkeypatch."""
    get_whatsapp_settings.cache_clear()
    get_whatsapp_client.cache_clear()


__all__ = [
    "InboundMessage",
    "SendResult",
    "WhatsAppClient",
    "get_whatsapp_client",
    "get_whatsapp_settings",
    "parse_inbound",
    "reset_whatsapp_caches",
    "verify_signature",
]
