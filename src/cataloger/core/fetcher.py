"""Fetch book metadata and cover images from multiple sources."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import structlog

from .models import BookData

log = structlog.get_logger()


class BookFetcher:
    """Fetches book metadata and cover images from multiple sources.

    Image waterfall: Bookcover API → Google Books thumbnail → Open Library.
    Metadata waterfall: Google Books → Open Library.
    """

    def __init__(self, image_dir: Path) -> None:
        self.image_dir = image_dir
        self.image_dir.mkdir(parents=True, exist_ok=True)

    async def fetch_metadata(self, client: httpx.AsyncClient, isbn: str) -> dict | None:
        """Fetch metadata from Google Books API with retry on 429."""
        url = "https://www.googleapis.com/books/v1/volumes"
        params = {"q": f"isbn:{isbn}"}
        for attempt in range(3):
            try:
                resp = await client.get(url, params=params)
                if resp.status_code == 429:
                    wait = 2 ** (attempt + 1)
                    log.debug("google_books_rate_limit", isbn=isbn, retry_in=wait)
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if data.get("totalItems", 0) == 0:
                    return None
                return data["items"][0]["volumeInfo"]
            except httpx.HTTPError as e:
                log.warning("google_books_error", isbn=isbn, error=str(e))
                return None
        log.warning("google_books_exhausted_retries", isbn=isbn)
        return None

    async def fetch_openlibrary_metadata(
        self, client: httpx.AsyncClient, isbn: str
    ) -> dict | None:
        """Fallback: fetch metadata from Open Library."""
        url = f"https://openlibrary.org/isbn/{isbn}.json"
        try:
            resp = await client.get(url, timeout=10, follow_redirects=True)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            result: dict = {"title": data.get("title", "")}
            author_keys = [a.get("key") for a in data.get("authors", [])]
            authors = []
            for key in author_keys:
                if key:
                    a_resp = await client.get(
                        f"https://openlibrary.org{key}.json", timeout=10
                    )
                    if a_resp.status_code == 200:
                        authors.append(a_resp.json().get("name", ""))
            result["authors"] = authors
            result["pageCount"] = data.get("number_of_pages", 0)
            desc = data.get("description", "")
            if isinstance(desc, dict):
                desc = desc.get("value", "")
            result["description"] = desc
            return result
        except httpx.HTTPError as e:
            log.debug("openlibrary_metadata_error", isbn=isbn, error=str(e))
            return None

    async def _try_bookcover_api(
        self, client: httpx.AsyncClient, isbn: str
    ) -> str | None:
        """Try Bookcover API (Goodreads covers)."""
        url = f"https://bookcover.longitood.com/bookcover?isbn={isbn}"
        try:
            resp = await client.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            cover_url = data.get("url")
            if cover_url:
                log.debug("bookcover_api_hit", isbn=isbn)
                return cover_url
        except httpx.HTTPError as e:
            log.debug("bookcover_api_miss", isbn=isbn, error=str(e))
        return None

    async def _try_google_thumbnail(
        self, metadata: dict | None, isbn: str
    ) -> str | None:
        """Extract and upgrade Google Books thumbnail URL."""
        if not metadata:
            return None
        image_links = metadata.get("imageLinks", {})
        thumb = image_links.get("thumbnail") or image_links.get("smallThumbnail")
        if thumb:
            upgraded = thumb.replace("zoom=1", "zoom=3")
            log.debug("google_thumbnail_hit", isbn=isbn)
            return upgraded
        return None

    def _open_library_url(self, isbn: str) -> str:
        return f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"

    async def _download_image(
        self, client: httpx.AsyncClient, url: str, isbn: str
    ) -> Path | None:
        """Download an image URL to disk. Returns path or None."""
        try:
            resp = await client.get(url, timeout=15, follow_redirects=True)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if len(resp.content) < 1000 or "text/html" in content_type:
                return None
            dest = self.image_dir / f"{isbn}.jpg"
            dest.write_bytes(resp.content)
            return dest
        except httpx.HTTPError as e:
            log.debug("image_download_failed", isbn=isbn, url=url, error=str(e))
            return None

    async def fetch_cover_image(
        self,
        client: httpx.AsyncClient,
        isbn: str,
        metadata: dict | None,
    ) -> tuple[Path | None, str, str]:
        """Waterfall: Bookcover API → Google thumbnail → Open Library.

        Returns (path, source_name, image_url).
        """
        # 1. Bookcover API
        url = await self._try_bookcover_api(client, isbn)
        if url:
            path = await self._download_image(client, url, isbn)
            if path:
                return path, "bookcover_api", url

        # 2. Google Books thumbnail
        url = await self._try_google_thumbnail(metadata, isbn)
        if url:
            path = await self._download_image(client, url, isbn)
            if path:
                return path, "google_books", url

        # 3. Open Library
        url = self._open_library_url(isbn)
        path = await self._download_image(client, url, isbn)
        if path:
            return path, "open_library", url

        return None, "", ""

    async def fetch_book(self, client: httpx.AsyncClient, isbn: str) -> BookData:
        """Fetch all data for a single ISBN."""
        book = BookData(isbn=isbn)

        metadata = await self.fetch_metadata(client, isbn)
        if metadata:
            book.title = metadata.get("title", "")
            authors = metadata.get("authors", [])
            book.author = ", ".join(authors) if authors else ""
            book.description = metadata.get("description", "")
            book.page_count = metadata.get("pageCount", 0)
            price_info = metadata.get("listPrice") or metadata.get("saleInfo", {}).get(
                "listPrice"
            )
            if price_info and "amount" in price_info:
                book.price = f"{price_info['amount']:.2f}"
        else:
            ol_meta = await self.fetch_openlibrary_metadata(client, isbn)
            if ol_meta:
                book.title = ol_meta.get("title", "")
                authors = ol_meta.get("authors", [])
                book.author = ", ".join(authors) if authors else ""
                book.description = ol_meta.get("description", "")
                book.page_count = ol_meta.get("pageCount", 0)
                log.info("openlibrary_metadata_used", isbn=isbn)
            else:
                book.errors.append("No metadata found")

        image_path, source, image_url = await self.fetch_cover_image(
            client, isbn, metadata
        )
        book.image_path = image_path
        book.image_source = source
        book.image_url = image_url
        if not image_path:
            book.errors.append("No cover image found")

        return book

    async def fetch_all(
        self,
        isbns: list[str],
        on_progress: callable | None = None,
    ) -> list[BookData]:
        """Fetch data for all ISBNs with staggered starts to avoid rate limits.

        on_progress is called with (index, total, book) after each ISBN completes.
        """
        results: list[BookData] = []
        async with httpx.AsyncClient() as client:
            for i, isbn in enumerate(isbns):
                book = await self.fetch_book(client, isbn)
                results.append(book)
                if on_progress:
                    on_progress(i + 1, len(isbns), book)
                if i < len(isbns) - 1:
                    await asyncio.sleep(0.3)
        return results
