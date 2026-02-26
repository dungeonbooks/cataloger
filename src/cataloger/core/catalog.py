"""Generate Square-compatible catalog CSV from book data."""

from __future__ import annotations

import csv
import io
from pathlib import Path

import structlog

from .models import BookData

log = structlog.get_logger()


def square_columns(location: str) -> list[str]:
    """Build the full 42-column header list with dynamic location name."""
    return [
        "Token",
        "Item Name",
        "Customer-facing Name",
        "Variation Name",
        "SKU",
        "Description",
        "Categories",
        "Reporting Category",
        "SEO Title",
        "SEO Description",
        "Permalink",
        "GTIN",
        "Square Online Item Visibility",
        "Item Type",
        "Weight (lb)",
        "Social Media Link Title",
        "Social Media Link Description",
        "Shipping Enabled",
        "Self-serve Ordering Enabled",
        "Delivery Enabled",
        "Pickup Enabled",
        "Price",
        "Online Sale Price",
        "Archived",
        "Sellable",
        "Contains Alcohol",
        "Stockable",
        "Skip Detail Screen in POS",
        "Option Name 1",
        "Option Value 1",
        f"Enabled {location}",
        f"Current Quantity {location}",
        f"New Quantity {location}",
        f"Stock Alert Enabled {location}",
        f"Stock Alert Count {location}",
        f"Price {location}",
    ]


def _book_to_row(book: BookData, location: str, columns: list[str]) -> dict[str, str]:
    """Map BookData fields to Square CSV columns."""
    item_name = f"{book.title} by {book.author}" if book.author else book.title
    row: dict[str, str] = dict.fromkeys(columns, "")
    row.update(
        {
            "Item Name": item_name,
            "Variation Name": "Regular",
            "SKU": book.isbn,
            "Description": book.description,
            "Categories": "Books",
            "Reporting Category": "Books",
            "GTIN": book.isbn,
            "Square Online Item Visibility": "visible",
            "Item Type": "Physical good",
            "Shipping Enabled": "Y",
            "Pickup Enabled": "Y",
            f"Enabled {location}": "Y",
        }
    )
    if book.price:
        row["Price"] = book.price
    return row


def write_csv(books: list[BookData], location: str, output: Path) -> None:
    """Write books to a Square-compatible CSV file."""
    columns = square_columns(location)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for book in books:
            if not book.title:
                continue
            writer.writerow(_book_to_row(book, location, columns))
    log.info("csv_written", path=str(output), books=len(books))


def generate_csv_bytes(books: list[BookData], location: str) -> bytes:
    """Generate CSV content as bytes (for web download)."""
    columns = square_columns(location)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns)
    writer.writeheader()
    for book in books:
        if not book.title:
            continue
        writer.writerow(_book_to_row(book, location, columns))
    return buf.getvalue().encode("utf-8")
