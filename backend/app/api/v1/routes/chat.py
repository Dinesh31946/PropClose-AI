from fastapi import APIRouter, HTTPException

from app.core.tenancy import TenantDep
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.chat_service import ChatService

router = APIRouter()
service = ChatService()


@router.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest, tenant: TenantDep) -> ChatResponse:
    """Tenant-scoped chat endpoint.

        Enterprise isolation layer - mandatory for SaaS scalability.
    The ChatService instance is shared across all tenants for memory
    efficiency, but ``tenant.org_id`` is threaded through every read/write
    so a request from Org A can never observe Org B's leads, properties,
    chat history, or RAG evidence.
    """
    try:
        return service.handle_chat(payload, org_id=tenant.org_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
