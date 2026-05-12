from typing import Any, Dict, List


PRICE_KEYWORDS = {"price", "cost", "budget", "rate", "floor-wise", "floor wise", "exact"}


def has_enough_evidence(units: List[Dict[str, Any]], chunks: List[Dict[str, Any]]) -> bool:
    return bool(units or chunks)


def has_confident_evidence(
    units: List[Dict[str, Any]], chunks: List[Dict[str, Any]], threshold: float
) -> bool:
    # We only trust evidence that has an explicit similarity score above threshold.
    # Missing similarity is treated as untrusted to avoid accidental hallucinations.
    def _is_confident(item: Dict[str, Any]) -> bool:
        score = item.get("similarity")
        return isinstance(score, (int, float)) and float(score) >= threshold

    return any(_is_confident(item) for item in units) or any(_is_confident(item) for item in chunks)


def is_high_risk_price_query(user_message: str) -> bool:
    text = user_message.lower()
    return any(keyword in text for keyword in PRICE_KEYWORDS)


def requires_handoff_for_price_accuracy(user_message: str) -> bool:
    # Floor-wise exact pricing is flagged in planning docs as a critical mislead risk,
    # so we force human handoff instead of allowing the model to estimate.
    text = user_message.lower()
    return "floor-wise" in text or "floor wise" in text or "exact" in text


def should_prioritize_inventory_fallback(user_message: str) -> bool:
    """True when the user is asking for listing economics — use unit_inventory
    even if vector similarity against embeddings is weak (e.g. WhatsApp 0.7 gate).
    """
    text = user_message.lower().strip()
    if not text:
        return False
    price_triggers = (
        "price",
        "pricing",
        "cost",
        "budget",
        "rate",
        "quoted",
        "quote",
        "how much",
        "kitna",
        "daam",
        "rent",
    )
    return any(t in text for t in price_triggers)

