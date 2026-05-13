"""Shared helpers for lightweight OpenAI ``chat.completions`` calls."""

from __future__ import annotations

import json
import logging
from typing import Any

from openai import OpenAI

from app.core.config import Settings

logger = logging.getLogger(__name__)


def chat_completion_json_object(
    *,
    settings: Settings,
    messages: list[dict[str, str]],
    temperature: float = 0,
) -> dict[str, Any]:
    """Run chat completion constrained to JSON; return parsed object or ``{{}}`` on failure."""
    client = OpenAI(api_key=settings.openai_api_key)
    response = client.chat.completions.create(
        model=settings.openai_model,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=temperature,
    )
    raw = response.choices[0].message.content or "{}"
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else {}
    except json.JSONDecodeError:
        logger.warning("chat_completion_json_object: invalid JSON from model (%s chars)", len(raw))
        return {}
