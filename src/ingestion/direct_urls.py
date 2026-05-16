"""
Config-driven scraper for specific act URLs (direct_urls).

Unlike monitor_uw.py (which scrapes list pages) and wz_uw.py (which scrapes
the WZ legal acts page), this scraper reads a YAML registry of specific
act URLs — typically foundational documents like Statut UW, Regulamin Studiów,
or specific WZ regulations that don't appear in automated scraper results.

Usage:
    python -m src.ingestion.direct_urls [--config direct_urls.yaml] [--no-download]
"""

import logging
import time
from pathlib import Path
from typing import Optional

import httpx
import pdfplumber
import yaml

from src.db import get_db, text_hash, upsert_akt

logger = logging.getLogger(__name__)


def load_registry(config_path: str = "direct_urls.yaml") -> list[dict]:
    """Load act registry from YAML file."""
    path = Path(config_path)
    if not path.exists():
        logger.error(f"Nie znaleziono pliku rejestru: {config_path}")
        return []

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    akty = data.get("akty", [])
    # Filter out entries without url_pdf
    valid = [a for a in akty if a.get("url_pdf")]
    logger.info(f"Załadowano {len(valid)} aktów z rejestru ({len(akty)} łącznie, "
                f"{len(akty) - len(valid)} bez URL)")
    return valid


def download_pdf(client: httpx.Client, url: str, dest_dir: str,
                 act_id: str) -> Optional[str]:
    """Download PDF to local storage. Returns local path or None."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    # Derive filename from act_id
    filename = f"{act_id}.pdf"
    filepath = dest / filename

    if filepath.exists():
        logger.info(f"  PDF już istnieje: {filepath}")
        return str(filepath)

    try:
        resp = client.get(url)
        resp.raise_for_status()
        filepath.write_bytes(resp.content)
        logger.info(f"  Pobrano PDF: {filepath} ({len(resp.content) / 1024:.0f} KB)")
        return str(filepath)
    except Exception as e:
        logger.warning(f"  Błąd pobierania PDF {url}: {e}")
        return None


def extract_text_from_pdf(pdf_path: str) -> Optional[str]:
    """Extract text from PDF using pdfplumber."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages_text = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages_text.append(text)
            full_text = "\n\n".join(pages_text) if pages_text else None
            if full_text:
                logger.info(f"  Wyekstrahowano {len(full_text)} znaków z {len(pdf.pages)} stron")
            else:
                logger.warning(f"  Brak tekstu w PDF (skan?): {pdf_path}")
            return full_text
    except Exception as e:
        logger.warning(f"  Błąd ekstrakcji tekstu z {pdf_path}: {e}")
        return None


def extract_numer(tytul: str) -> Optional[str]:
    """Extract act number from title."""
    import re
    # Pattern: NR 443, nr 67, NR 2/3/2022
    m = re.search(r"NR\s+([\d/]+)", tytul, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def run_ingestion(
    config_path: str = "direct_urls.yaml",
    db_path: str = "data/lex-uwwz.db",
    pdf_dir: str = "data/pdfs/direct",
    download_pdfs: bool = True,
    delay: float = 2.0,
):
    """Main ingestion pipeline for direct URL registry."""
    akty = load_registry(config_path)
    if not akty:
        logger.warning("Brak aktów do przetworzenia")
        return {"found": 0, "new": 0, "updated": 0, "errors": 0, "skipped": 0}

    conn = get_db(db_path)
    log_cur = conn.execute(
        "INSERT INTO scrape_log (zrodlo, widok) VALUES ('direct_urls', 'registry')"
    )
    log_id = log_cur.lastrowid
    conn.commit()

    stats = {"found": len(akty), "new": 0, "updated": 0, "errors": 0, "skipped": 0}

    headers = {"User-Agent": "lex-uwwz/0.1 (academic research tool)"}

    with httpx.Client(headers=headers, follow_redirects=True, timeout=60.0) as client:
        for akt in akty:
            act_id = akt["id"]
            tytul = akt["tytul"]
            logger.info(f"Przetwarzam: {act_id} — {tytul[:80]}...")

            try:
                numer = extract_numer(tytul)
                rok = akt.get("rok")
                if numer and rok and "/" not in numer:
                    numer_full = f"{numer}/{rok}"
                else:
                    numer_full = numer

                data = {
                    "monitor_id": act_id,
                    "numer": numer_full,
                    "organ": akt["organ"],
                    "typ": akt["typ"],
                    "tytul": tytul,
                    "data_wydania": akt.get("data_wydania"),
                    "rok": rok,
                    "url_szczegoly": akt.get("url_page", ""),
                    "url_pdf": akt["url_pdf"],
                    "zrodlo": "direct_urls",
                }

                if download_pdfs and akt.get("url_pdf"):
                    time.sleep(delay)
                    pdf_path = download_pdf(client, akt["url_pdf"], pdf_dir, act_id)
                    if pdf_path:
                        data["pdf_path"] = pdf_path
                        body = extract_text_from_pdf(pdf_path)
                        if body:
                            data["body_text"] = body
                            data["body_hash"] = text_hash(body)

                row_id, status = upsert_akt(conn, data)

                if status == "new":
                    stats["new"] += 1
                    conn.execute(
                        """INSERT INTO zmiany (akt_id, typ_zmiany, opis)
                           VALUES (?, 'nowy', 'Akt dodany z rejestru direct_urls')""",
                        (row_id,),
                    )
                    conn.commit()
                elif status == "updated":
                    stats["updated"] += 1

                logger.info(f"  [{status}] {act_id}")

            except Exception as e:
                logger.error(f"Błąd przetwarzania {act_id}: {e}")
                stats["errors"] += 1

    # Update scrape log
    conn.execute(
        """UPDATE scrape_log SET
           end_time=datetime('now'), akty_znalezione=?, akty_nowe=?,
           akty_zmienione=?, bledy=?, status='completed'
           WHERE id=?""",
        (stats["found"], stats["new"], stats["updated"], stats["errors"], log_id),
    )
    conn.commit()
    conn.close()

    return stats


def main():
    """CLI entry point."""
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    download = "--no-download" not in sys.argv
    config = "direct_urls.yaml"
    db_path = "data/lex-uwwz.db"
    pdf_dir = "data/pdfs/direct"

    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--config" and i + 1 < len(args):
            config = args[i + 1]
        if a == "--db" and i + 1 < len(args):
            db_path = args[i + 1]

    logger.info("=== lex-uwwz: Ingestion direct_urls ===")
    stats = run_ingestion(
        config_path=config,
        db_path=db_path,
        pdf_dir=pdf_dir,
        download_pdfs=download,
    )
    logger.info(
        f"Gotowe. Znaleziono: {stats['found']}, nowe: {stats['new']}, "
        f"zmienione: {stats['updated']}, błędy: {stats['errors']}"
    )


if __name__ == "__main__":
    main()
