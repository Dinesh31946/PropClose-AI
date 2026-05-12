"""Multi-tenant isolation tests.

    Enterprise isolation layer - mandatory for SaaS scalability.

These tests prove the four contracts the multi-tenant migration was built for:

    A. Two orgs can hold a property with the SAME name without collision.
    B. Two orgs can hold a lead with the SAME phone+property without collision.
    C. A request for Org A NEVER sees Org B's properties (strict matcher
       must reject Org B's project name).
    D. The retriever NEVER pulls a chunk from another tenant, even if a
       broken RPC ever returned cross-tenant rows (defense-in-depth filter).

Hermetic: no Supabase, no OpenAI, no Meta calls.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.tenancy import reset_tenant_cache
from app.main import app
from app.rag.retriever import Retriever
from app.services import automation_service as automation_module
from app.services import ingestion_service as ingestion_module

ORG_A = "11111111-aaaa-aaaa-aaaa-111111111111"
ORG_B = "22222222-bbbb-bbbb-bbbb-222222222222"


# ---------------------------------------------------------------------------
# Multi-tenant fake DB.  Same fluent API as the FakeDB in test_robustness.py
# but stores rows for any number of tenants and enforces org-scoped reads.
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, data: Any) -> None:
        self.data = data


class FakeQuery:
    def __init__(self, table: str, db: "MultiTenantFakeDB") -> None:
        self.table_name = table
        self.db = db
        self.action = "select"
        self.payload: Any = None
        self.filters: list[tuple[str, str, Any]] = []
        self.on_conflict: str | None = None

    def select(self, *_a, **_kw): self.action = "select"; return self
    def insert(self, payload): self.action = "insert"; self.payload = payload; return self
    def upsert(self, payload, on_conflict=None):
        self.action = "upsert"; self.payload = payload; self.on_conflict = on_conflict; return self
    def update(self, payload): self.action = "update"; self.payload = payload; return self
    def eq(self, f, v): self.filters.append(("eq", f, v)); return self
    def is_(self, f, v): self.filters.append(("is", f, v)); return self
    def in_(self, f, v): self.filters.append(("in", f, v)); return self
    def order(self, *_a, **_kw): return self
    def limit(self, _n): return self
    def single(self): return self
    def execute(self): return self.db.run(self)


class MultiTenantFakeDB:
    def __init__(self) -> None:
        self.properties: list[dict[str, Any]] = []
        self.leads: list[dict[str, Any]] = []
        self._lead_counter = 0

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(name, self)

    @staticmethod
    def _eq_filters(q: FakeQuery) -> dict[str, Any]:
        return {f: v for op, f, v in q.filters if op == "eq"}

    def run(self, q: FakeQuery) -> FakeResponse:
        eqs = self._eq_filters(q)

        if q.table_name == "properties":
            if q.action == "select":
                return FakeResponse(
                    [p for p in self.properties if all(p.get(k) == v for k, v in eqs.items())]
                )

        if q.table_name == "leads":
            if q.action == "select":
                return FakeResponse(
                    [l for l in self.leads if all(l.get(k) == v for k, v in eqs.items())]
                )
            if q.action in {"insert", "upsert"}:
                rows = q.payload if isinstance(q.payload, list) else [q.payload]
                inserted: list[dict[str, Any]] = []
                for raw in rows:
                    row = dict(raw)
                    # Lookup an existing row matching the upsert "natural key".
                    match = None
                    for existing in self.leads:
                        if (
                            existing.get("org_id") == row.get("org_id")
                            and existing.get("phone") == row.get("phone")
                            and existing.get("property_id") == row.get("property_id")
                        ):
                            match = existing
                            break
                    if match is not None:
                        match.update(row)
                        inserted.append(dict(match))
                    else:
                        self._lead_counter += 1
                        row["id"] = f"lead-{self._lead_counter}"
                        self.leads.append(row)
                        inserted.append(dict(row))
                return FakeResponse(inserted)

        return FakeResponse([])


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    reset_tenant_cache()


@pytest.fixture
def fake_db() -> MultiTenantFakeDB:
    db = MultiTenantFakeDB()
    # Both orgs intentionally name their flagship project the same.
    db.properties.append({"id": "prop-A", "org_id": ORG_A, "name": "Skyline Towers"})
    db.properties.append({"id": "prop-B", "org_id": ORG_B, "name": "Skyline Towers"})
    return db


@pytest.fixture
def client(fake_db: MultiTenantFakeDB, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(ingestion_module, "get_supabase_client", lambda: fake_db)
    monkeypatch.setattr(automation_module, "get_supabase_client", lambda: fake_db)
    monkeypatch.setattr(
        automation_module.AutomationService,
        "send_welcome_message",
        lambda self, lead_id, org_id: None,
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# A — same property name, different orgs: BOTH writes succeed.
# ---------------------------------------------------------------------------


def test_two_orgs_can_create_leads_with_same_phone_and_same_property_name(
    client: TestClient, fake_db: MultiTenantFakeDB
) -> None:
    payload = {
        "name": "Asha",
        "phone": "9876543210",
        "property_name": "Skyline Towers",
    }

    a = client.post("/api/v1/leads", json=payload, headers={"X-Org-Id": ORG_A})
    b = client.post("/api/v1/leads", json=payload, headers={"X-Org-Id": ORG_B})

    assert a.status_code == 200, a.text
    assert b.status_code == 200, b.text

    body_a, body_b = a.json(), b.json()
    assert body_a["org_id"] == ORG_A
    assert body_b["org_id"] == ORG_B
    assert body_a["property_id"] == "prop-A"
    assert body_b["property_id"] == "prop-B"
    assert body_a["lead_id"] != body_b["lead_id"]
    assert body_a["duplicate"] is False
    assert body_b["duplicate"] is False

    # Two physical rows now exist — the tenant-scoped unique index lets them
    # coexist; the legacy global index would have rejected the second.
    assert len(fake_db.leads) == 2
    assert {row["org_id"] for row in fake_db.leads} == {ORG_A, ORG_B}


# ---------------------------------------------------------------------------
# B — within ONE org, the dedup contract still holds.
# ---------------------------------------------------------------------------


def test_dedup_still_holds_inside_a_single_org(
    client: TestClient, fake_db: MultiTenantFakeDB
) -> None:
    payload = {
        "name": "Asha",
        "phone": "9876543210",
        "property_name": "Skyline Towers",
    }

    first = client.post("/api/v1/leads", json=payload, headers={"X-Org-Id": ORG_A})
    second = client.post("/api/v1/leads", json=payload, headers={"X-Org-Id": ORG_A})

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["lead_id"] == second.json()["lead_id"]
    assert second.json()["duplicate"] is True

    org_a_rows = [row for row in fake_db.leads if row["org_id"] == ORG_A]
    assert len(org_a_rows) == 1


# ---------------------------------------------------------------------------
# C — strict property match is org-scoped: Org A cannot hit Org B's project.
# ---------------------------------------------------------------------------


def test_org_cannot_link_lead_to_another_orgs_property(
    client: TestClient, fake_db: MultiTenantFakeDB
) -> None:
    # Remove Org A's project so the only "Skyline Towers" left belongs to Org B.
    fake_db.properties = [p for p in fake_db.properties if p["org_id"] != ORG_A]

    response = client.post(
        "/api/v1/leads",
        json={
            "name": "Asha",
            "phone": "9876543210",
            "property_name": "Skyline Towers",
        },
        headers={"X-Org-Id": ORG_A},
    )

    # The matcher MUST refuse rather than silently borrow Org B's project.
    assert response.status_code == 400, response.text
    detail = response.json()["detail"].lower()
    assert "property" in detail and "mismatch" in detail


# ---------------------------------------------------------------------------
# D — retriever defense-in-depth: cross-tenant rows are dropped even if the
#     SQL RPC ever leaked them through.
# ---------------------------------------------------------------------------


def test_retriever_drops_rows_with_a_different_org_id() -> None:
    settings = Settings(
        supabase_url="https://test.supabase.co",
        supabase_service_role_key="fake",
        openai_api_key="fake",
        rag_similarity_threshold=0.3,
        rag_top_k_units=4,
        rag_top_k_chunks=4,
    )

    fake_client = MagicMock()
    # Simulate a misconfigured RPC that leaks an Org-B row into Org A's call.
    fake_client.rpc.return_value.execute.side_effect = [
        MagicMock(data=[
            {
                "id": "u-leak",
                "org_id": ORG_B,
                "project_id": "prop-A",
                "similarity": 0.95,
                "unit_name": "Tower-X / 5",
            }
        ]),
        MagicMock(data=[
            {
                "id": "c-good",
                "org_id": ORG_A,
                "property_id": "prop-A",
                "similarity": 0.91,
                "content": "Price: 1.25 Cr starting.",
            },
            {
                "id": "c-leak",
                "org_id": ORG_B,
                "property_id": "prop-A",
                "similarity": 0.93,
                "content": "Org B confidential pricing.",
            },
        ]),
    ]

    retriever = Retriever(fake_client, settings)
    out = retriever.retrieve(
        query_embedding=[0.0] * 8,
        property_id="prop-A",
        org_id=ORG_A,
    )

    # The leaked unit (Org B) must be filtered out.
    assert out["units"] == []

    # Only the Org-A chunk survives; the Org-B "confidential" chunk is dropped
    # before it ever reaches the LLM prompt.
    assert len(out["chunks"]) == 1
    assert out["chunks"][0]["id"] == "c-good"
    assert out["chunks"][0]["org_id"] == ORG_A


def test_retriever_refuses_to_run_without_org_id() -> None:
    settings = Settings(
        supabase_url="https://test.supabase.co",
        supabase_service_role_key="fake",
        openai_api_key="fake",
    )
    retriever = Retriever(MagicMock(), settings)

    with pytest.raises(ValueError):
        retriever.retrieve(query_embedding=[0.0] * 8, property_id="prop-A", org_id="")
