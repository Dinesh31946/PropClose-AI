from io import BytesIO
from math import ceil

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pypdf import PdfReader

from app.core.config import Settings
from app.core.tenancy import TenantDep
from app.db.supabase_client import get_supabase_client
from app.rag.embedder import Embedder

router = APIRouter()


def _normalize_price_to_number(price_str: str) -> int:
    if not price_str:
        return 0
    normalized = price_str.lower().replace(",", "").strip()
    multiplier = 1
    if "cr" in normalized or "crore" in normalized:
        multiplier = 10000000
    elif "l" in normalized or "lac" in normalized or "lakh" in normalized:
        multiplier = 100000

    digits = "".join(char for char in normalized if char.isdigit() or char == ".")
    if not digits:
        return 0
    return int(round(float(digits) * multiplier))


def _chunk_text(text: str, chunk_size: int = 1500) -> list[str]:
    if not text:
        return []
    chunk_count = ceil(len(text) / chunk_size)
    return [text[i * chunk_size : (i + 1) * chunk_size] for i in range(chunk_count)]


def _extract_pdf_text(file_bytes: bytes) -> str:
    reader = PdfReader(BytesIO(file_bytes))
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n".join(pages).strip()


@router.post("/ingest")
async def ingest_brochure(
    tenant: TenantDep,
    file: UploadFile = File(...),
    title: str = Form(...),
    location: str = Form(...),
    price: str = Form(""),
) -> dict:
    """Ingest a brochure PDF and split it into tenant-scoped vector chunks.

        Enterprise isolation layer - mandatory for SaaS scalability.
    Both ``properties`` and ``brochure_chunks`` rows are stamped with
    ``tenant.org_id``; the RAG retriever filters on this column at query
    time so a brochure uploaded by Broker A can never surface in
    Broker B's chat answers.
    """
    settings = Settings.load()
    supabase = get_supabase_client()
    embedder = Embedder(settings)
    org_id = tenant.org_id

    if file.content_type not in {"application/pdf", "application/octet-stream"}:
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    extracted_text = _extract_pdf_text(file_bytes)
    if not extracted_text:
        raise HTTPException(
            status_code=400,
            detail="Could not extract brochure text from PDF.",
        )

    normalized_price = _normalize_price_to_number(price)
    project_summary = (
        f"Project: {title}. Location: {location}. Price starting at: {price}. "
        f"Price numeric: {normalized_price}."
    )
    property_embedding = embedder.embed_text(project_summary)

    property_insert = (
        supabase.table("properties")
        .insert(
            {
                "org_id": org_id,
                "name": title,
                "location": location,
                "price": price,
                "price_numeric": normalized_price,
                "description": extracted_text[:2000],
                "ai_summary": project_summary,
                "embedding": property_embedding,
            }
        )
        .execute()
    )
    property_data = property_insert.data or []
    if not property_data:
        raise HTTPException(status_code=500, detail="Failed to insert property record.")
    property_id = property_data[0]["id"]

    chunks = _chunk_text(extracted_text, chunk_size=1500)
    if not chunks:
        raise HTTPException(status_code=500, detail="No chunks were generated from brochure.")

    chunk_embeddings = embedder.embed_texts(chunks)
    if len(chunk_embeddings) != len(chunks):
        raise HTTPException(
            status_code=500,
            detail="Embedding generation mismatch for brochure chunks.",
        )

    chunk_rows = [
        {
            "org_id": org_id,
            "property_id": property_id,
            "content": chunk,
            "embedding": chunk_embeddings[index],
        }
        for index, chunk in enumerate(chunks)
    ]

    if any(not row.get("embedding") for row in chunk_rows):
        raise HTTPException(status_code=500, detail="Missing embeddings for one or more chunks.")

    supabase.table("brochure_chunks").insert(chunk_rows).execute()

    return {
        "success": True,
        "org_id": org_id,
        "property_id": property_id,
        "chunks_inserted": len(chunk_rows),
        "message": "Project and brochure knowledge ingested successfully.",
    }
