"""
Scraper for WZ UW (wz.uw.edu.pl/wydzial/akty-prawne/).

WZ UW is a WordPress site. The legal acts page contains direct links to
PDF/DOCX files in wp-content/uploads/. Each document is in a row with
a title in a "filename-column" div and a PDF link on an icon/image.

Document types are inferred from filename patterns:
- UCHWALA* -> uchwała (Rada Dydaktyczna / Rada Wydziału)
- ZARZADZENIE_KJD* -> zarządzenie KJD
- Zarzadzenie_Dziekana* -> zarządzenie Dziekana
- M.YYYY.* -> akt z Monitora UW (duplikat, skip or tag)
- Others -> dokument wewnętrzny WZ
"""

import re
import time
import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup
import pdfplumber

from src.db import get_db, text_hash, upsert_akt

logger = logging.getLogger(__name__)

BASE_URL = "https://wz.uw.edu.pl"
AKTY_URL = f"{BASE_URL}/wydzial/akty-prawne/"


@dataclass
class WzAktRaw:
    """Raw act parsed from WZ UW page."""
    tytul: str
    url_pdf: str
    filename: str
    organ: str
    typ: str


def classify_document(filename: str, tytul: str) -> tuple[str, str]:
    """Infer organ and type from filename/title patterns."""
    fn_lower = filename.lower()
    tytul_lower = tytul.lower()

    if fn_lower.startswith("zarzadzenie_dziekana") or "dziekan" in tytul_lower:
        return "Dziekan", "zarządzenie"
    if fn_lower.startswith("zarzadzenie_kjd") or "kjd" in fn_lower:
        return "KJD", "zarządzenie"
    if "zarzadzenie" in fn_lower or "zarządzeni" in tytul_lower:
        # Generic zarządzenie from WZ
        if "kjd" in tytul_lower or "kierownik" in tytul_lower:
            return "KJD", "zarządzenie"
        return "WZ", "zarządzenie"
    if fn_lower.startswith("uchwala") or "uchwał" in tytul_lower:
        if "rada dydaktyczn" in tytul_lower or "rady dydaktyczn" in tytul_lower:
            return "Rada Dydaktyczna", "uchwała"
        if "rada wydziału" in tytul_lower or "rady wydziału" in tytul_lower:
            return "Rada Wydziału", "uchwała"
        return "WZ", "uchwała"
    if fn_lower.startswith("m.") and re.match(r"m\.\d{4}\.", fn_lower):
        # This is a Monitor UW document mirrored on WZ site
        return "UW-mirror", "akt centralny"
    # Default
    return "WZ", "dokument"


def extract_numer_from_filename(filename: str) -> Optional[str]:
    """Try to extract act number from filename."""
    # Pattern: nr_1_02_2021 or nr-35 or NR_1
    m = re.search(r"nr[_-]?(\d+(?:[_/]\d+)?(?:[_/]\d{4})?)", filename, re.IGNORECASE)
    if m:
        return m.group(1).replace("_", "/")
    # Pattern: UCHWALA_nr_2_3_2021
    m = re.search(r"nr[_-](\d+)[_-](\d+)[_-](\d{4})", filename, re.IGNORECASE)
    if m:
        return f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    return None


def extract_year_from_title(tytul: str) -> Optional[int]:
    """Extract year from title text."""
    # Look for 4-digit year
    years = re.findall(r"20[12]\d", tytul)
    if years:
        return int(years[0])
    return None


def scrape_wz_page(client: httpx.Client) -> list[WzAktRaw]:
    """Scrape the WZ UW legal acts page."""
    resp = client.get(AKTY_URL)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    acts = []
    seen_urls = set()

    # Find all links to PDF/DOCX in wp-content/uploads
    for link in soup.find_all("a", href=re.compile(
        r"wz\.uw\.edu\.pl/wp-content/uploads/.*\.(pdf|docx)$", re.IGNORECASE
    )):
        url = link["href"]
        if url in seen_urls:
            continue
        seen_urls.add(url)

        filename = url.split("/")[-1]

        # Get title from surrounding context
        # Walk up to find the filename-column or nearest text container
        tytul = ""
        parent = link.parent
        for _ in range(5):
            if parent is None:
                break
            # Look for sibling or parent with filename-column class
            col = parent.find(class_=re.compile(r"filename-column"))
            if col:
                tytul = col.get_text(strip=True)
                break
            # Or look for any meaningful text in the row
            row_text = parent.get_text(strip=True)
            if row_text and len(row_text) > 10:
                # Clean up: remove icon characters and extra whitespace
                tytul = re.sub(r"\s+", " ", row_text).strip()
                if len(tytul) > 200:
                    tytul = tytul[:200]
                break
            parent = parent.parent

        if not tytul:
            # Fallback: humanize filename
            tytul = filename.replace("_", " ").replace("-", " ")
            tytul = re.sub(r"\.(pdf|docx)$", "", tytul, flags=re.IGNORECASE)

        organ, typ = classify_document(filename, tytul)

        acts.append(WzAktRaw(
            tytul=tytul,
            url_pdf=url,
            filename=filename,
            organ=organ,
            typ=typ,
        ))

    return acts


def download_pdf(client: httpx.Client, pdf_url: str, dest_dir: str,
                 filename: str) -> Optional[str]:
    """Download PDF and return local path."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    filepath = dest / filename

    if filepath.exists():
        return str(filepath)

    try:
        resp = client.get(pdf_url)
        resp.raise_for_status()
        filepath.write_bytes(resp.content)
        return str(filepath)
    except Exception as e:
        logger.warning(f"Błąd pobierania PDF {pdf_url}: {e}")
        return None


def extract_text_from_pdf(pdf_path: str) -> Optional[str]:
    """Extract text from PDF using pdfplumber."""
    if pdf_path.lower().endswith(".docx"):
        return None  # Skip DOCX for now
    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages_text = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages_text.append(text)
            return "\n\n".join(pages_text) if pages_text else None
    except Exception as e:
        logger.warning(f"Błąd ekstrakcji tekstu z {pdf_path}: {e}")
        return None


def run_ingestion(
    db_path: str = "data/lex-uwwz.db",
    pdf_dir: str = "data/pdfs/wz",
    download_pdfs: bool = True,
    delay: float = 1.0,
):
    """Main ingestion pipeline for WZ UW."""
    conn = get_db(db_path)
    log_cur = conn.execute(
        "INSERT INTO scrape_log (zrodlo, widok) VALUES ('wz_uw', 'akty-prawne')"
    )
    log_id = log_cur.lastrowid
    conn.commit()

    stats = {"found": 0, "new": 0, "updated": 0, "errors": 0, "skipped": 0}

    headers = {"User-Agent": "lex-uwwz/0.1 (academic research tool)"}

    with httpx.Client(headers=headers, follow_redirects=True, timeout=30.0) as client:
        logger.info("Scrapuję: WZ UW akty prawne")

        try:
            acts = scrape_wz_page(client)
        except Exception as e:
            logger.error(f"Błąd scrapowania WZ UW: {e}")
            conn.execute(
                "UPDATE scrape_log SET status='failed', end_time=datetime('now') WHERE id=?",
                (log_id,),
            )
            conn.commit()
            conn.close()
            return {"found": 0, "new": 0, "updated": 0, "errors": 1, "skipped": 0}

        stats["found"] = len(acts)
        logger.info(f"Znaleziono {len(acts)} dokumentów na stronie WZ UW")

        for akt in acts:
            try:
                # Skip Monitor UW mirrors (already in DB from monitor_uw scraper)
                if akt.organ == "UW-mirror":
                    logger.info(f"  [skip] Mirror UW: {akt.filename}")
                    stats["skipped"] += 1
                    continue

                numer = extract_numer_from_filename(akt.filename)
                rok = extract_year_from_title(akt.tytul)

                # Use filename as stable ID for WZ documents
                monitor_id = f"wz_{akt.filename}"

                data = {
                    "monitor_id": monitor_id,
                    "numer": numer,
                    "organ": akt.organ,
                    "typ": akt.typ,
                    "tytul": akt.tytul,
                    "data_wydania": None,  # WZ page doesn't show dates consistently
                    "rok": rok,
                    "url_szczegoly": AKTY_URL,
                    "url_pdf": akt.url_pdf,
                    "zrodlo": "wz_uw",
                }

                if download_pdfs:
                    time.sleep(delay)
                    pdf_path = download_pdf(client, akt.url_pdf, pdf_dir, akt.filename)
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
                           VALUES (?, 'nowy', 'Nowy akt WZ dodany do bazy')""",
                        (row_id,),
                    )
                    conn.commit()
                elif status == "updated":
                    stats["updated"] += 1

                logger.info(f"  [{status}] {akt.organ}: {akt.tytul[:70]}...")

            except Exception as e:
                logger.error(f"Błąd przetwarzania {akt.filename}: {e}")
                stats["errors"] += 1

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

    logger.info("=== lex-uwwz: Ingestion WZ UW ===")
    stats = run_ingestion(download_pdfs=download)
    logger.info(
        f"Gotowe. Znaleziono: {stats['found']}, nowe: {stats['new']}, "
        f"zmienione: {stats['updated']}, pominięte: {stats['skipped']}, "
        f"błędy: {stats['errors']}"
    )


if __name__ == "__main__":
    main()
