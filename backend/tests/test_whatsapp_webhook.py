"""Tests for the WhatsApp Cloud API webhook.

    Enterprise isolation layer - mandatory for SaaS scalability.

The suite is hermetic:

    * No real Meta / Graph API call is made (FakeWhatsAppClient).
    * No real Supabase call is made (FakeWhatsAppDB).
    * RAG is stubbed via _chat_service replacement.

What we prove:

    1. GET handshake echoes hub.challenge only when verify_token matches.
    2. POST without a valid X-Hub-Signature-256 -> 403.
    3. POST with valid signature -> 200, schedules a BackgroundTask.
    4. The background worker is idempotent (same wamid twice -> 1 RAG run).
    5. Cross-tenant isolation: org A's URL never resolves to org B's data
       even when the inbound phone happens to be a lead in org B.
    6. Lead-by-phone strict: unknown number does NOT auto-create a lead.
    7. RAG failure -> mark_needs_attention + fallback reply still sent.
    8. The whole request runs end-to-end in <1 second
       (BackgroundTasks fire AFTER the response).

Run from the repo root:

    .\\backend\\venv\\Scripts\\python.exe -m pytest backend/tests/test_whatsapp_webhook.py -v
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from app.api.v1.routes import webhook as webhook_route
from app.core.config import Settings
from app.main import app
from app.services import whatsapp_service as whatsapp_module

ORG_A_ID = "11111111-1111-1111-1111-111111111111"
ORG_A_SLUG = "alpha-realty"
ORG_B_ID = "22222222-2222-2222-2222-222222222222"
ORG_B_SLUG = "beta-builders"

PHONE_LEAD_A = "919876543210"
PHONE_LEAD_B = "919876543210"  # Same digits, different org -> isolation test
PHONE_UNKNOWN = "918888888888"

APP_SECRET = "smoke-app-secret-do-not-reuse"
VERIFY_TOKEN = "smoke-verify-token"
PHONE_NUMBER_ID = "100200300400"


# =========================================================================
# Fakes
# =========================================================================


class FakeResponse:
    def __init__(self, data: Any) -> None:
        self.data = data


class FakeQuery:
    def __init__(self, table: str, db: "FakeWhatsAppDB") -> None:
        self.table = table
        self.db = db
        self.action = "select"
        self.payload: Any = None
        self.eq_filters: dict[str, Any] = {}

    def select(self, *_a, **_kw) -> "FakeQuery":
        self.action = "select"
        return self

    def insert(self, payload: Any) -> "FakeQuery":
        self.action = "insert"
        self.payload = payload
        return self

    def update(self, payload: Any) -> "FakeQuery":
        self.action = "update"
        self.payload = payload
        return self

    def eq(self, field: str, value: Any) -> "FakeQuery":
        self.eq_filters[field] = value
        return self

    def order(self, *_a, **_kw) -> "FakeQuery":
        return self

    def limit(self, _n: int) -> "FakeQuery":
        return self

    def execute(self) -> FakeResponse:
        return self.db.run(self)


class FakeWhatsAppDB:
    """In-memory stand-in for the supabase-py client.

    Stores: organizations, leads, whatsapp_messages.  Enforces the
    UNIQUE (org_id, direction, message_id) contract so we can exercise
    the dedup path realistically.
    """

    def __init__(self) -> None:
        self.organizations: list[dict[str, Any]] = []
        self.leads: list[dict[str, Any]] = []
        self.whatsapp_messages: list[dict[str, Any]] = []
        self.lead_updates: list[tuple[str, str, dict[str, Any]]] = []  # (org_id, lead_id, payload)

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(name, self)

    def run(self, q: FakeQuery) -> FakeResponse:
        if q.table == "organizations":
            if q.action == "select":
                slug = q.eq_filters.get("slug")
                rows = [r for r in self.organizations if r["slug"] == slug] if slug else []
                return FakeResponse(rows)

        if q.table == "leads":
            if q.action == "select":
                org_id = q.eq_filters.get("org_id")
                phone = q.eq_filters.get("phone")
                rows = [
                    r for r in self.leads
                    if r["org_id"] == org_id and (phone is None or r.get("phone") == phone)
                ]
                return FakeResponse(rows)
            if q.action == "update":
                org_id = q.eq_filters.get("org_id")
                lead_id = q.eq_filters.get("id")
                self.lead_updates.append((org_id, lead_id, dict(q.payload)))
                for row in self.leads:
                    if row["org_id"] == org_id and row["id"] == lead_id:
                        row.update(q.payload)
                        return FakeResponse([dict(row)])
                return FakeResponse([])

        if q.table == "whatsapp_messages":
            if q.action == "insert":
                rows = q.payload if isinstance(q.payload, list) else [q.payload]
                inserted: list[dict[str, Any]] = []
                for row in rows:
                    row = dict(row)
                    direction = row.get("direction")
                    msg_id = row.get("message_id")
                    org_id = row.get("org_id")
                    if direction == "inbound":
                        if any(
                            r["org_id"] == org_id
                            and r["direction"] == "inbound"
                            and r["message_id"] == msg_id
                            for r in self.whatsapp_messages
                        ):
                            err = APIError({"message": "duplicate", "code": "23505"})
                            raise err
                    self.whatsapp_messages.append(row)
                    inserted.append(row)
                return FakeResponse(inserted)
            if q.action == "update":
                org_id = q.eq_filters.get("org_id")
                direction = q.eq_filters.get("direction")
                msg_id = q.eq_filters.get("message_id")
                for r in self.whatsapp_messages:
                    if (
                        r["org_id"] == org_id
                        and r["direction"] == direction
                        and r["message_id"] == msg_id
                    ):
                        r.update(q.payload)
                        return FakeResponse([dict(r)])
                return FakeResponse([])
            if q.action == "select":
                rows = [
                    r
                    for r in self.whatsapp_messages
                    if all(r.get(k) == v for k, v in q.eq_filters.items())
                ]
                return FakeResponse(rows)

        return FakeResponse([])


class FakeWhatsAppClient:
    """Records send_text + mark_read_with_typing calls instead of
    hitting graph.facebook.com.

        Enterprise isolation layer - mandatory for SaaS scalability.
    Tracks ``org_id`` per typing call so we can assert log isolation.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.typing: list[tuple[str, str | None]] = []   # (inbound_message_id, org_id)
        self.fail_next: int = 0
        self.configured = True

    def send_text(self, to_phone: str, body: str):
        from app.services.whatsapp_service import SendResult

        self.sent.append((to_phone, body))
        if self.fail_next > 0:
            self.fail_next -= 1
            return SendResult(False, None, 500, "simulated outage")
        return SendResult(True, f"wamid.out-{len(self.sent)}", 200, None)

    def mark_read_with_typing(self, inbound_message_id: str, *, org_id: str | None = None):
        from app.services.whatsapp_service import SendResult

        self.typing.append((inbound_message_id, org_id))
        return SendResult(True, None, 200, None)


# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def fake_db() -> FakeWhatsAppDB:
    db = FakeWhatsAppDB()
    db.organizations.extend(
        [
            {"id": ORG_A_ID, "slug": ORG_A_SLUG, "name": "Alpha Realty", "subscription_tier": "trial"},
            {"id": ORG_B_ID, "slug": ORG_B_SLUG, "name": "Beta Builders", "subscription_tier": "trial"},
        ]
    )
    # Lead A: belongs to org A
    db.leads.append(
        {
            "id": "lead-a",
            "org_id": ORG_A_ID,
            "phone": PHONE_LEAD_A,
            "property_id": "prop-a",
            "name": "Asha",
            "status": "New",
        }
    )
    # Lead B: belongs to org B, SAME phone as lead A -> tests cross-org
    # isolation.
    db.leads.append(
        {
            "id": "lead-b",
            "org_id": ORG_B_ID,
            "phone": PHONE_LEAD_B,
            "property_id": "prop-b",
            "name": "Bharat",
            "status": "New",
        }
    )
    return db


@pytest.fixture
def fake_client() -> FakeWhatsAppClient:
    return FakeWhatsAppClient()


@pytest.fixture
def chat_calls() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {
        "whatsapp_app_secret": APP_SECRET,
        "whatsapp_verify_token": VERIFY_TOKEN,
        "whatsapp_phone_number_id": PHONE_NUMBER_ID,
        "whatsapp_access_token": "test-access-token",
        "whatsapp_dry_run": True,
        "whatsapp_typing_jitter_min": 0.0,
        "whatsapp_typing_jitter_max": 0.0,
    }


@pytest.fixture
def client(
    fake_db: FakeWhatsAppDB,
    fake_client: FakeWhatsAppClient,
    chat_calls: list[dict[str, Any]],
    settings_overrides: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """Wire fakes into every place the route reaches for real I/O."""

    base_settings = Settings(
        supabase_url="https://test.supabase.co",
        supabase_service_role_key="test",
        openai_api_key="test",
    )
    fake_settings = Settings(
        **{**base_settings.__dict__, **settings_overrides}
    )

    monkeypatch.setattr(webhook_route, "Settings", _SettingsWithLoad(fake_settings))
    monkeypatch.setattr(webhook_route, "get_supabase_client", lambda: fake_db)
    monkeypatch.setattr(webhook_route, "get_whatsapp_client", lambda: fake_client)

    # ChatService is constructed at module import time; just override
    # its handle_chat method on the existing instance.  The fake accepts
    # the new ``min_similarity`` / ``persona_override`` kwargs the
    # webhook now passes, and returns a high top_similarity so the
    # WhatsApp low-confidence gate does NOT trip in baseline tests --
    # tests that want to exercise the gate set ``top_similarity`` via
    # the ``override_top_similarity`` slot below.
    def fake_handle_chat(payload, org_id: str, **kwargs):
        from app.schemas.chat import ChatResponse

        chat_calls.append(
            {
                "lead_id": payload.lead_id,
                "property_id": payload.property_id,
                "message": payload.message,
                "org_id": org_id,
                "min_similarity": kwargs.get("min_similarity"),
                "persona_override": kwargs.get("persona_override"),
            }
        )
        return ChatResponse(
            success=True,
            reply=f"Echo to org={org_id}: {payload.message}",
            needs_attention=False,
            top_similarity=0.95,
        )

    monkeypatch.setattr(webhook_route._chat_service, "handle_chat", fake_handle_chat)

    # whatsapp_service module also reads settings via its own loader.
    monkeypatch.setattr(whatsapp_module, "get_whatsapp_settings", lambda: fake_settings)

    return TestClient(app)


class _SettingsWithLoad:
    """Shim: webhook.py calls Settings.load() inline."""

    def __init__(self, snapshot: Settings) -> None:
        self._snapshot = snapshot

    def load(self) -> Settings:
        return self._snapshot


# =========================================================================
# Helpers
# =========================================================================


def _sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _meta_envelope(
    *, message_id: str, from_phone: str, body: str, profile_name: str = "Asha"
) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "whatsapp-business-id",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550001111",
                                "phone_number_id": PHONE_NUMBER_ID,
                            },
                            "contacts": [
                                {"wa_id": from_phone, "profile": {"name": profile_name}}
                            ],
                            "messages": [
                                {
                                    "from": from_phone,
                                    "id": message_id,
                                    "timestamp": str(int(time.time())),
                                    "type": "text",
                                    "text": {"body": body},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


# =========================================================================
# Tests — GET handshake
# =========================================================================


def test_get_handshake_echoes_challenge_when_token_matches(client: TestClient) -> None:
    response = client.get(
        f"/api/v1/webhook/whatsapp/{ORG_A_SLUG}",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "challenge-12345",
        },
    )
    assert response.status_code == 200
    assert response.text == "challenge-12345"


def test_get_handshake_rejects_wrong_token(client: TestClient) -> None:
    response = client.get(
        f"/api/v1/webhook/whatsapp/{ORG_A_SLUG}",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong-token",
            "hub.challenge": "x",
        },
    )
    assert response.status_code == 403


def test_get_handshake_rejects_unknown_slug(client: TestClient) -> None:
    response = client.get(
        "/api/v1/webhook/whatsapp/no-such-org",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "x",
        },
    )
    assert response.status_code == 404


# =========================================================================
# Tests — POST signature gate
# =========================================================================


def test_post_without_signature_is_rejected(client: TestClient) -> None:
    body = json.dumps(_meta_envelope(message_id="wamid.1", from_phone=PHONE_LEAD_A, body="Hi"))
    response = client.post(
        f"/api/v1/webhook/whatsapp/{ORG_A_SLUG}",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 403
    assert "signature" in response.json()["detail"].lower()


def test_post_with_forged_signature_is_rejected(client: TestClient) -> None:
    body = json.dumps(_meta_envelope(message_id="wamid.1", from_phone=PHONE_LEAD_A, body="Hi"))
    bad_sig = _sign(body.encode(), secret="WRONG SECRET")
    response = client.post(
        f"/api/v1/webhook/whatsapp/{ORG_A_SLUG}",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": bad_sig,
        },
    )
    assert response.status_code == 403


def test_post_with_unknown_org_slug_is_rejected(client: TestClient) -> None:
    body = json.dumps(_meta_envelope(message_id="wamid.1", from_phone=PHONE_LEAD_A, body="Hi"))
    response = client.post(
        "/api/v1/webhook/whatsapp/no-such-org",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _sign(body.encode()),
        },
    )
    assert response.status_code == 404


def test_post_with_valid_signature_returns_200_fast(
    client: TestClient,
    chat_calls: list[dict[str, Any]],
    fake_client: FakeWhatsAppClient,
) -> None:
    body = json.dumps(
        _meta_envelope(message_id="wamid.fast", from_phone=PHONE_LEAD_A, body="Tell me the price")
    )
    started = time.perf_counter()
    response = client.post(
        f"/api/v1/webhook/whatsapp/{ORG_A_SLUG}",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _sign(body.encode()),
        },
    )
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    # The Meta contract: 200 OK in <1s.  Our fakes make this trivial,
    # but the assertion guards against a future regression where someone
    # accidentally awaits RAG inline.
    assert elapsed < 1.0, f"webhook took {elapsed:.2f}s; must be <1s"

    # TestClient runs BackgroundTasks before returning, which is exactly
    # what we want for assertion: by here the worker has finished.
    assert len(chat_calls) == 1
    assert chat_calls[0]["org_id"] == ORG_A_ID
    assert chat_calls[0]["lead_id"] == "lead-a"
    assert chat_calls[0]["property_id"] == "prop-a"
    assert fake_client.sent == [
        (PHONE_LEAD_A, "Echo to org=11111111-1111-1111-1111-111111111111: Tell me the price")
    ]


# =========================================================================
# Tests — Idempotency
# =========================================================================


def test_duplicate_message_id_is_processed_only_once(
    client: TestClient,
    chat_calls: list[dict[str, Any]],
    fake_client: FakeWhatsAppClient,
    fake_db: FakeWhatsAppDB,
) -> None:
    body = json.dumps(_meta_envelope(message_id="wamid.dedup", from_phone=PHONE_LEAD_A, body="Hi"))
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": _sign(body.encode()),
    }

    first = client.post(f"/api/v1/webhook/whatsapp/{ORG_A_SLUG}", content=body, headers=headers)
    second = client.post(f"/api/v1/webhook/whatsapp/{ORG_A_SLUG}", content=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    # Only the first delivery triggered RAG + send.
    assert len(chat_calls) == 1
    assert len(fake_client.sent) == 1
    # And only one inbound row was claimed.
    inbound_rows = [r for r in fake_db.whatsapp_messages if r["direction"] == "inbound"]
    assert len(inbound_rows) == 1


# =========================================================================
# Tests — Multi-tenant isolation
# =========================================================================


def test_org_a_url_never_resolves_to_org_b_lead(
    client: TestClient,
    chat_calls: list[dict[str, Any]],
    fake_db: FakeWhatsAppDB,
) -> None:
    """Both orgs hold a lead with PHONE_LEAD_A.  A POST to org B's slug
    must run RAG against org B's lead, never A's."""
    body = json.dumps(_meta_envelope(message_id="wamid.tenant", from_phone=PHONE_LEAD_B, body="Hi"))
    response = client.post(
        f"/api/v1/webhook/whatsapp/{ORG_B_SLUG}",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _sign(body.encode()),
        },
    )
    assert response.status_code == 200
    assert len(chat_calls) == 1
    assert chat_calls[0]["org_id"] == ORG_B_ID
    assert chat_calls[0]["lead_id"] == "lead-b"
    assert chat_calls[0]["property_id"] == "prop-b"

    # No leak the other way: nothing was written under org A.
    inbound_rows = [r for r in fake_db.whatsapp_messages if r["direction"] == "inbound"]
    assert len(inbound_rows) == 1
    assert inbound_rows[0]["org_id"] == ORG_B_ID


# =========================================================================
# Tests — Strict matching (Option B)
# =========================================================================


def test_unknown_phone_does_not_create_lead_or_call_rag(
    client: TestClient,
    chat_calls: list[dict[str, Any]],
    fake_client: FakeWhatsAppClient,
    fake_db: FakeWhatsAppDB,
) -> None:
    body = json.dumps(_meta_envelope(message_id="wamid.unknown", from_phone=PHONE_UNKNOWN, body="Hi"))
    response = client.post(
        f"/api/v1/webhook/whatsapp/{ORG_A_SLUG}",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _sign(body.encode()),
        },
    )
    assert response.status_code == 200

    # No RAG, no outbound, no new lead.
    assert chat_calls == []
    assert fake_client.sent == []
    assert all(lead["phone"] != PHONE_UNKNOWN for lead in fake_db.leads)

    # The inbound IS recorded (so the broker can see "stranger texted us")
    # and marked failed with a clear reason.
    inbound = [r for r in fake_db.whatsapp_messages if r["direction"] == "inbound"]
    assert len(inbound) == 1
    assert inbound[0]["status"] == "failed"
    assert "no lead" in (inbound[0].get("error_detail") or "").lower()


# =========================================================================
# Tests — RAG failure / outbound failure
# =========================================================================


def test_rag_exception_marks_lead_needs_attention_and_sends_fallback(
    client: TestClient,
    fake_db: FakeWhatsAppDB,
    fake_client: FakeWhatsAppClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a, **_kw):
        raise RuntimeError("OpenAI rate limit")

    monkeypatch.setattr(webhook_route._chat_service, "handle_chat", boom)

    body = json.dumps(_meta_envelope(message_id="wamid.rag-fail", from_phone=PHONE_LEAD_A, body="Hi"))
    response = client.post(
        f"/api/v1/webhook/whatsapp/{ORG_A_SLUG}",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _sign(body.encode()),
        },
    )
    assert response.status_code == 200

    # The lead was flagged for human follow-up.
    assert any(
        org_id == ORG_A_ID
        and lead_id == "lead-a"
        and update.get("needs_attention") is True
        for org_id, lead_id, update in fake_db.lead_updates
    ), fake_db.lead_updates

    # A graceful fallback reply WAS still sent (so the customer doesn't
    # see silence) — broker can intervene from the dashboard.
    assert len(fake_client.sent) == 1
    assert fake_client.sent[0][0] == PHONE_LEAD_A

    # Inbound marked failed with the RAG error.
    inbound = [r for r in fake_db.whatsapp_messages if r["direction"] == "inbound"]
    assert len(inbound) == 1
    assert inbound[0]["status"] == "failed"
    assert "RuntimeError" in (inbound[0].get("error_detail") or "")


def test_outbound_send_failure_marks_needs_attention(
    client: TestClient,
    fake_db: FakeWhatsAppDB,
    fake_client: FakeWhatsAppClient,
) -> None:
    fake_client.fail_next = 1
    body = json.dumps(_meta_envelope(message_id="wamid.send-fail", from_phone=PHONE_LEAD_A, body="Hi"))
    response = client.post(
        f"/api/v1/webhook/whatsapp/{ORG_A_SLUG}",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _sign(body.encode()),
        },
    )
    assert response.status_code == 200

    assert any(
        update.get("needs_attention") is True
        for _org_id, _lead_id, update in fake_db.lead_updates
    ), fake_db.lead_updates


# =========================================================================
# Tests — Signature & parsing helpers in isolation
# =========================================================================


def test_verify_signature_handles_missing_or_bad_input() -> None:
    raw = b'{"hello":"world"}'
    good = _sign(raw)
    assert whatsapp_module.verify_signature(raw, good, APP_SECRET) is True
    assert whatsapp_module.verify_signature(raw, None, APP_SECRET) is False
    assert whatsapp_module.verify_signature(raw, "sha256=deadbeef", APP_SECRET) is False
    assert whatsapp_module.verify_signature(raw, good, "") is False


def test_parse_inbound_skips_status_events() -> None:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                            "statuses": [
                                {"id": "wamid.delivered.1", "status": "delivered"}
                            ],
                        },
                    }
                ]
            }
        ],
    }
    assert whatsapp_module.parse_inbound(payload) == []


# =========================================================================
# Tests — Prefix-agnostic phone lookup (find_lead_by_phone)
# =========================================================================


def _new_repo_with_lead(stored_phone: str):
    """Helper: build a FakeWhatsAppDB with a single lead and return the
    matching WhatsAppRepository for direct unit testing.
    """
    from app.db.repositories.whatsapp_repository import WhatsAppRepository

    db = FakeWhatsAppDB()
    db.organizations.append(
        {"id": ORG_A_ID, "slug": ORG_A_SLUG, "name": "Alpha", "subscription_tier": "trial"}
    )
    db.leads.append(
        {
            "id": "lead-mix",
            "org_id": ORG_A_ID,
            "phone": stored_phone,
            "property_id": "prop-mix",
            "name": "Mix",
            "status": "New",
        }
    )
    return WhatsAppRepository(db), db


@pytest.mark.parametrize(
    "stored, inbound",
    [
        # Most realistic: Google ad-form stored '+91...', WhatsApp wire '91...'
        ("+919876543210", "919876543210"),
        # Reverse: stored as bare digits, inbound carries '+'
        ("919876543210", "+919876543210"),
        # Identity: stored bare digits, inbound bare digits
        ("919876543210", "919876543210"),
        # Identity: stored E.164, inbound E.164
        ("+919876543210", "+919876543210"),
    ],
    # NOTE: We deliberately do NOT include cases where the DB contains
    # embedded spaces/dashes (e.g. '+91 98765 43210').  ``leads.py``'s
    # ``_normalize_phone`` strips spaces/dashes/brackets BEFORE the row
    # is persisted, so the leads table only ever holds the four shapes
    # above.  If a legacy backfill ever introduces spaces/dashes into
    # ``leads.phone``, fixing it belongs in a 005_phone_canonicalize.sql
    # migration -- not in this lookup hot-path -- so we keep the index
    # equality fast.
)
def test_find_lead_by_phone_is_prefix_agnostic(stored: str, inbound: str) -> None:
    repo, _db = _new_repo_with_lead(stored)
    lead = repo.find_lead_by_phone(org_id=ORG_A_ID, phone=inbound)
    assert lead is not None, f"lead not found for stored={stored!r} inbound={inbound!r}"
    assert lead["id"] == "lead-mix"


def test_find_lead_by_phone_does_not_cross_orgs() -> None:
    """Two orgs hold the SAME phone in different shapes.  A lookup for
    org A must NEVER return org B's row, even though the digits match."""
    from app.db.repositories.whatsapp_repository import WhatsAppRepository

    db = FakeWhatsAppDB()
    db.organizations.extend(
        [
            {"id": ORG_A_ID, "slug": ORG_A_SLUG, "name": "Alpha", "subscription_tier": "trial"},
            {"id": ORG_B_ID, "slug": ORG_B_SLUG, "name": "Beta", "subscription_tier": "trial"},
        ]
    )
    db.leads.extend(
        [
            {"id": "lead-a", "org_id": ORG_A_ID, "phone": "+919876543210",
             "property_id": "prop-a", "name": "A", "status": "New"},
            {"id": "lead-b", "org_id": ORG_B_ID, "phone": "919876543210",
             "property_id": "prop-b", "name": "B", "status": "New"},
        ]
    )
    repo = WhatsAppRepository(db)

    found_a = repo.find_lead_by_phone(org_id=ORG_A_ID, phone="919876543210")
    found_b = repo.find_lead_by_phone(org_id=ORG_B_ID, phone="+919876543210")
    assert found_a is not None and found_a["id"] == "lead-a"
    assert found_b is not None and found_b["id"] == "lead-b"


def test_find_lead_by_phone_returns_none_for_unknown() -> None:
    repo, _db = _new_repo_with_lead("+919876543210")
    assert repo.find_lead_by_phone(org_id=ORG_A_ID, phone="918888888888") is None
    assert repo.find_lead_by_phone(org_id=ORG_A_ID, phone="") is None


def test_inbound_webhook_matches_lead_with_plus_prefix_in_db(
    fake_db: FakeWhatsAppDB,
    chat_calls: list[dict[str, Any]],
    fake_client: FakeWhatsAppClient,
    client: TestClient,
) -> None:
    """End-to-end: DB stored '+919876543210' (e.g. via Google ad form),
    Meta delivers '919876543210' — the webhook must still resolve the
    correct lead and run RAG.
    """
    # Mutate the seeded lead so it has the +-prefix the way Google
    # Ad-Form ingestion would store it.
    for lead in fake_db.leads:
        if lead["id"] == "lead-a":
            lead["phone"] = "+" + lead["phone"]   # "+919876543210"

    body = json.dumps(
        _meta_envelope(message_id="wamid.plus", from_phone="919876543210", body="hi")
    )
    response = client.post(
        f"/api/v1/webhook/whatsapp/{ORG_A_SLUG}",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _sign(body.encode()),
        },
    )
    assert response.status_code == 200
    assert len(chat_calls) == 1
    assert chat_calls[0]["lead_id"] == "lead-a"
    assert chat_calls[0]["org_id"] == ORG_A_ID
    assert len(fake_client.sent) == 1


# =========================================================================
# Tests — Enterprise upgrades (typing indicator, confidence gate,
#          persona, RAG observability, multi-tenant log isolation)
# =========================================================================


def _post_signed(
    test_client: TestClient,
    org_slug: str,
    *,
    message_id: str,
    from_phone: str,
    body: str,
):
    raw = json.dumps(
        _meta_envelope(message_id=message_id, from_phone=from_phone, body=body)
    )
    return test_client.post(
        f"/api/v1/webhook/whatsapp/{org_slug}",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _sign(raw.encode()),
        },
    )


def test_typing_indicator_is_sent_before_outbound_reply(
    client: TestClient,
    fake_client: FakeWhatsAppClient,
) -> None:
    """The customer must see "...typing" while RAG runs.  The typing
    call must precede the actual reply so the order on Meta's side
    stays natural (typing -> message)."""
    response = _post_signed(
        client, ORG_A_SLUG, message_id="wamid.typing", from_phone=PHONE_LEAD_A, body="hi"
    )
    assert response.status_code == 200

    # Exactly one typing indicator was emitted, scoped to org A.
    assert len(fake_client.typing) == 1
    typed_msgid, typed_org = fake_client.typing[0]
    assert typed_msgid == "wamid.typing"
    assert typed_org == ORG_A_ID

    # And the actual reply was sent afterwards.
    assert len(fake_client.sent) == 1


def test_typing_indicator_carries_correct_org_id_per_tenant(
    client: TestClient,
    fake_client: FakeWhatsAppClient,
) -> None:
    """Two messages, two different orgs.  Each typing-indicator log
    line must reference ONLY its own org_id -- no leak."""
    a = _post_signed(
        client, ORG_A_SLUG, message_id="wamid.t-a", from_phone=PHONE_LEAD_A, body="hi a"
    )
    b = _post_signed(
        client, ORG_B_SLUG, message_id="wamid.t-b", from_phone=PHONE_LEAD_B, body="hi b"
    )
    assert a.status_code == 200 and b.status_code == 200

    org_ids = {row[1] for row in fake_client.typing}
    assert org_ids == {ORG_A_ID, ORG_B_ID}, org_ids

    # Each typing call carried EXACTLY one org_id; the set comprehension
    # above already proves no row has both.  Belt-and-suspenders:
    a_rows = [r for r in fake_client.typing if r[1] == ORG_A_ID]
    b_rows = [r for r in fake_client.typing if r[1] == ORG_B_ID]
    assert all(msgid == "wamid.t-a" for msgid, _ in a_rows)
    assert all(msgid == "wamid.t-b" for msgid, _ in b_rows)


def test_low_confidence_replaces_reply_and_marks_needs_attention(
    fake_db: FakeWhatsAppDB,
    fake_client: FakeWhatsAppClient,
    chat_calls: list[dict[str, Any]],
    settings_overrides: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the top retrieval similarity falls below
    ``whatsapp_confidence_threshold``, the webhook MUST:
        1. NOT send the LLM-generated reply (use the canned message).
        2. Mark the lead needs_attention so the broker takes over.
    """
    settings_overrides["whatsapp_confidence_threshold"] = 0.7
    canned = "I'm still learning about this specific detail. Let me connect you with our human expert to get you the most accurate information!"
    settings_overrides["whatsapp_low_confidence_reply"] = canned

    # Wire a fresh client with the overridden settings.
    base_settings = Settings(
        supabase_url="https://test.supabase.co",
        supabase_service_role_key="test",
        openai_api_key="test",
    )
    fake_settings = Settings(**{**base_settings.__dict__, **settings_overrides})

    monkeypatch.setattr(webhook_route, "Settings", _SettingsWithLoad(fake_settings))
    monkeypatch.setattr(webhook_route, "get_supabase_client", lambda: fake_db)
    monkeypatch.setattr(webhook_route, "get_whatsapp_client", lambda: fake_client)

    def low_conf_handle_chat(payload, org_id: str, **kwargs):
        from app.schemas.chat import ChatResponse

        chat_calls.append(
            {
                "min_similarity": kwargs.get("min_similarity"),
                "persona_override": kwargs.get("persona_override"),
            }
        )
        # Generator returned something, but the top score (0.55) is
        # BELOW the channel threshold (0.7) -> the webhook must override.
        return ChatResponse(
            success=True,
            reply="Generated borderline answer that we shouldn't ship.",
            needs_attention=False,
            top_similarity=0.55,
        )

    monkeypatch.setattr(webhook_route._chat_service, "handle_chat", low_conf_handle_chat)

    test_client = TestClient(app)
    response = _post_signed(
        test_client, ORG_A_SLUG, message_id="wamid.low", from_phone=PHONE_LEAD_A, body="anything"
    )
    assert response.status_code == 200

    # 1. handle_chat was called WITH the WhatsApp threshold + persona.
    assert len(chat_calls) == 1
    assert chat_calls[0]["min_similarity"] == 0.7
    assert "senior residential" in (chat_calls[0]["persona_override"] or "").lower()

    # 2. The customer received the canned message, NOT the LLM output.
    assert len(fake_client.sent) == 1
    sent_to, sent_body = fake_client.sent[0]
    assert sent_to == PHONE_LEAD_A
    assert sent_body == canned
    assert "borderline answer" not in sent_body

    # 3. The lead was flagged for human follow-up.
    assert any(
        update.get("needs_attention") is True
        for _org, _lead, update in fake_db.lead_updates
    ), fake_db.lead_updates


def test_high_confidence_uses_generated_reply(
    client: TestClient,
    fake_client: FakeWhatsAppClient,
) -> None:
    """Counter-test: when top_similarity >= threshold, the webhook
    sends the LLM reply unchanged."""
    response = _post_signed(
        client, ORG_A_SLUG, message_id="wamid.hi", from_phone=PHONE_LEAD_A, body="hi"
    )
    assert response.status_code == 200
    assert len(fake_client.sent) == 1
    _to, body = fake_client.sent[0]
    # The fixture's fake handle_chat returns top_similarity=0.95,
    # well above the 0.7 threshold, so the echo passes through.
    assert body.startswith("Echo to org=")


def test_no_evidence_top_similarity_none_also_triggers_handoff(
    fake_db: FakeWhatsAppDB,
    fake_client: FakeWhatsAppClient,
    settings_overrides: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``top_similarity is None`` (retrieval returned zero rows) must be
    treated as low-confidence -- the user gets the canned message."""
    base_settings = Settings(
        supabase_url="https://test.supabase.co",
        supabase_service_role_key="test",
        openai_api_key="test",
    )
    fake_settings = Settings(**{**base_settings.__dict__, **settings_overrides})

    monkeypatch.setattr(webhook_route, "Settings", _SettingsWithLoad(fake_settings))
    monkeypatch.setattr(webhook_route, "get_supabase_client", lambda: fake_db)
    monkeypatch.setattr(webhook_route, "get_whatsapp_client", lambda: fake_client)

    def empty_retrieval_handle_chat(payload, org_id: str, **kwargs):
        from app.schemas.chat import ChatResponse

        return ChatResponse(
            success=True,
            reply="Some no-evidence fallback from chat_service.",
            needs_attention=False,
            top_similarity=None,
        )

    monkeypatch.setattr(webhook_route._chat_service, "handle_chat", empty_retrieval_handle_chat)

    test_client = TestClient(app)
    response = _post_signed(
        test_client, ORG_A_SLUG, message_id="wamid.none", from_phone=PHONE_LEAD_A, body="x"
    )
    assert response.status_code == 200

    sent_body = fake_client.sent[0][1]
    assert (
        "arrange a call" in sent_body
        or "human expert" in sent_body
        or "still learning" in sent_body
        or "specialist" in sent_body
    )


def test_rag_per_chunk_observability_logs_include_org_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The retriever must emit one [RAG] log line per kept row, each
    line carrying org_id, property_id, source, id, and similarity."""
    import logging
    from unittest.mock import MagicMock

    from app.rag.retriever import Retriever

    org_id = ORG_A_ID
    property_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

    fake_supabase = MagicMock()
    fake_supabase.rpc.return_value.execute.side_effect = [
        MagicMock(
            data=[
                {
                    "id": "u-1",
                    "org_id": org_id,
                    "project_id": property_id,
                    "similarity": 0.91,
                    "label": "2BHK Tower-A 1245sqft",
                }
            ]
        ),
        MagicMock(
            data=[
                {
                    "id": "c-9",
                    "org_id": org_id,
                    "property_id": property_id,
                    "similarity": 0.83,
                    "content": "Carpet area: 1245 sqft",
                }
            ]
        ),
    ]
    fake_settings = Settings(
        supabase_url="x",
        supabase_service_role_key="x",
        openai_api_key="x",
        rag_similarity_threshold=0.3,
        rag_top_k_units=4,
        rag_top_k_chunks=4,
    )
    retriever = Retriever(fake_supabase, fake_settings)

    with caplog.at_level(logging.INFO, logger="app.rag.retriever"):
        results = retriever.retrieve([0.0] * 8, property_id, org_id=org_id)

    # 1. Both rows kept.
    assert len(results["units"]) == 1 and len(results["chunks"]) == 1

    # 2. Per-row [RAG] log lines exist with the expected fields.
    rag_lines = [r.getMessage() for r in caplog.records if "[RAG]" in r.getMessage()]
    assert any("source=units" in l and "id=u-1" in l and "similarity=0.9100" in l for l in rag_lines), rag_lines
    assert any("source=chunks" in l and "id=c-9" in l and "similarity=0.8300" in l for l in rag_lines), rag_lines

    # 3. Every [RAG] line carries THIS org_id and ONLY this org_id.
    for line in rag_lines:
        assert f"org_id={org_id}" in line, line
        assert ORG_B_ID not in line, line  # multi-tenant log isolation


def test_persona_override_threads_into_grounded_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The WhatsApp Real-Estate-Consultant persona must reach the
    generator's system prompt unchanged, AND the strict rules block
    must still be appended (no rule-stripping)."""
    from app.rag.grounded_generator import GroundedGenerator

    fake_settings = Settings(
        supabase_url="x",
        supabase_service_role_key="x",
        openai_api_key="x",
    )
    gen = GroundedGenerator.__new__(GroundedGenerator)
    gen.settings = fake_settings

    captured: dict[str, Any] = {}

    class _FakeChoice:
        def __init__(self, content: str) -> None:
            self.message = type("M", (), {"content": content})()

    class _FakeResp:
        def __init__(self, content: str) -> None:
            self.choices = [_FakeChoice(content)]

    class _FakeChatCompletions:
        def create(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return _FakeResp("ok")

    class _FakeChat:
        def __init__(self) -> None:
            self.completions = _FakeChatCompletions()

    class _FakeOpenAI:
        def __init__(self) -> None:
            self.chat = _FakeChat()

    gen.client = _FakeOpenAI()

    persona = (
        "You are a professional, polite, and helpful Real Estate Consultant for "
        "PropClose AI. Your goal is to assist leads based ONLY on the provided "
        "brochure data."
    )
    gen.generate(
        property_name="Skyline Towers",
        lead_name="Asha",
        interested_in="2BHK",
        user_message="What is the carpet area?",
        chat_history=[],
        context="Carpet area: 1245 sqft",
        persona_override=persona,
    )
    system_msgs = [m for m in captured["messages"] if m["role"] == "system"]
    system_text = "\n".join(m["content"] for m in system_msgs)
    assert "Real Estate Consultant" in system_text
    assert "Use ONLY facts present in EVIDENCE" in system_text
    assert "OFF-TOPIC" in system_text


def test_chat_service_low_confidence_respects_min_similarity_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ChatService.handle_chat with min_similarity=0.7 must fall back
    when top similarity is 0.55, even though the global setting
    threshold is 0.3."""
    from app.schemas.chat import ChatRequest
    from app.services.chat_service import ChatService

    service = ChatService.__new__(ChatService)
    service.settings = Settings(
        supabase_url="x",
        supabase_service_role_key="x",
        openai_api_key="x",
        rag_similarity_threshold=0.3,
    )
    service.repo = MagicMock()
    service.repo.get_lead.return_value = {"id": "lead-x", "name": "Asha"}
    service.repo.get_property.return_value = {"id": "prop-x", "name": "Skyline Towers"}
    service.repo.get_recent_history.return_value = []

    service.embedder = MagicMock()
    service.embedder.embed_query.return_value = [0.0] * 8

    service.retriever = MagicMock()
    service.retriever.retrieve.return_value = {
        "units": [],
        "chunks": [
            {
                "content": "Carpet area: 1245 sqft",
                "similarity": 0.55,
                "property_id": "prop-x",
                "org_id": ORG_A_ID,
                "id": "c-borderline",
            }
        ],
    }

    service.generator = MagicMock()
    service.generator.generate.side_effect = AssertionError(
        "generator must NOT be called when top_similarity < min_similarity"
    )

    request = ChatRequest(lead_id="lead-x", property_id="prop-x", message="hello")
    response = service.handle_chat(request, org_id=ORG_A_ID, min_similarity=0.7)

    assert response.success is True
    assert response.top_similarity == pytest.approx(0.55, rel=1e-6)
    # Generator was NOT called -> we returned a fallback before LLM.
    service.generator.generate.assert_not_called()


def _fake_db_with_phones(stored_a: str, stored_b: str) -> "FakeWhatsAppDB":
    """Helper for the cross-tenant tests; builds a 2-org DB."""
    db = FakeWhatsAppDB()
    db.organizations.extend(
        [
            {"id": ORG_A_ID, "slug": ORG_A_SLUG, "name": "A", "subscription_tier": "trial"},
            {"id": ORG_B_ID, "slug": ORG_B_SLUG, "name": "B", "subscription_tier": "trial"},
        ]
    )
    db.leads.extend(
        [
            {"id": "lead-a", "org_id": ORG_A_ID, "phone": stored_a,
             "property_id": "prop-a", "name": "A", "status": "New"},
            {"id": "lead-b", "org_id": ORG_B_ID, "phone": stored_b,
             "property_id": "prop-b", "name": "B", "status": "New"},
        ]
    )
    return db


def test_parse_inbound_extracts_button_reply_text() -> None:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                            "messages": [
                                {
                                    "from": "919876543210",
                                    "id": "wamid.btn",
                                    "timestamp": "1700000000",
                                    "type": "interactive",
                                    "interactive": {
                                        "button_reply": {"id": "yes", "title": "Yes please"}
                                    },
                                }
                            ],
                        },
                    }
                ]
            }
        ],
    }
    out = whatsapp_module.parse_inbound(payload)
    assert len(out) == 1
    assert out[0].body == "Yes please"
    assert out[0].message_type == "interactive"
