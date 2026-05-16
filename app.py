"""
lex-uwwz — Streamlit UI
Wyszukiwarka aktów wewnętrznych UW i WZ UW.
"""

import sqlite3
import re
from pathlib import Path

import streamlit as st

# --- Config ---
DB_PATH = "data/lex-uwwz.db"
APP_TITLE = "lex-uwwz"
APP_SUBTITLE = "Wyszukiwarka aktów wewnętrznych UW i WZ UW"


def get_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def search_fts(conn: sqlite3.Connection, query: str, organ: str = None,
               rok: int = None, limit: int = 50) -> list[dict]:
    """Full-text search with optional filters."""
    # Convert Polish query to FTS-friendly format
    # Add wildcards to each word for prefix matching (handles Polish inflection)
    words = query.strip().split()
    fts_query = " AND ".join(f"{w}*" for w in words if w)

    if not fts_query:
        return []

    sql = """
        SELECT a.*, rank
        FROM akty_fts f
        JOIN akty a ON a.id = f.rowid
        WHERE akty_fts MATCH ?
    """
    params = [fts_query]

    if organ and organ != "Wszystkie":
        sql += " AND a.organ = ?"
        params.append(organ)

    if rok:
        sql += " AND a.rok = ?"
        params.append(rok)

    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def browse_all(conn: sqlite3.Connection, organ: str = None,
               rok: int = None, limit: int = 100) -> list[dict]:
    """Browse all acts with optional filters."""
    sql = "SELECT * FROM akty WHERE 1=1"
    params = []

    if organ and organ != "Wszystkie":
        sql += " AND organ = ?"
        params.append(organ)

    if rok:
        sql += " AND rok = ?"
        params.append(rok)

    sql += " ORDER BY data_wydania DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_stats(conn: sqlite3.Connection) -> dict:
    """Get database statistics."""
    total = conn.execute("SELECT COUNT(*) FROM akty").fetchone()[0]
    with_text = conn.execute(
        "SELECT COUNT(*) FROM akty WHERE body_text IS NOT NULL"
    ).fetchone()[0]
    organs = conn.execute(
        "SELECT organ, COUNT(*) as cnt FROM akty GROUP BY organ ORDER BY cnt DESC"
    ).fetchall()
    years = conn.execute(
        "SELECT DISTINCT rok FROM akty ORDER BY rok DESC"
    ).fetchall()
    return {
        "total": total,
        "with_text": with_text,
        "organs": [dict(r) for r in organs],
        "years": [r["rok"] for r in years if r["rok"]],
    }


def highlight_snippet(text: str, query: str, context: int = 150) -> str:
    """Extract a snippet around the first match and highlight query terms."""
    if not text:
        return ""
    words = query.strip().split()
    pattern = "|".join(re.escape(w) for w in words)

    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        start = max(0, match.start() - context)
        end = min(len(text), match.end() + context)
        snippet = text[start:end]
        if start > 0:
            snippet = "..." + snippet
        if end < len(text):
            snippet = snippet + "..."
        # Bold the matched terms
        snippet = re.sub(
            f"({pattern})",
            r"**\1**",
            snippet,
            flags=re.IGNORECASE,
        )
        return snippet
    else:
        return text[:300] + "..." if len(text) > 300 else text


def render_result(akt: dict, query: str = ""):
    """Render a single search result."""
    organ_colors = {
        "Rektor": "🔴",
        "Senat": "🔵",
        "Kanclerz": "🟢",
        "Dziekan": "🟡",
        "KJD": "🟠",
    }
    icon = organ_colors.get(akt["organ"], "⚪")

    col1, col2 = st.columns([5, 1])
    with col1:
        st.markdown(f"### {icon} {akt['typ'].capitalize()} nr {akt.get('numer', '?')}")
        st.markdown(f"**{akt['tytul']}**")

        if query and akt.get("body_text"):
            snippet = highlight_snippet(akt["body_text"], query)
            if snippet:
                st.markdown(f"_{snippet}_")

    with col2:
        st.markdown(f"**{akt['organ']}**")
        st.markdown(f"{akt.get('data_wydania', '?')}")
        if akt.get("url_pdf"):
            st.markdown(f"[PDF]({akt['url_pdf']})")
        if akt.get("url_szczegoly"):
            st.markdown(f"[Monitor]({akt['url_szczegoly']})")

    st.divider()


# --- Main App ---
def main():
    st.set_page_config(
        page_title=APP_TITLE,
        page_icon="⚖️",
        layout="wide",
    )

    st.title(f"⚖️ {APP_TITLE}")
    st.caption(APP_SUBTITLE)

    # Check DB exists
    if not Path(DB_PATH).exists():
        st.error(
            f"Baza danych nie znaleziona: `{DB_PATH}`. "
            "Uruchom najpierw `python -m src.ingestion.monitor_uw`."
        )
        return

    conn = get_db()
    stats = get_stats(conn)

    # Sidebar
    with st.sidebar:
        st.header("Filtry")
        organs = ["Wszystkie"] + [o["organ"] for o in stats["organs"]]
        selected_organ = st.selectbox("Organ", organs)

        years = [None] + stats["years"]
        selected_year = st.selectbox(
            "Rok",
            years,
            format_func=lambda x: "Wszystkie" if x is None else str(x),
        )

        st.divider()
        st.header("Statystyki")
        st.metric("Aktów w bazie", stats["total"])
        st.metric("Z pełnym tekstem", stats["with_text"])
        for o in stats["organs"]:
            st.caption(f"{o['organ']}: {o['cnt']}")

    # Search
    query = st.text_input(
        "Szukaj w aktach prawnych",
        placeholder="np. środki trwałe, rekrutacja, program studiów...",
    )

    if query:
        results = search_fts(conn, query, selected_organ, selected_year)
        st.caption(f"Znaleziono: {len(results)} wyników")
        for akt in results:
            render_result(akt, query)
    else:
        st.caption("Przeglądaj najnowsze akty:")
        results = browse_all(conn, selected_organ, selected_year, limit=30)
        for akt in results:
            render_result(akt)

    conn.close()


if __name__ == "__main__":
    main()
