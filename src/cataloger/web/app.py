"""FastAPI web application for Cataloger."""

from __future__ import annotations

import os
import tempfile
from collections import defaultdict
from datetime import datetime, timezone

from dotenv import load_dotenv
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from guard.middleware import SecurityMiddleware
from guard.models import SecurityConfig

from ..core.cache import BookCache
from ..core.catalog import generate_csv_bytes
from ..core.fetcher import BookFetcher
from ..core.images import create_combined_zip, create_image_zip
from ..core.models import BookData

load_dotenv()

log = structlog.get_logger()

STATIC_DIR = Path(__file__).parent / "static"
SESSION_TTL = 1800
MAX_SESSIONS = 500
MAX_BODY_BYTES = 50_000

RATE_LIMIT = int(os.environ.get("RATE_LIMIT", "10"))
RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", "60"))


@dataclass
class Session:
    books: list[BookData]
    location: str
    created_at: float = field(default_factory=time.time)


sessions: dict[str, Session] = {}
_rate_log: dict[str, list[float]] = defaultdict(list)
book_cache = BookCache()


def _clean_expired() -> None:
    now = time.time()
    expired = [sid for sid, s in sessions.items() if now - s.created_at > SESSION_TTL]
    for sid in expired:
        sessions.pop(sid, None)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _is_rate_limited(ip: str) -> bool:
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW
    _rate_log[ip] = [t for t in _rate_log[ip] if t > window_start]
    return len(_rate_log[ip]) >= RATE_LIMIT


def _record_request(ip: str) -> None:
    _rate_log[ip].append(time.time())


app = FastAPI(title="Cataloger", docs_url=None, redoc_url=None)

security_config = SecurityConfig(
    enable_redis=False,
    auto_ban_threshold=5,
    rate_limit=100,
    custom_log_file="security.log",
    blocked_user_agents=["curl", "wget", "python-requests", "Go-http-client", "Nuclei", "zgrab"],
)
app.add_middleware(SecurityMiddleware, config=security_config)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "0.1.0",
        "environment": os.environ.get("ENV", "dev"),
        "sessions_active": len(sessions),
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text()


@app.post("/api/lookup")
async def lookup(request: Request):
    ip = _client_ip(request)
    if _is_rate_limited(ip):
        log.warning("rate_limited", ip=ip)
        return JSONResponse(
            {"error": "Too many requests. Please wait a minute and try again."},
            status_code=429,
        )

    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_BODY_BYTES:
        return JSONResponse({"error": "Request too large."}, status_code=413)

    body = await request.json()
    raw_isbns: list[str] = body.get("isbns", [])
    location: str = body.get("location", "").strip()

    if not location:
        return JSONResponse({"error": "Location name is required."}, status_code=400)

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

    _record_request(ip)

    _clean_expired()
    if len(sessions) >= MAX_SESSIONS:
        return JSONResponse(
            {"error": "Server is busy. Please try again in a few minutes."},
            status_code=503,
        )

    tmp_dir = Path(tempfile.mkdtemp(prefix="cataloger_"))
    image_dir = tmp_dir / "images"

    hardcover_token = os.environ.get("HARDCOVER_TOKEN", "")
    fetcher = BookFetcher(image_dir=image_dir, hardcover_token=hardcover_token, cache=book_cache)
    books = await fetcher.fetch_all(isbns)

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
                "genres": b.genres,
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
    port = int(os.environ.get("PORT", "8000"))
    is_dev = os.environ.get("ENV", "dev") == "dev"
    uvicorn.run(
        "cataloger.web.app:app",
        host="0.0.0.0",
        port=port,
        reload=is_dev,
    )
