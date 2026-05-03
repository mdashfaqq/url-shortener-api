"""URL shortener API with click analytics and rate limiting.

Run:  uvicorn main:app --reload
Docs: http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import string
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator

DB_PATH = os.getenv("DB_PATH", "urls.db")
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000")
ALPHABET = string.ascii_letters + string.digits
CODE_LEN = 7
RESERVED = {"docs", "redoc", "openapi.json", "api", "health"}


# ---------- database ----------

def init_db(path: str = DB_PATH):
    with sqlite3.connect(path) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS links (
            code        TEXT PRIMARY KEY,
            target_url  TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            expires_at  TEXT,
            owner_key   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS clicks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            code        TEXT NOT NULL REFERENCES links(code) ON DELETE CASCADE,
            clicked_at  TEXT NOT NULL,
            referrer    TEXT,
            user_agent  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_clicks_code ON clicks(code);
        """)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def db():
    with get_conn() as conn:
        yield conn


# ---------- rate limiting (sliding window, per client IP) ----------

class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: float):
        self.max = max_requests
        self.window = window_seconds
        self.hits: dict[str, deque] = defaultdict(deque)

    def check(self, key: str):
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.max:
            retry = int(self.window - (now - q[0])) + 1
            raise HTTPException(429, "Rate limit exceeded", headers={"Retry-After": str(retry)})
        q.append(now)


create_limiter = RateLimiter(max_requests=int(os.getenv("RATE_LIMIT", "10")), window_seconds=60)


def limit_creates(request: Request):
    create_limiter.check(request.client.host if request.client else "unknown")


# ---------- schemas ----------

class CreateLink(BaseModel):
    url: str = Field(..., max_length=2048)
    custom_code: str | None = Field(None, min_length=3, max_length=30, pattern=r"^[A-Za-z0-9_-]+$")
    expires_in_days: int | None = Field(None, ge=1, le=365)

    @field_validator("url")
    @classmethod
    def valid_url(cls, v: str) -> str:
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("URL must start with http:// or https:// and include a domain")
        return v


class LinkOut(BaseModel):
    code: str
    short_url: str
    target_url: str
    expires_at: str | None
    owner_key: str | None = None


# ---------- app ----------

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="URL Shortener", version="1.0", lifespan=lifespan)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_code(conn) -> str:
    for _ in range(10):
        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
        if not conn.execute("SELECT 1 FROM links WHERE code = ?", (code,)).fetchone():
            return code
    raise HTTPException(500, "Could not generate a unique code")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/links", response_model=LinkOut, status_code=201, dependencies=[Depends(limit_creates)])
def create_link(body: CreateLink, conn=Depends(db)):
    if body.custom_code:
        code = body.custom_code
        if code.lower() in RESERVED:
            raise HTTPException(400, "That code is reserved")
        if conn.execute("SELECT 1 FROM links WHERE code = ?", (code,)).fetchone():
            raise HTTPException(409, "That custom code is already taken")
    else:
        code = generate_code(conn)

    expires = None
    if body.expires_in_days:
        expires = (datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)).isoformat()
    owner_key = secrets.token_urlsafe(16)  # lets the creator view stats / delete; shown once
    conn.execute(
        "INSERT INTO links (code, target_url, created_at, expires_at, owner_key) VALUES (?, ?, ?, ?, ?)",
        (code, body.url, now_iso(), expires, owner_key),
    )
    return LinkOut(code=code, short_url=f"{BASE_URL}/{code}", target_url=body.url,
                   expires_at=expires, owner_key=owner_key)


def get_owned_link(code: str, owner_key: str | None, conn):
    row = conn.execute("SELECT * FROM links WHERE code = ?", (code,)).fetchone()
    if not row:
        raise HTTPException(404, "Link not found")
    if not owner_key or not secrets.compare_digest(owner_key, row["owner_key"]):
        raise HTTPException(403, "Invalid owner key")
    return row


@app.get("/api/links/{code}/stats")
def link_stats(code: str, request: Request, conn=Depends(db)):
    row = get_owned_link(code, request.headers.get("X-Owner-Key"), conn)
    total = conn.execute("SELECT COUNT(*) FROM clicks WHERE code = ?", (code,)).fetchone()[0]
    by_day = conn.execute(
        "SELECT substr(clicked_at, 1, 10) AS day, COUNT(*) AS clicks FROM clicks "
        "WHERE code = ? GROUP BY day ORDER BY day", (code,)).fetchall()
    referrers = conn.execute(
        "SELECT COALESCE(referrer, 'direct') AS referrer, COUNT(*) AS clicks FROM clicks "
        "WHERE code = ? GROUP BY referrer ORDER BY clicks DESC LIMIT 10", (code,)).fetchall()
    return {
        "code": code,
        "target_url": row["target_url"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "total_clicks": total,
        "clicks_by_day": [dict(r) for r in by_day],
        "top_referrers": [dict(r) for r in referrers],
    }


@app.delete("/api/links/{code}", status_code=204)
def delete_link(code: str, request: Request, conn=Depends(db)):
    get_owned_link(code, request.headers.get("X-Owner-Key"), conn)
    conn.execute("DELETE FROM links WHERE code = ?", (code,))


@app.get("/{code}")
def redirect(code: str, request: Request, conn=Depends(db)):
    row = conn.execute("SELECT target_url, expires_at FROM links WHERE code = ?", (code,)).fetchone()
    if not row:
        raise HTTPException(404, "Short link not found")
    if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        raise HTTPException(410, "This link has expired")
    conn.execute(
        "INSERT INTO clicks (code, clicked_at, referrer, user_agent) VALUES (?, ?, ?, ?)",
        (code, now_iso(), request.headers.get("referer"), request.headers.get("user-agent", "")[:300]),
    )
    return RedirectResponse(row["target_url"], status_code=307)
