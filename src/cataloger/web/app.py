"""FastAPI web application for Cataloger."""

from __future__ import annotations

import asyncio
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from ..core.catalog import generate_csv_bytes
from ..core.fetcher import BookFetcher
from ..core.images import create_combined_zip, create_image_zip
from ..core.models import BookData

log = structlog.get_logger()

STATIC_DIR = Path(__file__).parent / "static"
SESSION_TTL = 1800  # 30 minutes


@dataclass
class Session:
    books: list[BookData]
    location: str
    created_at: float = field(default_factory=time.time)


# In-memory session store
sessions: dict[str, Session] = {}


def _clean_expired() -> None:
    now = time.time()
    expired = [sid for sid, s in sessions.items() if now - s.created_at > SESSION_TTL]
    for sid in expired:
        sessions.pop(sid, None)


app = FastAPI(title="Cataloger")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text()


@app.post("/api/lookup")
async def lookup(request: Request):
    body = await request.json()
    raw_isbns: list[str] = body.get("isbns", [])
    location: str = body.get("location", "").strip()

    if not location:
        return JSONResponse({"error": "Location name is required."}, status_code=400)

    # Clean and deduplicate ISBNs
    isbns = []
    for raw in raw_isbns:
        cleaned = raw.strip().replace("-", "")
        if cleaned and cleaned not in isbns:
            isbns.append(cleaned)

    if not isbns:
        return JSONResponse({"error": "No valid ISBNs provided."}, status_code=400)

    if len(isbns) > 100:
        return JSONResponse(
            {"error": "Maximum 100 ISBNs per request."}, status_code=400
        )

    tmp_dir = Path(tempfile.mkdtemp(prefix="cataloger_"))
    image_dir = tmp_dir / "images"

    fetcher = BookFetcher(image_dir=image_dir)
    books = await fetcher.fetch_all(isbns)

    _clean_expired()
    session_id = uuid.uuid4().hex[:12]
    sessions[session_id] = Session(books=books, location=location)

    found = sum(1 for b in books if b.title)
    images_found = sum(1 for b in books if b.image_path)

    return {
        "session_id": session_id,
        "summary": {
            "total": len(isbns),
            "found": found,
            "missing": len(isbns) - found,
            "images": images_found,
        },
        "books": [
            {
                "isbn": b.isbn,
                "title": b.title,
                "author": b.author,
                "description": b.description[:200] if b.description else "",
                "page_count": b.page_count,
                "price": b.price,
                "image_url": b.image_url,
                "image_source": b.image_source,
                "errors": b.errors,
            }
            for b in books
        ],
    }


def _get_session(session_id: str) -> Session | None:
    session = sessions.get(session_id)
    if not session:
        return None
    if time.time() - session.created_at > SESSION_TTL:
        sessions.pop(session_id, None)
        return None
    return session


@app.get("/api/download/csv")
async def download_csv(session: str):
    s = _get_session(session)
    if not s:
        return JSONResponse({"error": "Session not found or expired."}, status_code=404)
    csv_bytes = generate_csv_bytes(s.books, s.location)
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="catalog.csv"'},
    )


@app.get("/api/download/images")
async def download_images(session: str):
    s = _get_session(session)
    if not s:
        return JSONResponse({"error": "Session not found or expired."}, status_code=404)
    zip_bytes = create_image_zip(s.books)
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="images.zip"'},
    )


@app.get("/api/download/all")
async def download_all(session: str):
    s = _get_session(session)
    if not s:
        return JSONResponse({"error": "Session not found or expired."}, status_code=404)
    csv_bytes = generate_csv_bytes(s.books, s.location)
    zip_bytes = create_combined_zip(csv_bytes, s.books)
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="cataloger.zip"'},
    )


def main():
    uvicorn.run(
        "cataloger.web.app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
