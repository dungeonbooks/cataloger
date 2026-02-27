"""SQLite-backed cache for ISBN lookup results."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import structlog

log = structlog.get_logger()

DEFAULT_TTL_DAYS = 7


class BookCache:
    """Cache book metadata and cover images in a local SQLite database."""

    def __init__(self, db_path: Path | None = None, ttl_days: float | None = None):
        if db_path is None:
            cache_dir = Path(os.environ.get("CACHE_DIR", ".cache"))
            cache_dir.mkdir(parents=True, exist_ok=True)
            db_path = cache_dir / "cataloger.db"

        if ttl_days is None:
            ttl_days = float(os.environ.get("CACHE_TTL_DAYS", DEFAULT_TTL_DAYS))

        self.db_path = db_path
        self.ttl_seconds = ttl_days * 86400
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS books (
                isbn TEXT PRIMARY KEY,
                metadata TEXT,
                image BLOB,
                image_source TEXT,
                image_url TEXT,
                cached_at REAL
            )"""
        )
        self._conn.commit()

    def get(self, isbn: str) -> tuple[dict, bytes | None, str, str] | None:
        """Return cached (metadata, image_bytes, image_source, image_url) or None."""
        row = self._conn.execute(
            "SELECT metadata, image, image_source, image_url, cached_at "
            "FROM books WHERE isbn = ?",
            (isbn,),
        ).fetchone()

        if row is None:
            return None

        metadata_json, image_bytes, image_source, image_url, cached_at = row

        if time.time() - cached_at > self.ttl_seconds:
            self._conn.execute("DELETE FROM books WHERE isbn = ?", (isbn,))
            self._conn.commit()
            log.debug("cache_expired", isbn=isbn)
            return None

        log.debug("cache_hit", isbn=isbn)
        return json.loads(metadata_json), image_bytes, image_source or "", image_url or ""

    def put(
        self,
        isbn: str,
        metadata: dict,
        image_bytes: bytes | None,
        image_source: str,
        image_url: str,
    ) -> None:
        """Store a lookup result in the cache."""
        self._conn.execute(
            "INSERT OR REPLACE INTO books (isbn, metadata, image, image_source, image_url, cached_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (isbn, json.dumps(metadata), image_bytes, image_source, image_url, time.time()),
        )
        self._conn.commit()
        log.debug("cache_store", isbn=isbn)
