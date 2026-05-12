"""Emergency / urgency detection and INTENT metadata tags for routed replies."""

from __future__ import annotations

import re


# Line appended by the orchestrator (never type free-form brackets in prose).
_INTENT_LINE_RE = re.compile(
    r"\n*\[INTENT\s*:\s*([^\]|]+)\s*\|\s*URGENCY\s*:\s*([^\]]+)\]\s*$",
    re.IGNORECASE | re.DOTALL,
)


def strip_trailing_intent_tag(body: str) -> str:
    """Remove only a **trailing** orchestrator line matching ``[INTENT: … | URGENCY: …]``.

    The pattern is anchored at the end of the string (``$``), so inline brackets
    inside the customer-visible body are left unchanged.
    """
    if not body:
        return ""
    return _INTENT_LINE_RE.sub("", body.rstrip()).rstrip()


def append_intent_tag(body: str, *, intent: str, urgency: str) -> str:
    """Append deterministic metadata footer for workflows (hidden from customers via strip before send)."""
    base = strip_trailing_intent_tag(body.strip())
    if not base:
        base = "Thanks — our team will help you shortly."
    iu = intent.strip().upper()
    uu = urgency.strip().upper()
    return f"{base}\n\n[INTENT: {iu} | URGENCY: {uu}]"


def sanitize_history_for_llm(history: list[dict]) -> list[dict]:
    """Drop intent footers from past assistant turns so the model stays focused."""
    out: list[dict] = []
    for item in history:
        role = item.get("role")
        content = item.get("content")
        if role not in {"user", "assistant"} or content is None:
            continue
        text = str(content)
        if role == "assistant":
            text = strip_trailing_intent_tag(text)
        out.append({"role": role, "content": text})
    return out


# ---- Emergency / high-urgency callback -------------------------------------------------

_URGENT_RE = re.compile(
    r"(?:\b(?:immediately|urgent|urgently|asap|emergency|right\s+now)\b|"
    r"\bcall\s+me\s+now\b|\bcall\s+now\b|\bphone\s+me\s+now\b|\bring\s+me\b|"
    r"turant|jaldi\s+call|abhi\s+call|kab\s+tak\s+call|\bbroker\s+abhi\b)",
    re.IGNORECASE,
)

_FRUSTRATION_RE = re.compile(
    r"(?:\bfrustrated\b|\bfed\s+up\b|\bwaste\s+of\s+time\b|\bpathetic\b|\bdisgusting\b|"
    r"\bworst\b.*\bservice\b|\bhad\s+enough\b|bakwas|faltu|pareshan\b)",
    re.IGNORECASE,
)


def emergency_callback_requested(user_message: str) -> bool:
    """Elevated urgency: customer needs broker contact soon (routing + NOTIFY).

    Signals: urgency language, NOW callbacks, sustained frustration wording.
    """
    if not user_message or not user_message.strip():
        return False
    s = user_message.strip()
    if _URGENT_RE.search(s):
        return True
    if _FRUSTRATION_RE.search(s):
        return True
    return False


# ---- Display phone (WhatsApp / CRM) ----------------------------------------------------

def format_display_phone(phone: object | None) -> str | None:
    """Normalize stored lead phone to a display form like ``+9198XXXXXXX`` for prompts."""
    if phone is None:
        return None
    raw = str(phone).strip()
    if not raw:
        return None
    digits = "".join(c for c in raw if c.isdigit())
    if not digits:
        return None
    return f"+{digits}"


# ---- Human callback (broker / real person) --------------------------------------------

_HUMAN_CALLBACK_RE = re.compile(
    r"(?:\bhuman\b.*\b(?:call|callback|phone|contact|speak)|"
    r"\b(?:call|callback)\b.*\bhuman\b|"
    r"\breal\s+(?:person|human|agent|someone)\b|"
    r"\btalk\s+to\s+(?:a\s+)?(?:human|person|agent|broker|someone\s+real)\b|"
    r"\bspeak\s+to\s+(?:a\s+)?(?:human|person|agent)\b|"
    r"\bmanager\s+(?:call|se\s+baat)|"
    r"\b(?:insaan|aadmi)\s+se\s+(?:baat|call)|"
    r"\bcall\s+back\s+from\s+(?:a\s+)?(?:human|person|agent))",
    re.IGNORECASE,
)


def human_call_back_requested(user_message: str) -> bool:
    """True when the lead explicitly wants a real human to call or speak with them."""
    if not user_message or not user_message.strip():
        return False
    return bool(_HUMAN_CALLBACK_RE.search(user_message.strip()))
