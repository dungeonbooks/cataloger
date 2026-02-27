"""Fetch book metadata and cover images from multiple sources."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import httpx
import structlog

from .cache import BookCache
from .models import BookData

log = structlog.get_logger()

# Open Library API compliance (https://openlibrary.org/developers/api)
# Identified requests get 3 req/s; unidentified get 1 req/s.
_OL_CONTACT = os.environ.get("OL_CONTACT_EMAIL", "")
_OL_USER_AGENT = f"DungeonBooksCataloger/0.1.0 ({_OL_CONTACT})" if _OL_CONTACT else "DungeonBooksCataloger/0.1.0"
_OL_MIN_INTERVAL = 0.35  # seconds between Open Library requests (~2.8 req/s, within 3 req/s limit)

HARDCOVER_QUERY = """
query ($isbn: String!) {
  editions(where: {isbn_13: {_eq: $isbn}}) {
    title
    pages
    image {
      url
    }
    book {
      description
      contributions {
        author {
          name
        }
      }
      cached_tags
    }
  }
}
"""


class BookFetcher:
    """Fetches book metadata and cover images from multiple sources.

    Metadata: Hardcover (primary) → Open Library (fallback).
    Image waterfall: Hardcover image → Bookcover API → Open Library.
    """

    def __init__(
        self,
        image_dir: Path,
        hardcover_token: str = "",
        cache: BookCache | None = None,
    ) -> None:
        self.image_dir = image_dir
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.hardcover_token = hardcover_token
        self.cache = cache
        self._ol_last_request: float = 0.0  # monotonic timestamp of last OL request

    async def _ol_get(
        self, client: httpx.AsyncClient, url: str, **kwargs: object
    ) -> httpx.Response:
        """Rate-limited GET for Open Library endpoints.

        Enforces per-request throttling and sets the required User-Agent header.
        """
        kwargs.setdefault("timeout", 10)
        kwargs.setdefault("headers", {})
        kwargs["headers"]["User-Agent"] = _OL_USER_AGENT  # type: ignore[index]

        # Enforce minimum interval between OL requests
        now = time.monotonic()
        elapsed = now - self._ol_last_request
        if elapsed < _OL_MIN_INTERVAL:
            await asyncio.sleep(_OL_MIN_INTERVAL - elapsed)
        self._ol_last_request = time.monotonic()

        return await client.get(url, **kwargs)

    async def fetch_hardcover(
        self, client: httpx.AsyncClient, isbn: str
    ) -> dict | None:
        """Fetch metadata from Hardcover GraphQL API.

        Returns dict with title, author, description, pages, genres, cover_url
        or None if no match.
        """
        if not self.hardcover_token:
            return None

        url = "https://api.hardcover.app/v1/graphql"
        headers = {"Authorization": self.hardcover_token, "Content-Type": "application/json"}

        try:
            resp = await client.post(
                url,
                json={"query": HARDCOVER_QUERY, "variables": {"isbn": isbn}},
                headers=headers,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            editions = data.get("data", {}).get("editions", [])
            if not editions:
                log.debug("hardcover_no_match", isbn=isbn)
                return None

            edition = editions[0]
            book = edition.get("book") or {}

            # Title from edition
            title = edition.get("title", "")

            # Authors from contributions
            authors = []
            for contrib in book.get("contributions", []):
                name = contrib.get("author", {}).get("name", "")
                if name:
                    authors.append(name)

            # Description from book
            desc = book.get("description", "") or ""

            # Pages from edition
            pages = edition.get("pages") or 0

            # Genres from cached_tags (dict keyed by category)
            genres = []
            cached_tags = book.get("cached_tags") or {}
            for entry in cached_tags.get("Genre", []):
                tag_name = entry.get("tag", "")
                if tag_name:
                    genres.append(tag_name)

            # Cover image
            cover_url = ""
            image = edition.get("image") or {}
            if image.get("url"):
                cover_url = image["url"]

            log.debug(
                "hardcover_hit",
                isbn=isbn,
                title=title,
                authors=authors,
                has_desc=bool(desc),
                pages=pages,
                genres_count=len(genres),
            )
            return {
                "title": title,
                "author": ", ".join(authors),
                "description": desc,
                "pages": pages,
                "genres": genres,
                "cover_url": cover_url,
            }

        except httpx.HTTPError as e:
            log.debug("hardcover_error", isbn=isbn, error=str(e))
            return None

    async def fetch_metadata(
        self, client: httpx.AsyncClient, isbn: str
    ) -> dict | None:
        """Fetch metadata from Open Library edition endpoint."""
        url = f"https://openlibrary.org/isbn/{isbn}.json"
        try:
            resp = await self._ol_get(url=url, client=client, follow_redirects=True)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            result: dict = {"title": data.get("title", "")}
            author_keys = [a.get("key") for a in data.get("authors", [])]
            authors = []
            for key in author_keys:
                if key:
                    a_resp = await self._ol_get(
                        client=client, url=f"https://openlibrary.org{key}.json"
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
            resp = await self._ol_get(client=client, url=url)
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
                a_resp = await self._ol_get(
                    client=client, url=f"https://openlibrary.org{key}.json"
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

    async def _download_ol_image(
        self, client: httpx.AsyncClient, url: str, isbn: str
    ) -> Path | None:
        """Download an Open Library image with rate limiting and User-Agent."""
        try:
            resp = await self._ol_get(client=client, url=url, timeout=15, follow_redirects=True)
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
        hardcover_cover_url: str = "",
    ) -> tuple[Path | None, str, str]:
        """Waterfall: Hardcover image → Bookcover API → Open Library.

        Returns (path, source_name, image_url).
        """
        # 1. Hardcover cover image
        if hardcover_cover_url:
            path = await self._download_image(client, hardcover_cover_url, isbn)
            if path:
                return path, "hardcover", hardcover_cover_url

        # 2. Bookcover API
        url = await self._try_bookcover_api(client, isbn)
        if url:
            path = await self._download_image(client, url, isbn)
            if path:
                return path, "bookcover_api", url

        # 3. Open Library (rate-limited)
        url = self._open_library_url(isbn)
        path = await self._download_ol_image(client, url, isbn)
        if path:
            return path, "open_library", url

        return None, "", ""

    def _book_from_cache(
        self, isbn: str, metadata: dict, image_bytes: bytes | None, image_source: str, image_url: str
    ) -> BookData:
        """Build a BookData from cached values and write image to disk."""
        book = BookData(
            isbn=isbn,
            title=metadata.get("title", ""),
            author=metadata.get("author", ""),
            description=metadata.get("description", ""),
            page_count=metadata.get("page_count", 0),
            genres=metadata.get("genres", []),
            image_source=image_source,
            image_url=image_url,
        )
        if image_bytes:
            dest = self.image_dir / f"{isbn}.jpg"
            dest.write_bytes(image_bytes)
            book.image_path = dest
        else:
            book.errors.append("No cover image found")
        return book

    async def fetch_book(self, client: httpx.AsyncClient, isbn: str) -> BookData:
        """Fetch all data for a single ISBN.

        Flow:
        0. Check cache — return immediately on hit
        1. Try Hardcover API (title, author, description, pages, genres, cover image)
        2. If no Hardcover match → Open Library edition + works (fallback)
        3. Cover image waterfall: Hardcover → Bookcover API → Open Library
        4. Store result in cache
        """
        # 0. Cache check
        if self.cache:
            cached = self.cache.get(isbn)
            if cached:
                self._last_cache_hit = True
                return self._book_from_cache(isbn, *cached)

        book = BookData(isbn=isbn)
        hardcover_cover_url = ""

        # 1. Try Hardcover API
        hardcover = await self.fetch_hardcover(client, isbn)
        if hardcover:
            book.title = hardcover.get("title", "")
            book.author = hardcover.get("author", "")
            book.description = hardcover.get("description", "")
            book.page_count = hardcover.get("pages", 0)
            book.genres = hardcover.get("genres", [])
            hardcover_cover_url = hardcover.get("cover_url", "")
        else:
            # 2. Fallback to Open Library
            metadata = await self.fetch_metadata(client, isbn)
            if metadata:
                book.title = metadata.get("title", "")
                authors = metadata.get("authors", [])
                book.author = ", ".join(authors) if authors else ""
                book.description = metadata.get("description", "")
                book.page_count = metadata.get("pageCount", 0)

                # If missing description or author, try Works endpoint
                works_key = metadata.get("works_key", "")
                if works_key and (not book.description or not book.author):
                    works = await self.fetch_works_data(client, works_key, isbn)
                    if not book.description and works["description"]:
                        book.description = works["description"]
                    if not book.author and works["authors"]:
                        book.author = ", ".join(works["authors"])
            else:
                book.errors.append("No metadata found")

        # 3. Cover image waterfall
        image_path, source, image_url = await self.fetch_cover_image(
            client, isbn, hardcover_cover_url
        )
        book.image_path = image_path
        book.image_source = source
        book.image_url = image_url
        if not image_path:
            book.errors.append("No cover image found")

        # 4. Store in cache
        if self.cache and book.title:
            image_bytes = book.image_path.read_bytes() if book.image_path else None
            self.cache.put(
                isbn,
                {
                    "title": book.title,
                    "author": book.author,
                    "description": book.description,
                    "page_count": book.page_count,
                    "genres": book.genres,
                },
                image_bytes,
                book.image_source,
                book.image_url,
            )

        return book

    async def fetch_all(
        self,
        isbns: list[str],
        on_progress: callable | None = None,
    ) -> list[BookData]:
        """Fetch data for all ISBNs with staggered starts to avoid rate limits.

        on_progress is called with (index, total, book) after each ISBN completes.
        Cache hits skip the rate-limit sleep since no API call is made.
        """
        results: list[BookData] = []
        async with httpx.AsyncClient() as client:
            for i, isbn in enumerate(isbns):
                self._last_cache_hit = False
                book = await self.fetch_book(client, isbn)
                results.append(book)
                if on_progress:
                    on_progress(i + 1, len(isbns), book)
                # Rate limiting is handled per-request in _ol_get; no extra
                # sleep needed between ISBNs.
        return results
