# WZ UW IntraLex

Wyszukiwarka aktów wewnętrznych Uniwersytetu Warszawskiego i Wydziału Zarządzania UW.

**[Otwórz aplikację](https://piotrnawrocki.github.io/lex-uwwz/)** (GitHub Pages)

## Funkcje

- Pełnotekstowe wyszukiwanie 155 aktów prawnych (BM25 po chunkach)
- Q&A w języku naturalnym z cytatami ze źródeł (wymaga window.ai / Chrome Built-in AI)
- Odnośniki krzyżowe między aktami
- Nawigacja po strukturze aktu (rozdziały, paragrafy)
- Kategorie tematyczne, zakładki, eksport do PDF
- Tryb ciemny, PL/EN

## Źródła danych

| Źródło | Organ | Aktów |
|--------|-------|-------|
| Monitor UW | Rektor, Senat, Kanclerz | 113 |
| WZ UW | Dziekan, KJD, Rada Dydaktyczna | 24 |
| Dokumenty bezpośrednie | Statut, regulaminy | 18 |

## Architektura

Aplikacja jest w pełni kliencka — jeden plik HTML + dane w `data/akty.js`. Nie wymaga serwera.

Backend (scrapery, eksport) wymaga Pythona:

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Ingestion
python -m src.ingestion.monitor_uw
python -m src.ingestion.wz_uw
python -m src.ingestion.direct_urls

# Eksport do JSON/JS
python src/export_json.py
```

## Licencja

MIT — patrz [LICENSE](LICENSE)
