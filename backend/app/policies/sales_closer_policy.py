import zlib


def enforce_sales_closer_policy(reply: str) -> str:
    """Normalize whitespace only.

    Site-visit CTAs belong in ``GroundedGenerator`` rules so they can respect
    chat history (no repetitive boilerplate after every answer).
    """
    normalized = reply.strip()
    if not normalized:
        normalized = "Main aapki help ke liye ready hoon."
    return normalized


_EXPERT_BRIDGE_VARIANTS: tuple[str, ...] = (
    "I'm notifying our project expert right now — they'll reach out with the accurate details shortly.",
    "I'll have a consultant call you with those specific details shortly.",
    "Let me loop in our on-site specialist so you get a precise answer without guesswork.",
    "I'm connecting you with our team for a quick callback — someone will walk you through it.",
    "Verified detail is light on this exact point — I'm not guessing. "
    "I'll have an expert call you shortly with the numbers and policy clarity you need.",
    "Context mein is specific point ka verified slice abhi tight nahi mila — galat assumption nahi lenge. "
    "I'm routing this to our specialist so you get an exact answer on the call.",
    "Ye specifics double-check karne ke liye main abhi expert ko notify kar raha hoon — "
    "woh aapko jald callback karenge with clear details.",
)


def pick_expert_bridge_message(seed: str) -> str:
    """Rotate Expert Bridge copy so repeat visits do not get identical boilerplate."""
    key = seed or "default"
    idx = zlib.adler32(key.encode("utf-8")) % len(_EXPERT_BRIDGE_VARIANTS)
    return _EXPERT_BRIDGE_VARIANTS[idx]


def fallback_no_evidence_response(seed: str = "") -> str:
    """Grounding failed — escalate via broker bridge (no sterile 'no data' phrasing)."""
    return pick_expert_bridge_message(seed)


def pick_whatsapp_low_confidence_message(*, seed: str, display_phone: str | None) -> str:
    """WhatsApp channel: hand off without asking for a number the user is already using."""
    variants_with = (
        "I'm lining up the right specialist for this - I will arrange a call for you on this number ({phone}). "
        "They'll confirm the exact details with you.",
        "I'll arrange a call for you on this number ({phone}) so we don't risk a half-confident answer on WhatsApp. "
        "Our expert will clarify everything on that call.",
        "Got it - I will arrange a call for you on this number ({phone}). Someone from the team will reach you shortly "
        "with precise numbers and policy.",
    )
    variants_without = (
        "I'm connecting you with our specialist - I will arrange a call for you on this WhatsApp number. "
        "No need to share your number again.",
        "I'll arrange a call back on the number you're messaging from. Our expert will follow up with accurate details.",
        "Let me route this to our team for a quick callback on this chat line - you'll get the exact answers there.",
    )
    key = seed or "wa"
    if display_phone:
        basket = variants_with
        idx = zlib.adler32(key.encode("utf-8")) % len(basket)
        return basket[idx].format(phone=display_phone)
    basket = variants_without
    idx = zlib.adler32(key.encode("utf-8")) % len(basket)
    return basket[idx]


def handoff_response_for_exact_pricing() -> str:
    return (
        "Exact floor-wise pricing frequently change hoti rehti hai, "
        "isliye main aapko approximate number nahi dunga. "
        "Main abhi expert broker se exact live price confirm kara deta hoon — "
        "woh aapko detail mein walk-through karenge. [HANDOFF_REQUIRED]"
    )

