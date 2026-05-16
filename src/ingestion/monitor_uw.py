"""
Scraper for Monitor UW (monitor.uw.edu.pl).

Monitor UW is a SharePoint site with a single list "Uchway" filtered by views:
- Zarządzenia Rektora
- Uchwały Senatu
- Zarządzenia Kanclerza

Each view returns an HTML table with columns: Pozycja, Dane aktu prawnego, Rok, Data wydania.
Each act links to a detail page, which contains a link to the PDF attachment.
"""

import re
import time
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, asdict

import httpx
from bs4 import BeautifulSoup
import pdfplumber
import yaml

from src.db import get_db, text_hash, upsert_akt

logger = logging.getLogger(__name__)

BASE_URL = "https://monitor.uw.edu.pl"

VIEWS = {
    "zarzadzenia_rektora": {
        "path": "/lists/uchway/zarzdzenia%20rektora.aspx",
        "organ": "Rektor",
        "typ": "zarządzenie",
    },
    "uchway_senatu": {
        "path": "/lists/uchway/uchway%20senatu.aspx",
        "organ": "Senat",
        "typ": "uchwała",
    },
    "zarzadzenia_kanclerza": {
        "path": "/lists/uchway/zarzdzenia%20kanclerza.aspx",
        "organ": "Kanclerz",
        "typ": "zarządzenie",
    },
    "obwieszczenia": {
        "path": "/lists/uchway/obwieszczenia.aspx",
        "organ": "Rektor",
        "typ": "obwieszczenie",
    },
    "organizacyjne_regulaminy": {
        "path": "/lists/uchway/organizacyjne%20%20regulaminy.aspx",
        "organ": "Rektor",
        "typ": "zarządzenie",
    },
}


@dataclass
class AktRaw:
    """Raw act parsed from Monitor UW HTML."""
    monitor_id: str
    tytul: str
    rok: Optional[int]
    data_wydania: Optional[str]
    url_szczegoly: str
    organ: str
    typ: str


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def parse_date(date_str: str) -> Optional[str]:
    """Convert '20.04.2026' to '2026-04-20'."""
    date_str = date_str.strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def extract_numer(tytul: str) -> Optional[str]:
    """Extract act number from title, e.g. 'ZARZĄDZENIE NR 43' -> '43'."""
    m = re.search(r"NR\s+(\d+)", tytul, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def detect_typ_organ(tytul: str, default_typ: str, default_organ: str) -> tuple[str, str]:
    """Detect act type and organ from title text. Used for mixed-content views."""
    tytul_upper = tytul.upper()
    if tytul_upper.startswith("OBWIESZCZENIE"):
        typ = "obwieszczenie"
    elif tytul_upper.startswith("UCHWAŁA"):
        typ = "uchwała"
    elif tytul_upper.startswith("ZARZĄDZENIE"):
        typ = "zarządzenie"
    else:
        typ = default_typ

    if "KANCLERZA" in tytul_upper:
        organ = "Kanclerz"
    elif "SENATU" in tytul_upper:
        organ = "Senat"
    else:
        organ = default_organ

    return typ, organ


def scrape_list_page(client: httpx.Client, url: str, organ: str, typ: str) -> list[AktRaw]:
    """Scrape one page of the act list view. Returns list of AktRaw."""
    resp = client.get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    acts = []
    # SharePoint renders acts in table rows with class containing "ms-itmhover"
    rows = soup.select("tr.ms-itmhover")

    if not rows:
        # Fallback: look for any table rows with links to listform.aspx
        rows = soup.find_all("tr")

    for row in rows:
        cells = row.find_all("td")
        if len(cells) != 4:
            continue

        # Find the link to detail page
        link = row.find("a", href=re.compile(r"listform\.aspx"))
        if not link:
            continue

        tytul = link.get_text(strip=True)
        if not tytul:
            continue

        href = link["href"]
        if not href.startswith("http"):
            href = BASE_URL + href

        # Extract pozycja (first cell with a number)
        monitor_id = None
        for cell in cells:
            txt = cell.get_text(strip=True)
            if txt.isdigit():
                monitor_id = txt
                break

        if not monitor_id:
            continue

        # Extract rok and data wydania from last cells
        rok_text = cells[-2].get_text(strip=True) if len(cells) >= 3 else ""
        data_text = cells[-1].get_text(strip=True) if len(cells) >= 4 else ""

        rok = int(rok_text) if rok_text.isdigit() else None
        data_wydania = parse_date(data_text) if data_text else None

        # Detect actual typ/organ from title (for mixed-content views)
        act_typ, act_organ = detect_typ_organ(tytul, typ, organ)

        acts.append(AktRaw(
            monitor_id=monitor_id,
            tytul=tytul,
            rok=rok,
            data_wydania=data_wydania,
            url_szczegoly=href,
            organ=act_organ,
            typ=act_typ,
        ))

    return acts


def find_next_page_url(client: httpx.Client, url: str) -> Optional[str]:
    """Check if there's a 'Dalej' (Next) pagination link."""
    resp = client.get(url)
    soup = BeautifulSoup(resp.text, "lxml")
    next_link = soup.find("a", string=re.compile(r"Dalej|Next"))
    if next_link and next_link.get("href"):
        href = next_link["href"]
        if not href.startswith("http"):
            href = BASE_URL + href
        return href
    return None


def scrape_view(client: httpx.Client, view_key: str, view_config: dict,
                max_pages: int = 50) -> list[AktRaw]:
    """Scrape all pages of a Monitor UW view."""
    all_acts = []
    url = BASE_URL + view_config["path"]
    organ = view_config["organ"]
    typ = view_config["typ"]

    for page_num in range(1, max_pages + 1):
        logger.info(f"  Strona {page_num}: {url}")
        acts = scrape_list_page(client, url, organ, typ)
        if not acts:
            break
        all_acts.extend(acts)
        logger.info(f"  Znaleziono {len(acts)} aktów na stronie {page_num}")

        # Check for next page
        next_url = find_next_page_url(client, url)
        if next_url and next_url != url:
            url = next_url
            time.sleep(2)
        else:
            break

    return all_acts


def scrape_detail_page(client: httpx.Client, url: str) -> Optional[str]:
    """Scrape the detail page to find PDF URL."""
    try:
        resp = client.get(url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # Look for PDF attachment link
        pdf_link = soup.find("a", href=re.compile(r"\.pdf$", re.IGNORECASE))
        if pdf_link:
            href = pdf_link["href"]
            if not href.startswith("http"):
                href = BASE_URL + href
            return href

        # Fallback: look in attachment section
        for a in soup.find_all("a", href=True):
            if "/Attachments/" in a["href"] and a["href"].lower().endswith(".pdf"):
                href = a["href"]
                if not href.startswith("http"):
                    href = BASE_URL + href
                return href

    except Exception as e:
        logger.warning(f"Błąd pobierania strony szczegółów {url}: {e}")

    return None


def download_pdf(client: httpx.Client, pdf_url: str, dest_dir: str,
                 monitor_id: str) -> Optional[str]:
    """Download PDF and return local path."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    filename = f"{monitor_id}.pdf"
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
    pdf_dir: str = "data/pdfs",
    views: Optional[dict] = None,
    download_pdfs: bool = True,
    delay: float = 2.0,
):
    """Main ingestion pipeline for Monitor UW."""
    if views is None:
        views = VIEWS

    conn = get_db(db_path)
    log_cur = conn.execute(
        "INSERT INTO scrape_log (zrodlo, widok) VALUES ('monitor_uw', 'all')"
    )
    log_id = log_cur.lastrowid
    conn.commit()

    stats = {"found": 0, "new": 0, "updated": 0, "errors": 0}

    headers = {"User-Agent": "lex-uwwz/0.1 (academic research tool)"}

    with httpx.Client(headers=headers, follow_redirects=True, timeout=30.0) as client:
        for view_key, view_config in views.items():
            logger.info(f"Scrapuję: {view_key} ({view_config['organ']})")

            try:
                acts = scrape_view(client, view_key, view_config)
            except Exception as e:
                logger.error(f"Błąd scrapowania widoku {view_key}: {e}")
                stats["errors"] += 1
                continue

            stats["found"] += len(acts)

            for akt in acts:
                try:
                    numer = extract_numer(akt.tytul)
                    if numer and akt.rok:
                        numer_full = f"{numer}/{akt.rok}"
                    else:
                        numer_full = numer

                    data = {
                        "monitor_id": akt.monitor_id,
                        "numer": numer_full,
                        "organ": akt.organ,
                        "typ": akt.typ,
                        "tytul": akt.tytul,
                        "data_wydania": akt.data_wydania,
                        "rok": akt.rok,
                        "url_szczegoly": akt.url_szczegoly,
                        "zrodlo": "monitor_uw",
                    }

                    # Get PDF URL from detail page
                    if download_pdfs:
                        time.sleep(delay)
                        pdf_url = scrape_detail_page(client, akt.url_szczegoly)
                        if pdf_url:
                            data["url_pdf"] = pdf_url
                            time.sleep(delay)
                            pdf_path = download_pdf(
                                client, pdf_url, pdf_dir, akt.monitor_id
                            )
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
                               VALUES (?, 'nowy', 'Nowy akt dodany do bazy')""",
                            (row_id,),
                        )
                        conn.commit()
                    elif status == "updated":
                        stats["updated"] += 1

                    logger.info(
                        f"  [{status}] {akt.monitor_id}: {akt.tytul[:80]}..."
                    )

                except Exception as e:
                    logger.error(f"Błąd przetwarzania aktu {akt.monitor_id}: {e}")
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

    # Parse optional arguments
    download = "--no-download" not in sys.argv
    db_path = "data/lex-uwwz.db"
    pdf_dir = "data/pdfs"

    logger.info("=== lex-uwwz: Ingestion Monitor UW ===")
    stats = run_ingestion(
        db_path=db_path,
        pdf_dir=pdf_dir,
        download_pdfs=download,
    )
    logger.info(f"Gotowe. Znaleziono: {stats['found']}, nowe: {stats['new']}, "
                f"zmienione: {stats['updated']}, błędy: {stats['errors']}")


if __name__ == "__main__":
    main()
