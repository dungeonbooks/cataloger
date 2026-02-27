"""Data models for book metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BookData:
    isbn: str
    title: str = ""
    author: str = ""
    description: str = ""
    page_count: int = 0
    price: str = ""
    genres: list[str] = field(default_factory=list)
    image_path: Path | None = None
    image_url: str = ""
    image_source: str = ""
    errors: list[str] = field(default_factory=list)
