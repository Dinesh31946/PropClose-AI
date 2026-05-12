"""Unit tests for routing metadata + urgency heuristics."""

from app.policies.conversation_intent import (
    append_intent_tag,
    emergency_callback_requested,
    format_display_phone,
    human_call_back_requested,
    sanitize_history_for_llm,
    strip_trailing_intent_tag,
)


def test_strip_and_append_intent_roundtrip() -> None:
    raw = "Hello\n\n[INTENT: GENERAL | URGENCY: NORMAL]"
    assert strip_trailing_intent_tag(raw) == "Hello"
    tagged = append_intent_tag("Done", intent="CALL_BACK", urgency="HIGH")
    assert tagged.endswith("[INTENT: CALL_BACK | URGENCY: HIGH]")
    assert strip_trailing_intent_tag(tagged) == "Done"


def test_sanitize_history_removes_trailing_tag() -> None:
    hist = [
        {"role": "assistant", "content": "Hi\n\n[INTENT: GENERAL | URGENCY: NORMAL]"},
        {"role": "user", "content": "Price?"},
    ]
    clean = sanitize_history_for_llm(hist)
    assert clean[0]["content"] == "Hi"
    assert clean[1]["content"] == "Price?"


def test_strip_intent_only_trailing_line() -> None:
    mid = "We mention [INTENT: X | URGENCY: Y] only as an example in the text."
    assert strip_trailing_intent_tag(mid) == mid
    tagged = "Hello\n\n[INTENT: GENERAL | URGENCY: NORMAL]"
    assert strip_trailing_intent_tag(tagged) == "Hello"


def test_emergency_signals() -> None:
    assert emergency_callback_requested("This is urgent — need pricing today")
    assert emergency_callback_requested("I am frustrated, nobody is helping")
    assert not emergency_callback_requested("What is the carpet area?")


def test_format_display_phone() -> None:
    assert format_display_phone("919876543210") == "+919876543210"
    assert format_display_phone("+91 98765 43210") == "+919876543210"
    assert format_display_phone(None) is None


def test_human_call_back_phrases() -> None:
    assert human_call_back_requested("I need a human call back please")
    assert human_call_back_requested("Can a real person call me?")
    assert human_call_back_requested("Talk to human")
    assert not human_call_back_requested("What is the price?")
