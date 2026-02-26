# Cataloger — Project Plan

## What This Is

A free web tool (+ optional desktop app) that lets independent booksellers turn a list of ISBNs into a Square-ready catalog import — complete with cover images.

### The Problem

Adding books to Square is painful. For each book, a bookseller has to:
1. Look up the title, author, description, price
2. Hunt down a cover image (Google Images → right-click → save → crop → upload)
3. Fill out ~40 fields in Square's catalog import spreadsheet
4. Repeat for every single book

A shop adding 50 new books can easily burn a full afternoon on data entry.

### The Solution

Paste ISBNs → get a CSV + images ZIP → import into Square. Done in minutes.

---

## Product Tiers

### Tier 1 — Free Web App (no auth required)

A single-page web app hosted publicly.

**User flow:**
1. Bookseller pastes ISBNs (one per line) into a text box
2. Clicks "Generate Catalog"
3. Progress indicator shows books being fetched
4. Results page shows what was found (title, author, cover thumbnail, price)
5. User can review/edit before downloading
6. Downloads:
   - `catalog.csv` — Square-compatible, ready to import
   - `images.zip` — cover images named by ISBN

**Image upload is manual** — user drags images into Square Dashboard item-by-item, or uses Square's bulk image upload if available. We provide brief instructions.

### Tier 2 — Desktop App (user provides Square API token)

A downloadable macOS/Windows binary for power users.

**Additional capability:**
- User provides their Square access token (stored locally, never sent to us)
- App uploads images directly to Square via `CreateCatalogImage` API
- Links images to the correct catalog items automatically
- Can also create catalog items directly via `BatchUpsertCatalogObjects` (skip CSV entirely)

**Distribution:** GitHub releases, maybe Homebrew for macOS.

**Tech:** Python + uv for dependency management, PyInstaller or similar for packaging. Reuses the same core library as the web app.

---

## Architecture

```
cataloger/
├── src/
│   └── cataloger/
│       ├── core/                  # Shared library (used by both web + desktop)
│       │   ├── fetcher.py         # BookFetcher — metadata + image waterfall
│       │   ├── models.py          # BookData dataclass
│       │   ├── catalog.py         # CatalogWriter — Square CSV generation
│       │   └── images.py          # Image downloading + ZIP bundling
│       ├── web/                   # Tier 1 — Web app
│       │   ├── app.py             # FastAPI app
│       │   ├── routes.py          # API endpoints
│       │   ├── static/            # Frontend assets
│       │   │   ├── index.html     # Single page (vanilla HTML/CSS/JS or htmx)
│       │   │   ├── style.css
│       │   │   └── app.js
│       │   └── templates/         # Jinja2 if needed
│       └── desktop/               # Tier 2 — Desktop app (future)
│           ├── cli.py             # CLI entry point
│           └── square_client.py   # Square API integration
├── tests/
├── pyproject.toml
└── README.md
```

### Key Design Decisions

**Web framework:** FastAPI (same as Marty — team familiarity, async-native for concurrent ISBN fetching)

**Frontend:** Keep it dead simple. A single HTML page with vanilla JS or htmx. No React/Vue/build step. The UI is literally a text box, a button, and a results table.

**Processing model:**
- User submits ISBNs via POST
- Backend fetches metadata + images concurrently (with rate limit staggering)
- Returns results as JSON for the preview page
- Download endpoints serve the CSV and ZIP

**Package management:** uv for fast dependency resolution, lockfile, and running scripts.

**Hosting:** Railway (same platform as Marty, team already knows it) or Vercel/Fly.io — it's a lightweight stateless app.

---

## Data Sources (same as PoC)

| Source | What | Auth | Notes |
|---|---|---|---|
| Google Books API | Title, author, description, page count, list price | None | Free, rate-limited (~100 req/min) |
| Open Library API | Metadata fallback | None | Free, reliable, no price data |
| Bookcover API | High-quality cover images (via Goodreads) | None | Free, best image quality |
| Open Library Covers | Image fallback | None | Direct URL, always available |

### Image Waterfall (unchanged)

1. Bookcover API (`bookcover.longitood.com`) — best quality
2. Google Books thumbnail (upscaled `zoom=3`) — decent fallback
3. Open Library Covers — always available, variable quality

---

## API Endpoints

```
POST /api/lookup
  Body: { "isbns": ["9780553381696", "9780451457998", ...] }
  Response: { "books": [...], "summary": { "found": 2, "missing": 0 } }
  → Fetches metadata + images, returns results for preview

GET /api/download/csv?session={id}
  → Returns the Square-compatible CSV

GET /api/download/images?session={id}
  → Returns ZIP of cover images

GET /api/download/all?session={id}
  → Returns ZIP containing both CSV + images/
```

### Session Model

Results are stored ephemerally (in-memory or temp files) with a session ID. Auto-expire after 30 minutes. No database needed.

---

## Square CSV Format

42 columns matching Square's catalog export format. Populated fields:

| Column | Value |
|---|---|
| Item Name | `"{Title} by {Author}"` |
| Variation Name | `"Regular"` |
| SKU | ISBN-13 |
| GTIN | ISBN-13 |
| Description | From metadata source |
| Categories | `"Books"` |
| Reporting Category | `"Books"` |
| Square Online Item Visibility | `"visible"` |
| Item Type | `"Physical good"` |
| Shipping Enabled | `"Y"` |
| Pickup Enabled | `"Y"` |
| Enabled {Location} | `"Y"` |
| Price | List price (if available) |

All other columns left empty for Square/user to fill.

---

## Implementation Phases

### Phase 1 — Core Library + CLI
- [ ] Set up repo with uv (pyproject.toml, ruff, pytest, CI)
- [ ] Port and refine `BookFetcher` from PoC (metadata + images)
- [ ] Port `CatalogWriter` (Square CSV)
- [ ] Add ZIP bundling for images
- [ ] CLI entry point (`uv run cataloger 9780553381696 ...`)
- [ ] Tests for core logic (mock HTTP responses)

### Phase 2 — Web App
- [ ] FastAPI app with API endpoints
- [ ] Session management (temp storage for results)
- [ ] Frontend: single HTML page with ISBN input + results preview
- [ ] Download endpoints (CSV, ZIP, combined)
- [ ] Deploy to Railway
- [ ] Basic rate limiting (prevent abuse)

### Phase 3 — Desktop App (Tier 2)
- [ ] Square API client (catalog + image upload)
- [ ] CLI with `--square-token` flag
- [ ] PyInstaller packaging for macOS + Windows
- [ ] GitHub Releases distribution
- [ ] User-facing docs for getting a Square API token

---

## Open Questions

1. **Location name in CSV** — Square CSV uses location-specific columns like "Enabled Bookstore", "Price Bookstore". Should we let users specify their location name, or default to something generic?
2. **Author/title search** — The user mentioned "author/titles" as input too, not just ISBNs. Should Phase 1 support fuzzy title search → ISBN resolution?
3. **Branding** — Should this be branded as a Dungeon Books product, or a standalone tool? (Affects domain, logo, etc.)
4. **Rate limiting** — How many ISBNs per request? 50? 100? Need to balance UX with API rate limits.
