"""Tests for the Specific-Listing-Lock policy.

    Enterprise isolation layer - mandatory for SaaS scalability.

These tests prove four production-grade contracts:

    1. The deterministic detectors in ``policies/listing_scope`` catch
       broader-search inquiries and explicit consent without false
       positives on benign price / area questions.

    2. ``ChatService.handle_chat`` retrieves with ``property_id`` set
       (LOCKED) by default, returns the canned redirect on broader
       intent (AWAITING_CONSENT), and only retrieves org-wide AFTER
       explicit yes from the lead (UNLOCKED).

    3. The retriever passes ``match_property_id`` to the SQL RPCs so
       the ANN scan stops at the property boundary.

    4. The Python defense-in-depth filter still drops cross-property
       rows even if the RPC lies (legacy migration / bug).

Run from the repo root:

    .\\backend\\venv\\Scripts\\python.exe -m pytest backend/tests/test_listing_scope.py -v
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.core.config import Settings
from app.policies.listing_scope import (
    REDIRECT_MARKER,
    is_affirmative_consent,
    is_broader_search_inquiry,
    redirect_template,
    strip_redirect_marker,
    was_last_turn_a_redirect,
)
from app.rag.retriever import Retriever
from app.schemas.chat import ChatRequest
from app.services.chat_service import ChatService

from tests.helpers_chat_service import attach_chat_service_profiling_stub

ORG_ID = "11111111-1111-1111-1111-111111111111"
PROPERTY_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_PROPERTY_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


# =========================================================================
# Detector unit tests
# =========================================================================


@pytest.mark.parametrize(
    "message, expected",
    [
        ("What is the price?", True),
        ("Yahan ki pricing kitni hai?", True),
        ("Is parking included?", False),
        ("Possession kab hai?", False),
    ],
)
def test_inventory_fallback_detector_for_price_intent(message: str, expected: bool) -> None:
    from app.rag.validators import should_prioritize_inventory_fallback

    assert should_prioritize_inventory_fallback(message) is expected


@pytest.mark.parametrize(
    "message",
    [
        "show me other 2BHKs",
        "do you have any 3BHK in another building?",
        "what about Phase 2?",
        "Suggest me alternative projects",
        "any other property in your inventory?",
        "koi aur project hai aapke paas?",
        "Doosra society dikhaiye",
        "Aur kya options hain?",
        "Tell me about Skyline Heights",
    ],
)
def test_broader_search_intent_is_detected(message: str) -> None:
    assert is_broader_search_inquiry(message), message


@pytest.mark.parametrize(
    "message",
    [
        # Standard property-locked questions — must NOT trigger.
        "What is the price of the 2BHK?",
        "Is parking available?",
        "Tell me about the carpet area",
        "Yahan ka price kya hai?",
        "Kya is project mein gym hai?",
        "Possession date kya hai?",
        # Edge: 'another floor' in same project should NOT redirect.
        "Can I see another floor in the same tower?",
    ],
)
def test_focused_questions_do_not_trigger_broader_search(message: str) -> None:
    assert not is_broader_search_inquiry(message), message


@pytest.mark.parametrize(
    "message", ["yes", "yes please", "Sure!", "haan ji", "Theek hai", "OK", "Bilkul kar do", "Go ahead"]
)
def test_affirmative_consent_is_detected(message: str) -> None:
    assert is_affirmative_consent(message), message


@pytest.mark.parametrize(
    "message",
    [
        "no",
        "not now",
        "later",
        "what about price?",
        "tell me carpet area",
        "x" * 500,  # too long to be bare consent
    ],
)
def test_non_consent_messages_are_not_classified_as_yes(message: str) -> None:
    assert not is_affirmative_consent(message), message


def test_redirect_template_includes_property_name_and_marker() -> None:
    text = redirect_template("Skyline Towers")
    assert "Skyline Towers" in text
    assert REDIRECT_MARKER in text
    assert "similar options" in text.lower()


def test_was_last_turn_a_redirect_handles_history_order() -> None:
    history = [
        {"role": "user", "content": "What's the price?"},
        {"role": "assistant", "content": "The price is..."},
        {"role": "user", "content": "Show me other towers"},
        {
            "role": "assistant",
            "content": redirect_template("Skyline Towers"),
        },
    ]
    assert was_last_turn_a_redirect(history)


def test_was_last_turn_a_redirect_ignores_older_redirects_after_normal_reply() -> None:
    history = [
        {"role": "user", "content": "Show other towers"},
        {"role": "assistant", "content": redirect_template("Skyline Towers")},
        {"role": "user", "content": "Actually never mind, what's the carpet area?"},
        {"role": "assistant", "content": "Carpet area is 1245 sqft."},
    ]
    assert not was_last_turn_a_redirect(history)


def test_strip_redirect_marker_removes_it_cleanly() -> None:
    raw = redirect_template("Skyline Towers")
    cleaned = strip_redirect_marker(raw)
    assert REDIRECT_MARKER not in cleaned
    assert "Skyline Towers" in cleaned


# =========================================================================
# Retriever: SQL RPCs receive match_property_id
# =========================================================================


def _build_retriever_with_capture(
    units_data: list[dict[str, Any]] | None = None,
    chunks_data: list[dict[str, Any]] | None = None,
) -> tuple[Retriever, MagicMock]:
    """Stand up a retriever wired to a MagicMock supabase client.
    Returns (retriever, captured_rpc_calls)."""
    fake_client = MagicMock()
    rpc_calls: list[tuple[str, dict[str, Any]]] = []

    def rpc(name: str, args: dict[str, Any]):
        rpc_calls.append((name, dict(args)))
        if name == "match_units":
            return MagicMock(execute=lambda: MagicMock(data=units_data or []))
        if name == "match_chunks":
            return MagicMock(execute=lambda: MagicMock(data=chunks_data or []))
        return MagicMock(execute=lambda: MagicMock(data=[]))

    fake_client.rpc.side_effect = rpc
    settings = Settings(
        supabase_url="x",
        supabase_service_role_key="x",
        openai_api_key="x",
        rag_similarity_threshold=0.3,
        rag_top_k_units=4,
        rag_top_k_chunks=4,
    )
    retriever = Retriever(fake_client, settings)
    retriever._captured_rpc_calls = rpc_calls   # type: ignore[attr-defined]
    return retriever, rpc_calls


def test_retriever_passes_match_property_id_when_locked() -> None:
    retriever, calls = _build_retriever_with_capture()
    retriever.retrieve([0.0] * 8, PROPERTY_ID, org_id=ORG_ID)

    units_call = next(args for name, args in calls if name == "match_units")
    chunks_call = next(args for name, args in calls if name == "match_chunks")

    assert units_call["match_org_id"] == ORG_ID
    assert units_call["match_property_id"] == PROPERTY_ID
    assert chunks_call["match_org_id"] == ORG_ID
    assert chunks_call["match_property_id"] == PROPERTY_ID


def test_retriever_omits_match_property_id_when_unlocked() -> None:
    """Broader-search consent path: property_id=None must NOT pin
    the RPC; org-wide search runs (still tenant-scoped)."""
    retriever, calls = _build_retriever_with_capture()
    retriever.retrieve([0.0] * 8, None, org_id=ORG_ID)

    units_call = next(args for name, args in calls if name == "match_units")
    chunks_call = next(args for name, args in calls if name == "match_chunks")

    assert units_call["match_org_id"] == ORG_ID
    assert "match_property_id" not in units_call
    assert chunks_call["match_org_id"] == ORG_ID
    assert "match_property_id" not in chunks_call


def test_retriever_python_defense_drops_cross_property_rows() -> None:
    """Even if the RPC lied and returned a sibling project's row,
    the Python defense-in-depth filter must drop it."""
    units_data = [
        {"id": "u-target", "org_id": ORG_ID, "project_id": PROPERTY_ID, "similarity": 0.9},
        # Sneaky: same org, DIFFERENT property -- must be dropped.
        {"id": "u-leak", "org_id": ORG_ID, "project_id": OTHER_PROPERTY_ID, "similarity": 0.95},
    ]
    chunks_data = [
        {"id": "c-target", "org_id": ORG_ID, "property_id": PROPERTY_ID, "similarity": 0.85},
        {"id": "c-leak", "org_id": ORG_ID, "property_id": OTHER_PROPERTY_ID, "similarity": 0.99},
    ]
    retriever, _ = _build_retriever_with_capture(units_data, chunks_data)

    results = retriever.retrieve([0.0] * 8, PROPERTY_ID, org_id=ORG_ID)
    unit_ids = {u["id"] for u in results["units"]}
    chunk_ids = {c["id"] for c in results["chunks"]}

    assert "u-target" in unit_ids
    assert "c-target" in chunk_ids
    assert "u-leak" not in unit_ids, "cross-property unit leaked through!"
    assert "c-leak" not in chunk_ids, "cross-property chunk leaked through!"


# =========================================================================
# ChatService: 3-state lock-gate
# =========================================================================


def _build_chat_service(
    *,
    history: list[dict[str, Any]] | None = None,
) -> tuple[ChatService, MagicMock]:
    """Compose a ChatService with all dependencies mocked.

    Returns (service, retriever_mock) so tests can inspect what
    property_id the retriever was asked for.
    """
    service = ChatService.__new__(ChatService)
    service.settings = Settings(
        supabase_url="x",
        supabase_service_role_key="x",
        openai_api_key="x",
        rag_similarity_threshold=0.3,
    )

    service.repo = MagicMock()
    service.repo.get_lead.return_value = {"id": "lead-x", "name": "Asha"}
    service.repo.get_property.return_value = {"id": PROPERTY_ID, "name": "Skyline Towers"}
    service.repo.get_recent_history.return_value = history or []

    service.embedder = MagicMock()
    service.embedder.embed_query.return_value = [0.0] * 8

    service.retriever = MagicMock()
    service.retriever.retrieve.return_value = {
        "units": [
            {
                "id": "u-1",
                "org_id": ORG_ID,
                "project_id": PROPERTY_ID,
                "similarity": 0.92,
                "unit_name": "Tower-A 401",
                "configuration": "2BHK",
                "carpet_area": "1245 sqft",
                "price": "1.85 Cr",
                "status": "Available",
            }
        ],
        "chunks": [],
    }

    service.generator = MagicMock()
    service.generator.generate.return_value = "The price is 1.85 Cr."

    attach_chat_service_profiling_stub(service)

    return service, service.retriever


def test_locked_state_retrieves_only_lead_property() -> None:
    """Default state: a normal price question must hit retriever
    with the lead's property_id (LOCKED)."""
    service, retriever = _build_chat_service()
    request = ChatRequest(
        lead_id="lead-x",
        property_id=PROPERTY_ID,
        message="What is the price?",
    )
    response = service.handle_chat(request, org_id=ORG_ID)

    retriever.retrieve.assert_called_once()
    args = retriever.retrieve.call_args
    _embedding, called_property_id = args.args
    assert called_property_id == PROPERTY_ID
    assert args.kwargs.get("org_id") == ORG_ID
    assert response.success is True


def test_broader_intent_returns_redirect_without_running_rag() -> None:
    """AWAITING_CONSENT: user asks about other listings without prior
    consent -> AI returns canned redirect, retriever NEVER called."""
    service, retriever = _build_chat_service(history=[])
    request = ChatRequest(
        lead_id="lead-x",
        property_id=PROPERTY_ID,
        message="show me other 2BHKs in your inventory",
    )
    response = service.handle_chat(request, org_id=ORG_ID)

    retriever.retrieve.assert_not_called()
    service.generator.generate.assert_not_called()
    assert response.success is True
    assert "Skyline Towers" in response.reply
    assert "similar options" in response.reply.lower()
    # The marker is stripped from the visible reply.
    assert REDIRECT_MARKER not in response.reply
    # But the marker IS in the row that was saved into chat_history.
    save_args = service.repo.save_messages.call_args.args
    assert REDIRECT_MARKER in save_args[3]


def test_explicit_consent_unlocks_org_wide_search() -> None:
    """UNLOCKED: after the redirect prompt, the user says 'yes' ->
    retriever is called with property_id=None (org-wide)."""
    history = [
        {"role": "user", "content": "show me other 2BHKs"},
        {
            "role": "assistant",
            "content": redirect_template("Skyline Towers"),
        },
    ]
    service, retriever = _build_chat_service(history=history)
    request = ChatRequest(
        lead_id="lead-x",
        property_id=PROPERTY_ID,
        message="yes please",
    )
    response = service.handle_chat(request, org_id=ORG_ID)

    retriever.retrieve.assert_called_once()
    _embedding, called_property_id = retriever.retrieve.call_args.args
    assert called_property_id is None, "broader search must drop property_id"
    assert response.success is True


def test_consent_without_prior_redirect_is_ignored() -> None:
    """A bare 'yes' with NO redirect in history must remain LOCKED."""
    history = [
        {"role": "user", "content": "What's the price?"},
        {"role": "assistant", "content": "The price is 1.85 Cr."},
    ]
    service, retriever = _build_chat_service(history=history)
    request = ChatRequest(
        lead_id="lead-x",
        property_id=PROPERTY_ID,
        message="yes",
    )
    service.handle_chat(request, org_id=ORG_ID)

    retriever.retrieve.assert_called_once()
    _embedding, called_property_id = retriever.retrieve.call_args.args
    assert called_property_id == PROPERTY_ID, "must stay locked without prior redirect"


def test_locked_state_threads_listing_locked_into_generator() -> None:
    service, _retriever = _build_chat_service()
    request = ChatRequest(
        lead_id="lead-x", property_id=PROPERTY_ID, message="What is the price?"
    )
    service.handle_chat(request, org_id=ORG_ID)

    service.generator.generate.assert_called_once()
    kwargs = service.generator.generate.call_args.kwargs
    assert kwargs["listing_locked"] is True
    assert kwargs["property_name"] == "Skyline Towers"


def test_unlocked_state_threads_listing_locked_false_into_generator() -> None:
    history = [
        {"role": "user", "content": "show other towers"},
        {"role": "assistant", "content": redirect_template("Skyline Towers")},
    ]
    service, _retriever = _build_chat_service(history=history)
    request = ChatRequest(
        lead_id="lead-x", property_id=PROPERTY_ID, message="yes"
    )
    service.handle_chat(request, org_id=ORG_ID)

    service.generator.generate.assert_called_once()
    kwargs = service.generator.generate.call_args.kwargs
    assert kwargs["listing_locked"] is False


@pytest.mark.parametrize(
    "inventory_cfg,hint,expected",
    [
        ("GF Shop facing road", "Shop enquiry", True),
        ("Ground Floor Retail", "Retail space", True),
        ("Retail Office unit", "Retail", True),
        ("2 BHK Flat tower A", "2 BHK", True),
        ("2 BHK Flat tower A", "Shop", False),
        ("Corporate Office wing", "Office", True),
        ("", "Flat", False),
    ],
)
def test_row_matches_configuration_filter_product_and_bhk(
    inventory_cfg: str, hint: str, expected: bool
) -> None:
    from app.services.ingestion_service import row_matches_configuration_filter

    assert row_matches_configuration_filter(inventory_cfg, hint) is expected
