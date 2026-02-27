"""Fetch book metadata and cover images from multiple sources."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import structlog

from .models import BookData

log = structlog.get_logger()

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

    def __init__(self, image_dir: Path, hardcover_token: str = "") -> None:
        self.image_dir = image_dir
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.hardcover_token = hardcover_token

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

        # 3. Open Library
        url = self._open_library_url(isbn)
        path = await self._download_image(client, url, isbn)
        if path:
            return path, "open_library", url

        return None, "", ""

    async def fetch_book(self, client: httpx.AsyncClient, isbn: str) -> BookData:
        """Fetch all data for a single ISBN.

        Flow:
        1. Try Hardcover API (title, author, description, pages, genres, cover image)
        2. If no Hardcover match → Open Library edition + works (fallback)
        3. Cover image waterfall: Hardcover → Bookcover API → Open Library
        """
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
