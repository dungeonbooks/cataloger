"""Bundle cover images into a ZIP archive."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from .models import BookData


def create_image_zip(books: list[BookData]) -> bytes:
    """Create a ZIP archive of all downloaded cover images.

    Returns the ZIP as bytes.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for book in books:
            if book.image_path and book.image_path.exists():
                zf.write(book.image_path, f"{book.isbn}.jpg")
    return buf.getvalue()


def create_combined_zip(
    csv_bytes: bytes, books: list[BookData]
) -> bytes:
    """Create a ZIP containing both the CSV and cover images."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("catalog.csv", csv_bytes)
        for book in books:
            if book.image_path and book.image_path.exists():
                zf.write(book.image_path, f"images/{book.isbn}.jpg")
    return buf.getvalue()
