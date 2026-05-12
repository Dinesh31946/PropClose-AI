"""Phase 2 — Robustness / Edge-case tests for PropClose AI.

These tests deliberately exercise the four contracts the Blueprint calls
"data integrity is priority #1":

    1. Option B          — phone is NOT NULL; null/empty must be refused.
    2. Strict matching   — typo in `property_name` must NOT auto-link.
    3. Deduplication     — same phone + same property must NOT create a 2nd row.
    4. RAG retrieval     — chat engine must surface a "price" from a brochure
                            chunk (mocked) all the way to the reply.

The suite is hermetic: no Supabase, no OpenAI, no Meta calls. Every external
boundary is monkey-patched. Every request also carries an ``X-Org-Id`` header
because the multi-tenant refactor made tenancy non-negotiable.

Run from the repo root:

    .\\backend\\venv\\Scripts\\python.exe -m pytest backend/tests/test_robustness.py -v
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.chat import ChatRequest
from app.services import automation_service as automation_module
from app.services import ingestion_service as ingestion_module
from app.services.chat_service import ChatService

# Stable test tenant — must match the X-Org-Id header on every request.
TEST_ORG_ID = "11111111-1111-1111-1111-111111111111"
TENANT_HEADERS = {"X-Org-Id": TEST_ORG_ID}


# ---------------------------------------------------------------------------
# Minimal fluent fake of the supabase-py client (only what leads.py touches).
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, data: Any) -> None:
        self.data = data


class FakeQuery:
    """Captures supabase-py fluent calls and dispatches to FakeDB on execute()."""

    def __init__(self, table_name: str, db: "FakeDB") -> None:
        self.table_name = table_name
        self.db = db
        self.action = "select"
        self.payload: Any = None
        self.filters: list[tuple[str, str, Any]] = []
        self.on_conflict: str | None = None

    def select(self, *_args, **_kwargs) -> "FakeQuery":
        self.action = "select"
        return self

    def insert(self, payload: Any) -> "FakeQuery":
        self.action = "insert"
        self.payload = payload
        return self

    def upsert(self, payload: Any, on_conflict: str | None = None) -> "FakeQuery":
        self.action = "upsert"
        self.payload = payload
        self.on_conflict = on_conflict
        return self

    def update(self, payload: Any) -> "FakeQuery":
        self.action = "update"
        self.payload = payload
        return self

    def eq(self, field: str, value: Any) -> "FakeQuery":
        self.filters.append(("eq", field, value))
        return self

    def is_(self, field: str, value: Any) -> "FakeQuery":
        self.filters.append(("is", field, value))
        return self

    def in_(self, field: str, values: Any) -> "FakeQuery":
        self.filters.append(("in", field, values))
        return self

    def order(self, *_args, **_kwargs) -> "FakeQuery":
        return self

    def limit(self, _n: int) -> "FakeQuery":
        return self

    def single(self) -> "FakeQuery":
        return self

    def execute(self) -> FakeResponse:
        return self.db.run(self)


class FakeDB:
    """In-memory DB for the leads route. Stores only what we need to assert."""

    def __init__(self) -> None:
        self.properties: list[dict[str, Any]] = []
        # Lead rows keyed by (org_id, phone, property_id) so we can prove the
        # tenant-scoped uniqueness contract.
        self.leads: dict[tuple[str, str | None, str | None], dict[str, Any]] = {}
        self._lead_counter = 0

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(name, self)

    def _filter_value(self, q: FakeQuery, field: str) -> Any:
        return next((v for op, f, v in q.filters if f == field and op == "eq"), None)

    def _all_eq_filters(self, q: FakeQuery) -> dict[str, Any]:
        return {f: v for op, f, v in q.filters if op == "eq"}

    def run(self, q: FakeQuery) -> FakeResponse:
        if q.table_name == "properties":
            org_id = self._filter_value(q, "org_id")
            scoped = [p for p in self.properties if (org_id is None or p.get("org_id") == org_id)]
            return FakeResponse(scoped)

        if q.table_name == "leads":
            if q.action == "select":
                eq_filters = self._all_eq_filters(q)
                org_id = eq_filters.get("org_id")
                phone = eq_filters.get("phone")
                property_id = eq_filters.get("property_id")
                key = (org_id, phone, property_id)
                row = self.leads.get(key)
                return FakeResponse([dict(row)] if row else [])

            if q.action in {"insert", "upsert"}:
                rows = q.payload if isinstance(q.payload, list) else [q.payload]
                inserted: list[dict[str, Any]] = []
                for row in rows:
                    row = dict(row)
                    key = (row.get("org_id"), row.get("phone"), row.get("property_id"))
                    if key in self.leads:
                        self.leads[key].update(row)
                        inserted.append(dict(self.leads[key]))
                    else:
                        self._lead_counter += 1
                        row["id"] = f"lead-{self._lead_counter}"
                        self.leads[key] = row
                        inserted.append(dict(row))
                return FakeResponse(inserted)

            if q.action == "update":
                eq_filters = self._all_eq_filters(q)
                target_id = eq_filters.get("id")
                target_org = eq_filters.get("org_id")
                for row in self.leads.values():
                    if row.get("id") == target_id and (
                        target_org is None or row.get("org_id") == target_org
                    ):
                        row.update(q.payload)
                        return FakeResponse([dict(row)])
                return FakeResponse([])

        return FakeResponse([])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_db() -> FakeDB:
    db = FakeDB()
    db.properties.append({"id": "prop-1", "org_id": TEST_ORG_ID, "name": "Skyline Towers"})
    return db


@pytest.fixture
def client(fake_db: FakeDB, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with Supabase + WhatsApp side-effects neutralised."""
    monkeypatch.setattr(ingestion_module, "get_supabase_client", lambda: fake_db)
    monkeypatch.setattr(automation_module, "get_supabase_client", lambda: fake_db)
    # Background welcome-message side-effect is exercised in its own test
    # file; here we no-op so route assertions stay pure.
    monkeypatch.setattr(
        automation_module.AutomationService,
        "send_welcome_message",
        lambda self, lead_id, org_id: None,
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tenant gate — every protected route must demand a tenant context.
# ---------------------------------------------------------------------------


def test_lead_creation_rejects_request_without_tenant_header(client: TestClient) -> None:
    response = client.post(
        "/api/v1/leads",
        json={"name": "Asha", "phone": "9876543210", "property_name": "Skyline Towers"},
    )
    assert response.status_code == 401, response.text
    assert "tenant context missing" in response.json()["detail"].lower()


def test_lead_creation_rejects_malformed_org_uuid(client: TestClient) -> None:
    response = client.post(
        "/api/v1/leads",
        json={"name": "Asha", "phone": "9876543210", "property_name": "Skyline Towers"},
        headers={"X-Org-Id": "not-a-uuid"},
    )
    assert response.status_code == 400, response.text
    assert "uuid" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Edge case 1 — Option B: phone null / empty MUST be rejected.
# ---------------------------------------------------------------------------


def test_lead_rejected_when_phone_field_is_missing(client: TestClient) -> None:
    response = client.post(
        "/api/v1/leads",
        json={"name": "Asha", "property_name": "Skyline Towers"},
        headers=TENANT_HEADERS,
    )
    assert response.status_code == 422, response.text


def test_lead_rejected_when_phone_is_empty_string(client: TestClient) -> None:
    response = client.post(
        "/api/v1/leads",
        json={"name": "Asha", "phone": "", "property_name": "Skyline Towers"},
        headers=TENANT_HEADERS,
    )
    assert response.status_code == 422, response.text


def test_lead_rejected_when_phone_is_only_whitespace(client: TestClient) -> None:
    """Whitespace passes Pydantic's min_length but must fail the app's own check."""
    response = client.post(
        "/api/v1/leads",
        json={"name": "Asha", "phone": "   ", "property_name": "Skyline Towers"},
        headers=TENANT_HEADERS,
    )
    assert response.status_code == 400, response.text
    assert "phone" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Edge case 2 — Strict matching: typo in property_name MUST be rejected.
# ---------------------------------------------------------------------------


def test_lead_rejected_on_property_name_typo(client: TestClient) -> None:
    response = client.post(
        "/api/v1/leads",
        json={
            "name": "Asha",
            "phone": "9876543210",
            "property_name": "Skyline Tower",
        },
        headers=TENANT_HEADERS,
    )
    assert response.status_code == 400, response.text
    detail = response.json()["detail"].lower()
    assert "property" in detail and "mismatch" in detail


def test_lead_accepted_when_property_name_matches_exactly(client: TestClient) -> None:
    response = client.post(
        "/api/v1/leads",
        json={
            "name": "Asha",
            "phone": "9876543210",
            "property_name": "Skyline Towers",
        },
        headers=TENANT_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert body["property_id"] == "prop-1"
    assert body["org_id"] == TEST_ORG_ID


# ---------------------------------------------------------------------------
# Edge case 3 — Deduplication: same phone + same property submitted twice.
# ---------------------------------------------------------------------------


def test_duplicate_lead_returns_duplicate_flag_without_creating_new_row(
    client: TestClient, fake_db: FakeDB
) -> None:
    payload = {
        "name": "Asha",
        "phone": "98765 43210",
        "property_name": "Skyline Towers",
        "source": "Website",
    }

    first = client.post("/api/v1/leads", json=payload, headers=TENANT_HEADERS)
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["duplicate"] is False
    assert first_body["lead_id"]

    second = client.post("/api/v1/leads", json=payload, headers=TENANT_HEADERS)
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["duplicate"] is True
    assert second_body["lead_id"] == first_body["lead_id"]

    assert len(fake_db.leads) == 1


# ---------------------------------------------------------------------------
# Edge case 4 — RAG retrieval: brochure chunk ("price") reaches the reply.
# ---------------------------------------------------------------------------


def test_chat_service_uses_brochure_chunk_for_price_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Wires a mock retriever returning a `brochure_chunks`-shaped record that
    mentions price. Asserts:

      * the chat engine forwards that chunk into the generator's evidence,
      * the generator's output (stubbed to echo evidence) reaches the reply,
      * the price token survives the sales-closer policy post-processing,
      * the retriever is invoked with the request's org_id.
    """
    service = ChatService.__new__(ChatService)
    service.settings = MagicMock(rag_similarity_threshold=0.3)

    service.repo = MagicMock()
    service.repo.get_lead.return_value = {"id": "lead-1", "name": "Asha"}
    service.repo.get_property.return_value = {"id": "prop-1", "name": "Skyline Towers"}
    service.repo.get_recent_history.return_value = []

    service.embedder = MagicMock()
    service.embedder.embed_query.return_value = [0.0] * 8

    mock_chunk = {
        "content": "Price: 1.25 Cr starting for 2BHK units.",
        "similarity": 0.92,
        "property_id": "prop-1",
        "org_id": TEST_ORG_ID,
    }
    service.retriever = MagicMock()
    service.retriever.retrieve.return_value = {"units": [], "chunks": [mock_chunk]}

    captured: dict[str, str] = {}

    def fake_generate(**kwargs: Any) -> str:
        captured["context"] = kwargs["context"]
        return f"Based on brochure: {kwargs['context']}"

    service.generator = MagicMock()
    service.generator.generate.side_effect = fake_generate

    request = ChatRequest(
        lead_id="lead-1",
        property_id="prop-1",
        interested_in="2BHK",
        message="Yahan ka price kya hai?",
    )

    response = service.handle_chat(request, org_id=TEST_ORG_ID)

    # 1. The retriever was actually consulted with the lead's tenant + property scope.
    service.retriever.retrieve.assert_called_once()
    call = service.retriever.retrieve.call_args
    _embedding_arg, property_id_arg = call.args
    assert property_id_arg == "prop-1"
    assert call.kwargs.get("org_id") == TEST_ORG_ID

    # 2. The brochure chunk made it into the generator's evidence packet.
    assert "price" in captured["context"].lower(), captured
    assert "1.25 cr" in captured["context"].lower(), captured

    # 3. The reply itself carries the price token.
    assert response.success is True
    assert "1.25 cr" in response.reply.lower(), response.reply

    # 4. Persistence was tenant-scoped.
    service.repo.save_messages.assert_called_once()
    save_args = service.repo.save_messages.call_args.args
    assert TEST_ORG_ID in save_args


def test_chat_service_falls_back_when_no_brochure_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Counterpart of the test above: when retrieval returns nothing, the engine
    MUST refuse to invent a price and MUST mark the lead for human attention
    (price-keyword path -> needs_attention=True), all under the request's org_id.
    """
    service = ChatService.__new__(ChatService)
    service.settings = MagicMock(rag_similarity_threshold=0.3)

    service.repo = MagicMock()
    service.repo.get_lead.return_value = {"id": "lead-1", "name": "Asha"}
    service.repo.get_property.return_value = {"id": "prop-1", "name": "Skyline Towers"}
    service.repo.get_recent_history.return_value = []

    service.embedder = MagicMock()
    service.embedder.embed_query.return_value = [0.0] * 8

    service.retriever = MagicMock()
    service.retriever.retrieve.return_value = {"units": [], "chunks": []}

    service.generator = MagicMock()
    service.generator.generate.side_effect = AssertionError(
        "generator must NOT be called when there is zero evidence"
    )

    request = ChatRequest(
        lead_id="lead-1",
        property_id="prop-1",
        interested_in="2BHK",
        message="Iska price kitna hai?",
    )

    response = service.handle_chat(request, org_id=TEST_ORG_ID)

    assert response.success is True
    assert response.needs_attention is True
    service.repo.mark_needs_attention.assert_called_once_with("lead-1", TEST_ORG_ID)
