# URL Shortener API

A production-style URL shortener REST API built with **FastAPI and SQLite**, with click analytics, link expiry, custom aliases, owner-key authorization and rate limiting. Fully covered by an automated test suite.

## Features

- **Short codes**: 7-character codes from a cryptographically secure generator (`secrets`), with collision checks
- **Custom aliases**: `/my-portfolio` style codes, validated, with reserved words blocked
- **Link expiry**: optional TTL in days, and expired links return `410 Gone`
- **Click analytics**: total clicks, clicks per day and top referrers
- **Owner keys**: each link gets a secret key, shown once, needed to view stats or delete it. Compared with `secrets.compare_digest` to avoid timing attacks
- **Rate limiting**: sliding-window limiter per client IP on link creation, returns `429` with a `Retry-After` header
- **Input validation**: Pydantic v2 models, so only `http(s)` URLs with a domain are accepted
- **SQLite with foreign keys**: deleting a link cascades to its click records

## API

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/links` | none | Create a short link |
| GET | `/{code}` | none | Redirect (307) and record a click |
| GET | `/api/links/{code}/stats` | `X-Owner-Key` | Click analytics |
| DELETE | `/api/links/{code}` | `X-Owner-Key` | Delete the link |
| GET | `/health` | none | Health check |

### Example

```bash
curl -X POST http://127.0.0.1:8000/api/links \
  -H "Content-Type: application/json" \
  -d '{"url": "https://github.com", "custom_code": "gh", "expires_in_days": 30}'
```

```json
{
  "code": "gh",
  "short_url": "http://127.0.0.1:8000/gh",
  "target_url": "https://github.com",
  "expires_at": "2026-10-29T10:00:00+00:00",
  "owner_key": "q3Zk...save-this..."
}
```

```bash
curl http://127.0.0.1:8000/api/links/gh/stats -H "X-Owner-Key: q3Zk..."
```

## Run it

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Interactive docs: http://127.0.0.1:8000/docs

Run the tests:

```bash
pytest -q
```

## Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `DB_PATH` | `urls.db` | SQLite file |
| `BASE_URL` | `http://127.0.0.1:8000` | Used to build `short_url` |
| `RATE_LIMIT` | `10` | Link creations allowed per IP per minute |

## Design notes

- The in-memory rate limiter is fine for a single process. Running several workers would need a shared store like Redis.
- Clicks are stored as individual rows, so any analytics can be computed later with SQL.

## Tech stack

Python, FastAPI, Pydantic v2, SQLite, pytest
