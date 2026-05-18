"""
Export akty from SQLite to JSON for the standalone HTML UI.
Includes chunk-level indexing for precise BM25 search and RAG retrieval.

Usage:
    python export_json_v2.py [--db data/lex-uwwz.db] [--output data/akty.json]
"""

import json
import re
import sqlite3
import sys
from pathlib import Path
from datetime import datetime


# ── Chunking ──────────────────────────────────────────────────

# Regex patterns mirroring parseActStructure() in lex-uwwz.html
_RE_SECTION = re.compile(
    r"^(Rozdział|ROZDZIAŁ|Dział|DZIAŁ)\s+([IVXLCDM\d]+\.?)\s*(.*)", re.IGNORECASE
)
_RE_PARAGRAPH = re.compile(r"^§\s*(\d+[\w]*\.?)")
_RE_ARTICLE = re.compile(r"^(Art\.|Artykuł)\s*(\d+[\w]*\.?)", re.IGNORECASE)

MAX_CHUNK_CHARS = 3000   # hard ceiling per chunk
MIN_CHUNK_CHARS = 200    # merge tiny leftovers into previous chunk


def chunk_act_structural(act_id, body_text):
    """Split body_text using legal structure (§, Rozdział, Art.).
    Returns list of chunk dicts with metadata."""
    lines = body_text.split("\n")
    raw_blocks = []  # {section, ref, text}
    current_section = None
    current_ref = None
    current_lines = []

    def flush():
        text = "\n".join(current_lines).strip()
        if text:
            raw_blocks.append({
                "section": current_section,
                "ref": current_ref,
                "text": text,
            })

    for line in lines:
        stripped = line.strip()
        if not stripped:
            current_lines.append("")
            continue

        s_match = _RE_SECTION.match(stripped)
        if s_match:
            flush()
            current_section = (
                s_match.group(1) + " " + s_match.group(2)
                + (" " + s_match.group(3) if s_match.group(3) else "")
            ).strip()
            current_ref = None
            current_lines = [stripped]
            continue

        p_match = _RE_PARAGRAPH.match(stripped) or _RE_ARTICLE.match(stripped)
        if p_match:
            flush()
            current_ref = p_match.group(0).strip()
            current_lines = [stripped]
            continue

        current_lines.append(line)

    flush()

    if not raw_blocks:
        return []

    # Merge blocks that are too small; split blocks that are too large
    chunks = []
    for blk in raw_blocks:
        text = blk["text"]
        section = blk["section"]
        ref = blk["ref"]

        if len(text) <= MAX_CHUNK_CHARS:
            # Try to merge tiny block into previous chunk if same section
            if (
                len(text) < MIN_CHUNK_CHARS
                and chunks
                and chunks[-1]["act_id"] == act_id
                and chunks[-1]["section"] == section
                and len(chunks[-1]["text"]) + len(text) <= MAX_CHUNK_CHARS
            ):
                chunks[-1]["text"] += "\n\n" + text
                if ref and not chunks[-1]["ref"]:
                    chunks[-1]["ref"] = ref
            else:
                chunks.append({
                    "act_id": act_id,
                    "section": section,
                    "ref": ref,
                    "text": text,
                })
        else:
            # Large block → split by line accumulation (chunkText pattern)
            sub_chunks = _chunk_by_lines(text, MAX_CHUNK_CHARS)
            for i, sc in enumerate(sub_chunks):
                chunks.append({
                    "act_id": act_id,
                    "section": section,
                    "ref": ref if i == 0 else (ref + " (cd.)" if ref else None),
                    "text": sc,
                })

    return chunks


def _chunk_by_lines(text, max_chars):
    """Fallback line-based chunking (transcript-tool pattern)."""
    lines = text.split("\n")
    result = []
    current = ""
    for line in lines:
        if current and len(current) + len(line) + 1 > max_chars:
            result.append(current)
            current = ""
        current += ("\n" if current else "") + line
    if current:
        result.append(current)
    return result


def chunk_act_fallback(act_id, body_text):
    """Line-based chunking for acts without legal structure markers."""
    parts = _chunk_by_lines(body_text, MAX_CHUNK_CHARS)
    return [
        {"act_id": act_id, "section": None, "ref": None, "text": p}
        for p in parts
    ]


def chunk_act(act_id, body_text):
    """Smart chunking: structural if markers found, else line-based."""
    if not body_text or len(body_text.strip()) < 50:
        return []

    # Check if text has structural markers (search full text, not just prefix)
    _re_par_multi = re.compile(r"^§\s*\d", re.MULTILINE)
    _re_sec_multi = re.compile(r"^(Rozdział|ROZDZIAŁ|Dział|DZIAŁ)\s+[IVXLCDM\d]", re.MULTILINE | re.IGNORECASE)
    _re_art_multi = re.compile(r"^(Art\.|Artykuł)\s*\d", re.MULTILINE | re.IGNORECASE)
    has_structure = bool(
        _re_sec_multi.search(body_text)
        or _re_par_multi.search(body_text)
        or _re_art_multi.search(body_text)
    )

    if has_structure:
        chunks = chunk_act_structural(act_id, body_text)
        if chunks:
            return chunks

    return chunk_act_fallback(act_id, body_text)


# ── Export ────────────────────────────────────────────────────

def export(db_path, output_path):
    if not Path(db_path).exists():
        print(f"Baza nie znaleziona: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT id, monitor_id, numer, organ, typ, tytul,
               data_wydania, rok, url_szczegoly, url_pdf,
               body_text, zrodlo, status
        FROM akty
        ORDER BY data_wydania DESC, id DESC
    """).fetchall()

    akty = []
    all_chunks = []
    chunk_id = 0

    for r in rows:
        akt = dict(r)
        body = akt.get("body_text") or ""

        # Chunk the full body_text
        act_chunks = chunk_act(akt["id"], body)
        for c in act_chunks:
            c["chunk_id"] = chunk_id
            chunk_id += 1
        all_chunks.extend(act_chunks)

        akty.append(akt)

    stats_row = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN body_text IS NOT NULL THEN 1 ELSE 0 END) as with_text
        FROM akty
    """).fetchone()

    # Chunk stats
    structural_count = sum(1 for c in all_chunks if c.get("ref") or c.get("section"))
    avg_len = (
        sum(len(c["text"]) for c in all_chunks) / len(all_chunks)
        if all_chunks else 0
    )

    output = {
        "version": "2.0",
        "exported_at": datetime.now().isoformat(),
        "stats": {
            "total": stats_row["total"],
            "with_text": stats_row["with_text"],
            "chunks": len(all_chunks),
            "chunks_structural": structural_count,
            "chunk_avg_chars": int(avg_len),
        },
        "akty": akty,
        "chunks": all_chunks,
    }

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=None)

    size_mb = Path(output_path).stat().st_size / 1024 / 1024
    print(f"Wyeksportowano {len(akty)} aktów, {len(all_chunks)} chunków do {output_path} ({size_mb:.1f} MB)")
    print(f"  Chunki strukturalne: {structural_count}, fallback: {len(all_chunks) - structural_count}")
    print(f"  Średnia długość chunka: {int(avg_len)} znaków")

    # Also generate .js wrapper for file:// protocol (no server needed)
    js_path = Path(output_path).with_suffix(".js")
    with open(js_path, "w", encoding="utf-8") as f:
        f.write("window.AKTY_DATA = ")
        json.dump(output, f, ensure_ascii=False, indent=None)
        f.write(";\n")
    js_mb = js_path.stat().st_size / 1024 / 1024
    print(f"  + {js_path} ({js_mb:.1f} MB) — do otwierania po file://")

    conn.close()


if __name__ == "__main__":
    db = "data/lex-uwwz.db"
    out = "data/akty.json"

    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--db" and i + 1 < len(args):
            db = args[i + 1]
        if a == "--output" and i + 1 < len(args):
            out = args[i + 1]

    export(db, out)
