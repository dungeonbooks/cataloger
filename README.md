# Cataloger

Batch-import books into Square by ISBN. Paste ISBNs, get a ready-to-import CSV with titles, authors, descriptions, genres, and cover images.

**Live at [tools.dungeonbooks.com](https://tools.dungeonbooks.com/)**

## How it works

1. Enter your Square store location name
2. Paste up to 100 ISBNs
3. Hit "Generate Catalog"
4. Download the CSV and cover images
5. Import the CSV into Square

Cover images can't be imported via CSV — the app downloads them separately, named by ISBN, for manual upload. Direct Square integration is in progress.

## Data sources

Metadata is fetched in a waterfall:

- **[Hardcover](https://hardcover.app)** (primary) — title, author, description, pages, genres, cover
- **[Open Library](https://openlibrary.org)** (fallback) — title, author, description, pages

Cover images fall back through Hardcover → [Bookcover API](https://bookcover.longitood.com) → Open Library Covers.

## Setup

Requires Python 3.12+ and [uv](https://github.com/astral-sh/uv).

```bash
uv sync
```

Create a `.env` file:

```
HARDCOVER_TOKEN=Bearer <your-token>
```

Get a token from your [Hardcover account](https://hardcover.app). Without it, the app falls back to Open Library only.

## Run

```bash
uv run cataloger
```

The app starts at `http://localhost:8000`.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `HARDCOVER_TOKEN` | — | Hardcover API bearer token |
| `PORT` | `8000` | Server port |
| `ENV` | `dev` | `dev` enables hot reload |
| `RATE_LIMIT` | `10` | Max requests per window |
| `RATE_LIMIT_WINDOW` | `60` | Rate limit window in seconds |
| `CACHE_DIR` | `.cache` | SQLite cache directory |
| `CACHE_TTL_DAYS` | `7` | Cache expiration in days |

## Stack

- **Backend:** FastAPI, httpx, SQLite (cache), structlog
- **Frontend:** Vanilla JS, Tailwind + DaisyUI (CDN)
- **Deployment:** Railway

## License

MIT
