"""Specific-Listing-Lock policy.

    Enterprise isolation layer - mandatory for SaaS scalability.

This module implements the "Anti-Distraction Rule" from the product
spec.  When a customer is talking to the AI about Property X (the
property the lead originally enquired about), the AI must NEVER
unilaterally pull data for Property Y -- even if both belong to the
same broker.  Three deterministic detectors back this guarantee:

    1. ``is_broader_search_inquiry(message)``
       Catches "show me other 2BHKs", "any 3BHK in Phase 2",
       "different project", "alternatives", etc.  Hinglish + English.

    2. ``is_affirmative_consent(message)``
       Catches the lead's explicit yes after the redirect prompt:
       "yes", "haan", "sure", "ok", "go ahead", etc.

    3. ``redirect_template(property_name)``
       Builds the canned "I am currently assisting you with X.  Would
       you like me to find similar options?" prompt verbatim, so the
       consent detector can match it back from chat_history.

The three states the chat-service gate moves between:

    LOCKED            (default; retrieve only ``lead.property_id``)
        ↓  user asks about other listings
    AWAITING_CONSENT  (return redirect_template(...) verbatim, no RAG)
        ↓  user replies "yes" / "haan" / "sure"
    UNLOCKED          (retrieve org-wide, scoped to ``org_id`` only)

State is reconstructed from the last 1-2 chat_history rows -- no extra
DB column needed and works correctly even when the worker is restarted
mid-conversation.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Broader-search intent
# ---------------------------------------------------------------------------
# Word boundaries (\b) keep these patterns from over-firing on benign
# phrases like "another floor in this same tower".  Every "broader"
# verb / quantifier MUST be followed by a "broader-noun" before we
# trigger the redirect -- this is what stops "see another floor" from
# being misclassified as a comparison-shopping intent.
_BROADER_NOUN_GROUP = (
    r"(?:project|projects|property|properties|listing|listings|"
    r"building|buildings|tower|towers|society|societies|"
    r"flat|flats|apartment|apartments|unit|units|"
    r"option|options|home|homes|villa|villas|plot|plots|"
    r"\d+\s*bhks?)"
)

_BROADER_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 1. "other / another / different / alternative" + broader-noun.
    re.compile(
        rf"\b(other|another|different|alternative|alternate|some other|any other)\b"
        rf"\s+{_BROADER_NOUN_GROUP}\b",
        re.IGNORECASE,
    ),
    # 2. "show / suggest / find me other/different/similar X".
    #    The X must be a broader-noun -- this is what kills the
    #    "see another floor in the same tower" false positive.
    re.compile(
        rf"\b(show|suggest|find|recommend|browse|list|see|share)\b\s+(me\s+)?"
        rf"(other|another|different|alternative|alternate|similar)\s+{_BROADER_NOUN_GROUP}\b",
        re.IGNORECASE,
    ),
    # 3. "any 2BHK / some 3BHK / got 1BHK" -- discovery question.
    re.compile(
        r"\b(any|some|got|have)\b\s+(other\s+)?\d+\s*bhks?\b",
        re.IGNORECASE,
    ),
    # 4. Hinglish: aur / doosra / koi + noun within 30 chars.
    re.compile(
        rf"\b(doosra|dusra|doosri|dusri|aur|koi|kuch)\b.{{0,30}}\b{_BROADER_NOUN_GROUP}\b",
        re.IGNORECASE,
    ),
    # 5. "what about / how about" + non-attribute follow-up.  The
    #    negative lookahead refuses follow-ups that are clearly
    #    property-locked attributes ("what about THE PRICE / area / ...").
    re.compile(
        r"\b(what about|how about)\b\s+"
        r"(?!the\s+(price|prices|cost|carpet|area|amenities|amenity|"
        r"status|specs|spec|location)\b)",
        re.IGNORECASE,
    ),
    # 6. "your inventory / listings / portfolio" -- meta-catalog question.
    re.compile(
        r"\byour\s+(inventory|listings|portfolio|projects|properties|catalogue|catalog)\b",
        re.IGNORECASE,
    ),
    # 7. "tell me about <Capitalised proper noun>" -- typical signal that
    #    the lead has typed a DIFFERENT project name.  The Capitalised-
    #    initial check is intentional: "tell me about the carpet area"
    #    starts with lowercase 'the', so it won't match.
    re.compile(r"(?i:\btell me about\b)\s+([A-Z][a-zA-Z]+)"),
)


def is_broader_search_inquiry(message: str | None) -> bool:
    """Return True when the user is asking to see a *different* listing.

    Conservative: only fires when the keyword + noun together suggest
    a comparison-shopping intent.  Pure follow-ups like "what is the
    price?" or "is parking available?" never match.
    """
    if not message:
        return False
    text = str(message).strip()
    if not text:
        return False
    return any(p.search(text) for p in _BROADER_PATTERNS)


# ---------------------------------------------------------------------------
# Affirmative consent
# ---------------------------------------------------------------------------
_AFFIRMATIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Whole-message yes -- the most common form on WhatsApp.
    re.compile(
        r"^\s*(yes|yeah|yep|yup|sure|ok|okay|please|go ahead|"
        r"haan|haan ji|han|ji|theek hai|chalo|sahi|kar do|do it|"
        r"bilkul|bilkul kar|kar dijiye|dikhao|dikhaiye|"
        r"sounds good|that works|absolutely|of course|definitely)\b",
        re.IGNORECASE,
    ),
    # Polite "yes please" anywhere in a short message.
    re.compile(
        r"\b(yes please|please do|please show|kar do|dikha do|share kar do)\b",
        re.IGNORECASE,
    ),
)


def is_affirmative_consent(message: str | None) -> bool:
    """Return True when the user has explicitly agreed to widen the search."""
    if not message:
        return False
    text = str(message).strip()
    if not text:
        return False
    if len(text) > 200:
        # Long messages are likely follow-up questions, not bare consent.
        return False
    return any(p.search(text) for p in _AFFIRMATIVE_PATTERNS)


# ---------------------------------------------------------------------------
# Redirect template
# ---------------------------------------------------------------------------
# We keep the EXACT wording from the product spec so a customer service
# review can grep for it across logs.  The marker tag at the end lets
# the consent path identify "the previous assistant turn was the
# redirect" without parsing free-form copy.
REDIRECT_MARKER = "[LISTING_LOCK_REDIRECT]"


def redirect_template(property_name: str) -> str:
    """Canned "I'm assisting you with X" prompt + machine-readable marker.

    The marker is stripped by the sales-closer policy / WhatsApp send
    path before the customer ever sees it; it survives in chat_history
    so the next turn can detect "we are awaiting consent".
    """
    name = (property_name or "this property").strip()
    return (
        f"I am currently assisting you with {name}. "
        "Would you like me to find other similar options from my "
        f"inventory instead? {REDIRECT_MARKER}"
    )


def was_last_turn_a_redirect(chat_history: Iterable[dict[str, Any]]) -> bool:
    """Return True iff the most recent assistant message asked the
    "do you want me to widen the search?" question.

        Enterprise isolation layer - mandatory for SaaS scalability.
    Looks at the chat_history slice the caller already loaded for the
    LLM prompt -- no additional DB roundtrip.  History rows are
    dicts with ``role`` and ``content`` (oldest→newest, matching
    ``ChatRepository.get_recent_history``).
    """
    history = list(chat_history or [])
    for item in reversed(history):
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role != "assistant":
            continue
        content = str(item.get("content") or "")
        return REDIRECT_MARKER in content
    return False


def strip_redirect_marker(reply: str) -> str:
    """Remove the internal marker before the reply hits the customer."""
    if not reply:
        return ""
    return reply.replace(REDIRECT_MARKER, "").strip()


__all__ = [
    "REDIRECT_MARKER",
    "is_affirmative_consent",
    "is_broader_search_inquiry",
    "redirect_template",
    "strip_redirect_marker",
    "was_last_turn_a_redirect",
]
