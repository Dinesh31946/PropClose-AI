"""Progressive profiling: extract budget, timeline, purpose, and requirement from chat."""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.policies.conversation_intent import sanitize_history_for_llm
from app.utils.llm_utils import chat_completion_json_object

# Highest-priority gaps to fill first when asking discovery questions (left → right).
_PROFILE_FIELD_QUESTION_PRIORITY: tuple[str, ...] = (
    "budget",
    "timeline",
    "purpose",
    "requirement",
)

_ALLOWED_EXTRACTION_KEYS = frozenset(_PROFILE_FIELD_QUESTION_PRIORITY)

_EXTRACTION_SYSTEM = """You extract real-estate buyer signals from the user's *latest message* plus short prior chat turns.

Return ONE JSON object with ONLY these optional string fields (omit a key entirely if unknown or not stated in THIS turn — do NOT guess):
- "budget": e.g. "1 Cr", "80 lakhs", "around 90L"
- "timeline": e.g. "ready to move", "within 6 months", "next year"
- "purpose": e.g. "investment", "self-use", "rental yield"
- "requirement": e.g. "1 BHK", "2 BHK", "shop"

Rules:
1. Prefer what the USER clearly says in their latest message; use history only to resolve obvious references ("that budget").
2. If the user mentioned nothing new for any field, return {} (empty JSON object).
3. Values MUST be concise plain text; use null only if your API forces the key — we prefer omitting the key."""


class ProfilingService:
    """LLM-backed extraction plus deterministic merge / next-question hints."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.load()

    def extract_signals(
        self,
        *,
        message: str,
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return profiling fields inferred from THIS turn using the LLM; ``{{}}`` if nothing new."""

        sanitized = sanitize_history_for_llm(history)
        trimmed_message = (message or "").strip()

        messages: list[dict[str, str]] = [{"role": "system", "content": _EXTRACTION_SYSTEM}]
        for item in sanitized:
            role = item.get("role")
            content = item.get("content")
            if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
                messages.append({"role": str(role), "content": content.strip()})

        probe = (
            trimmed_message + "\n\n"
            "**Task:** JSON only — fields the user stated in their **latest message** "
            '(see system rules); empty {{}} if nothing new.'
        ).strip()

        parsed = chat_completion_json_object(
            settings=self.settings,
            messages=messages + [{"role": "user", "content": probe}],
            temperature=0,
        )
        return self._validate_extraction(parsed)

    def merge_into_profile(self, profiling_data: dict[str, Any] | None, extracted: dict[str, Any]) -> dict[str, Any]:
        """Combine DB ``profiling_data`` with a freshly extracted patch (non-empty values win)."""

        base = dict(profiling_data or {})
        for key in _ALLOWED_EXTRACTION_KEYS:
            if key not in extracted:
                continue
            val = extracted.get(key)
            if self._is_present(val):
                base[key] = val
        return base

    def select_next_missing_key(self, profiling_data: dict[str, Any] | None) -> str | None:
        """Return the highest-priority profiling key that is still missing from ``profiling_data``."""

        profile = profiling_data or {}
        for key in _PROFILE_FIELD_QUESTION_PRIORITY:
            if not self._is_present(profile.get(key)):
                return key
        return None

    # --- internals ---

    @staticmethod
    def _validate_extraction(raw: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in _ALLOWED_EXTRACTION_KEYS:
            if key not in raw:
                continue
            val = raw[key]
            if isinstance(val, str):
                trimmed = val.strip()
                if trimmed:
                    out[key] = trimmed
            elif val is None:
                continue
            else:
                text = str(val).strip()
                if text:
                    out[key] = text
        return out

    @staticmethod
    def _is_present(value: Any) -> bool:
        if value is None:
            return False
        text = value if isinstance(value, str) else str(value)
        return bool(text.strip())
