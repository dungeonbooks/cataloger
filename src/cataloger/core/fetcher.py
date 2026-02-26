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

    Metadata: Open Library (primary) → Google Books (enrichment).
    Image waterfall: Bookcover API → Google Books thumbnail → Open Library.
    """

    def __init__(self, image_dir: Path) -> None:
        self.image_dir = image_dir
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self._google_books_disabled = False

    async def fetch_metadata(
        self, client: httpx.AsyncClient, isbn: str
    ) -> dict | None:
        """Fetch metadata from Open Library edition endpoint."""
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

            # Store works key so we can follow it for descriptions
            works = data.get("works", [])
            if works:
                result["works_key"] = works[0].get("key", "")

            return result
        except httpx.HTTPError as e:
            log.debug("openlibrary_metadata_error", isbn=isbn, error=str(e))
            return None

    async def fetch_works_data(
        self, client: httpx.AsyncClient, works_key: str, isbn: str
    ) -> dict:
        """Fetch description and authors from Open Library Works endpoint.

        Returns dict with 'description' and 'authors' keys.
        """
        url = f"https://openlibrary.org{works_key}.json"
        result: dict = {"description": "", "authors": []}
        try:
            resp = await client.get(url, timeout=10)
            if resp.status_code != 200:
                return result
            data = resp.json()

            desc = data.get("description", "")
            if isinstance(desc, dict):
                desc = desc.get("value", "")
            result["description"] = desc

            # Authors on Works are stored as {"author": {"key": "/authors/..."}}
            author_keys = []
            for entry in data.get("authors", []):
                key = entry.get("author", {}).get("key", "")
                if key:
                    author_keys.append(key)
            authors = []
            for key in author_keys:
                a_resp = await client.get(
                    f"https://openlibrary.org{key}.json", timeout=10
                )
                if a_resp.status_code == 200:
                    name = a_resp.json().get("name", "")
                    if name:
                        authors.append(name)
            result["authors"] = authors

            if desc or authors:
                log.debug(
                    "works_data_found",
                    isbn=isbn,
                    works_key=works_key,
                    has_desc=bool(desc),
                    authors=authors,
                )
            return result
        except httpx.HTTPError as e:
            log.debug("works_data_error", isbn=isbn, error=str(e))
            return result

    async def fetch_google_books(
        self, client: httpx.AsyncClient, isbn: str
    ) -> dict | None:
        """Fetch from Google Books with circuit breaker.

        After the first 429, disables Google Books for the rest of the batch
        to avoid wasting time on doomed retries.

        Returns dict with 'description', 'price', and 'thumbnail' keys,
        or None on failure.
        """
        if self._google_books_disabled:
            return None

        url = f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}"

        await asyncio.sleep(1)  # pre-call delay to respect rate limits

        try:
            resp = await client.get(url, timeout=10)
            if resp.status_code == 429:
                log.warning(
                    "google_books_rate_limited_disabling",
                    isbn=isbn,
                    msg="Disabling Google Books for remaining ISBNs",
                )
                self._google_books_disabled = True
                return None
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", [])
            if not items:
                return None

            volume = items[0].get("volumeInfo", {})
            sale = items[0].get("saleInfo", {})

            result: dict = {}

            # Description
            result["description"] = volume.get("description", "")

            # Price from saleInfo.listPrice
            list_price = sale.get("listPrice", {})
            if list_price:
                amount = list_price.get("amount")
                currency = list_price.get("currencyCode", "")
                if amount is not None:
                    result["price"] = f"{currency} {amount}"

            # Thumbnail
            images = volume.get("imageLinks", {})
            result["thumbnail"] = images.get("thumbnail", "")

            log.debug("google_books_hit", isbn=isbn, has_price=bool(result.get("price")))
            return result

        except httpx.HTTPError as e:
            log.debug("google_books_error", isbn=isbn, error=str(e))
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
        google_thumbnail: str = "",
    ) -> tuple[Path | None, str, str]:
        """Waterfall: Bookcover API → Google Books thumbnail → Open Library.

        Returns (path, source_name, image_url).
        """
        # 1. Bookcover API
        url = await self._try_bookcover_api(client, isbn)
        if url:
            path = await self._download_image(client, url, isbn)
            if path:
                return path, "bookcover_api", url

        # 2. Google Books thumbnail
        if google_thumbnail:
            path = await self._download_image(client, google_thumbnail, isbn)
            if path:
                return path, "google_books", google_thumbnail

        # 3. Open Library
        url = self._open_library_url(isbn)
        path = await self._download_image(client, url, isbn)
        if path:
            return path, "open_library", url

        return None, "", ""

    async def fetch_book(self, client: httpx.AsyncClient, isbn: str) -> BookData:
        """Fetch all data for a single ISBN.

        Flow:
        1. Open Library edition metadata
        2. If no description → Open Library Works endpoint
        3. If still no description OR no price → Google Books
        4. Cover image waterfall
        """
        book = BookData(isbn=isbn)
        google_thumbnail = ""

        # 1. Open Library edition metadata
        metadata = await self.fetch_metadata(client, isbn)
        if metadata:
            book.title = metadata.get("title", "")
            authors = metadata.get("authors", [])
            book.author = ", ".join(authors) if authors else ""
            book.description = metadata.get("description", "")
            book.page_count = metadata.get("pageCount", 0)

            # 2. If missing description or author, try Works endpoint
            works_key = metadata.get("works_key", "")
            if works_key and (not book.description or not book.author):
                works = await self.fetch_works_data(client, works_key, isbn)
                if not book.description and works["description"]:
                    book.description = works["description"]
                if not book.author and works["authors"]:
                    book.author = ", ".join(works["authors"])
        else:
            book.errors.append("No metadata found")

        # 3. Google Books enrichment (if missing description or price)
        if not book.description or not book.price:
            google = await self.fetch_google_books(client, isbn)
            if google:
                if not book.description and google.get("description"):
                    book.description = google["description"]
                if google.get("price"):
                    book.price = google["price"]
                google_thumbnail = google.get("thumbnail", "")

        # 4. Cover image waterfall
        image_path, source, image_url = await self.fetch_cover_image(
            client, isbn, google_thumbnail
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
        self._google_books_disabled = False
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
