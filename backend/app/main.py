from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.routes.chat import router as chat_router
from app.api.v1.routes.health import router as health_router
from app.api.v1.routes.ingest import router as ingest_router
from app.api.v1.routes.inventory import router as inventory_router
from app.api.v1.routes.leads import router as leads_router
from app.api.v1.routes.webhook import router as webhook_router
from app.core.logging import configure_logging


configure_logging()

app = FastAPI(
    title="PropClose AI Backend",
    description="Hallucination-intolerant constrained RAG backend for sales closer workflows.",
    version="0.1.0",
)

# CORS: Next.js frontend runs on :3000 and calls FastAPI on :8000.
# Agar CORS allow nahi hua to browser "TypeError: Failed to fetch" dikhata hai.
allowed_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router, prefix="/api/v1", tags=["health"])
app.include_router(chat_router, prefix="/api/v1", tags=["chat"])
app.include_router(leads_router, prefix="/api/v1", tags=["leads"])
app.include_router(inventory_router, prefix="/api/v1", tags=["inventory"])
app.include_router(ingest_router, prefix="/api/v1", tags=["ingest"])
app.include_router(webhook_router, prefix="/api/v1", tags=["webhook"])

