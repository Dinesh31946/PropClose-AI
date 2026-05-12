from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    lead_id: str = Field(..., description="Lead ID from CRM")
    property_id: str = Field(
        default="",
        description="Listing UUID when known (WhatsApp/dashboard). Empty → resolve from lead row / property_name.",
    )
    interested_in: str | None = Field(default="", description="Preferred configuration")
    message: str = Field(..., description="User message")


class EvidenceItem(BaseModel):
    """One retrieval row that fed the prompt.

        Enterprise isolation layer - mandatory for SaaS scalability.
    Used for observability and channel-level confidence gating.  Every
    field is tenant-scoped via ``org_id`` so one broker's audit trail
    can never accidentally surface another broker's chunk_ids.
    """

    source: str   # "units" | "chunks"
    org_id: str
    property_id: str | None = None
    chunk_id: str | None = None
    similarity: float | None = None


class ChatResponse(BaseModel):
    success: bool
    reply: str
    needs_attention: bool = False
    site_visit_confirmed: bool = False
    top_similarity: float | None = Field(
        default=None,
        description=(
            "Highest similarity score across all retrieved units + chunks for this "
            "request, AFTER tenant scoping but BEFORE the channel-specific gate. "
            "None when retrieval returned nothing."
        ),
    )
    evidence: list[EvidenceItem] = Field(
        default_factory=list,
        description="Per-row retrieval observability: chunk_id, property_id, similarity.",
    )

