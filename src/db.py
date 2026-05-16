"""Database layer for lex-uwwz — SQLite with FTS5."""

import sqlite3
import hashlib
from pathlib import Path
from typing import Optional


DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS akty (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id TEXT,               -- pozycja w Monitorze UW (np. "108")
    numer TEXT,                    -- numer aktu (np. "43/2026")
    organ TEXT NOT NULL,           -- Rektor | Senat | Kanclerz | Dziekan | KJD | Rada Wydziału
    typ TEXT NOT NULL,             -- zarządzenie | uchwała | pismo okólne | obwieszczenie
    tytul TEXT NOT NULL,
    data_wydania TEXT,             -- ISO format YYYY-MM-DD
    rok INTEGER,
    url_szczegoly TEXT,            -- link do strony szczegółów
    url_pdf TEXT,                  -- link do PDF
    pdf_path TEXT,                 -- lokalna ścieżka do PDF
    body_text TEXT,                -- wyekstrahowany tekst z PDF
    body_hash TEXT,                -- SHA-256 znormalizowanego tekstu
    zrodlo TEXT NOT NULL,          -- "monitor_uw" | "wz_uw"
    status TEXT DEFAULT 'obowiązujący',  -- obowiązujący | uchylony | zmieniony
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(zrodlo, monitor_id)
);

CREATE TABLE IF NOT EXISTS zmiany (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    akt_id INTEGER NOT NULL REFERENCES akty(id),
    typ_zmiany TEXT NOT NULL,      -- nowy | zmiana_tekstu | zmiana_statusu | nowy_akt_zmieniajacy
    opis TEXT,
    stary_hash TEXT,
    nowy_hash TEXT,
    wykryto TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scrape_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zrodlo TEXT NOT NULL,
    widok TEXT,
    start_time TEXT DEFAULT (datetime('now')),
    end_time TEXT,
    akty_znalezione INTEGER DEFAULT 0,
    akty_nowe INTEGER DEFAULT 0,
    akty_zmienione INTEGER DEFAULT 0,
    bledy INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running'  -- running | completed | failed
);

-- FTS5 index for full-text search
CREATE VIRTUAL TABLE IF NOT EXISTS akty_fts USING fts5(
    tytul,
    body_text,
    content='akty',
    content_rowid='id',
    tokenize='unicode61'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS akty_ai AFTER INSERT ON akty BEGIN
    INSERT INTO akty_fts(rowid, tytul, body_text)
    VALUES (new.id, new.tytul, new.body_text);
END;

CREATE TRIGGER IF NOT EXISTS akty_ad AFTER DELETE ON akty BEGIN
    INSERT INTO akty_fts(akty_fts, rowid, tytul, body_text)
    VALUES ('delete', old.id, old.tytul, old.body_text);
END;

CREATE TRIGGER IF NOT EXISTS akty_au AFTER UPDATE ON akty BEGIN
    INSERT INTO akty_fts(akty_fts, rowid, tytul, body_text)
    VALUES ('delete', old.id, old.tytul, old.body_text);
    INSERT INTO akty_fts(rowid, tytul, body_text)
    VALUES (new.id, new.tytul, new.body_text);
END;
"""


def get_db(db_path: str = "data/lex-uwwz.db") -> sqlite3.Connection:
    """Open (or create) the database and ensure schema exists."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(DB_SCHEMA)
    return conn


def text_hash(text: Optional[str]) -> Optional[str]:
    """SHA-256 of normalized text for change detection."""
    if not text:
        return None
    normalized = " ".join(text.split()).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def upsert_akt(conn: sqlite3.Connection, data: dict) -> tuple[int, str]:
    """Insert or update an akt. Returns (row_id, 'new'|'updated'|'unchanged')."""
    cur = conn.execute(
        "SELECT id, body_hash FROM akty WHERE zrodlo = ? AND monitor_id = ?",
        (data["zrodlo"], data["monitor_id"]),
    )
    existing = cur.fetchone()

    if existing is None:
        cur = conn.execute(
            """INSERT INTO akty
               (monitor_id, numer, organ, typ, tytul, data_wydania, rok,
                url_szczegoly, url_pdf, pdf_path, body_text, body_hash, zrodlo)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data["monitor_id"],
                data.get("numer"),
                data["organ"],
                data["typ"],
                data["tytul"],
                data.get("data_wydania"),
                data.get("rok"),
                data.get("url_szczegoly"),
                data.get("url_pdf"),
                data.get("pdf_path"),
                data.get("body_text"),
                data.get("body_hash"),
                data["zrodlo"],
            ),
        )
        conn.commit()
        return cur.lastrowid, "new"

    row_id = existing["id"]
    old_hash = existing["body_hash"]
    new_hash = data.get("body_hash")

    if new_hash and old_hash != new_hash:
        conn.execute(
            """UPDATE akty SET
               tytul=?, body_text=?, body_hash=?, pdf_path=?,
               url_pdf=?, updated_at=datetime('now')
               WHERE id=?""",
            (
                data["tytul"],
                data.get("body_text"),
                new_hash,
                data.get("pdf_path"),
                data.get("url_pdf"),
                row_id,
            ),
        )
        conn.execute(
            """INSERT INTO zmiany (akt_id, typ_zmiany, opis, stary_hash, nowy_hash)
               VALUES (?, 'zmiana_tekstu', 'Wykryto zmianę treści PDF', ?, ?)""",
            (row_id, old_hash, new_hash),
        )
        conn.commit()
        return row_id, "updated"

    return row_id, "unchanged"


def search_fts(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[dict]:
    """Full-text search over akty."""
    rows = conn.execute(
        """SELECT a.*, rank
           FROM akty_fts f
           JOIN akty a ON a.id = f.rowid
           WHERE akty_fts MATCH ?
           ORDER BY rank
           LIMIT ?""",
        (query, limit),
    ).fetchall()
    return [dict(r) for r in rows]
