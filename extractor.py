# extractor.py
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from bs4 import BeautifulSoup


# =========================
# Utils
# =========================
def make_soup(html: str) -> BeautifulSoup:
    # Pas besoin de lxml
    return BeautifulSoup(html, "html.parser")


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _parse_fr_number(raw: Optional[str]) -> Optional[float]:
    """
    Convertit: "3,12 %", "-0,34 %", "—" -> float (en %), ou None
    """
    if raw is None:
        return None
    s = _clean_text(raw).replace("\u202f", " ").replace("\xa0", " ")
    s = s.replace("%", "").strip()
    if s in {"", "—", "-", "N/A", "n/a"}:
        return None
    s = s.replace(" ", "").replace(",", ".")
    m = re.search(r"[-+]?\d+(\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def read_html(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


# =========================
# Morningstar
# =========================
def extract_morningstar_name(soup: BeautifulSoup) -> Optional[str]:
    node = soup.select_one('h1 span[itemprop="name"]')
    if node:
        return _clean_text(node.get_text(" ", strip=True))
    if soup.title and soup.title.string:
        return _clean_text(_clean_text(soup.title.string).split("|")[0])
    return None


def extract_morningstar_stars(soup: BeautifulSoup) -> Optional[float]:
    # FR: "Morningstar Rating 3 sur 5 étoiles"
    for el in soup.find_all(attrs={"aria-label": True}):
        aria = el.get("aria-label") or ""
        m = re.search(r"Morningstar Rating\s+(\d+(?:\.\d+)?)\s+sur\s+5", aria, flags=re.I)
        if m:
            return float(m.group(1))

    # EN fallback: "rating of 4 out of 5 stars"
    for el in soup.find_all(attrs={"aria-label": True}):
        aria = el.get("aria-label") or ""
        m = re.search(r"rating of\s+(\d+(?:\.\d+)?)\s+out of\s+5\s+stars", aria, flags=re.I)
        if m:
            return float(m.group(1))

    return None


def _table_to_matrix(table) -> Tuple[list[str], list[list[str]]]:
    headers: list[str] = []
    thead = table.find("thead")
    if thead:
        headers = [_clean_text(th.get_text(" ", strip=True)) for th in thead.find_all(["th", "td"])]

    rows: list[list[str]] = []
    tbody = table.find("tbody") or table
    for tr in tbody.find_all("tr"):
        cells = [_clean_text(td.get_text(" ", strip=True)) for td in tr.find_all(["th", "td"])]
        if any(cells):
            rows.append(cells)

    return headers, rows


def _find_value_in_any_table(
    soup: BeautifulSoup,
    row_label_candidates: list[str],
    col_label_candidates: list[str],
) -> Optional[str]:
    row_l = [r.lower() for r in row_label_candidates]
    col_l = [c.lower() for c in col_label_candidates]

    for table in soup.find_all("table"):
        headers, rows = _table_to_matrix(table)
        if not headers or not rows:
            continue

        headers_l = [h.lower() for h in headers]

        col_idx = None
        for c in col_l:
            if c in headers_l:
                col_idx = headers_l.index(c)
                break
        if col_idx is None:
            continue

        for r in rows:
            if not r:
                continue
            if r[0].lower() in row_l and col_idx < len(r):
                return r[col_idx]

    return None


def extract_morningstar_performances(soup: BeautifulSoup) -> Dict[str, Optional[float]]:
    row_fund = ["Fonds", "Fund"]

    perf_4w = _find_value_in_any_table(
        soup, row_fund, ["4 sem.", "4 sem", "4 weeks", "4w", "1 mois", "1 month", "1m"]
    )
    perf_ytd = _find_value_in_any_table(
        soup, row_fund, ["YTD", "year to date", "depuis le début", "depuis le début de l'année"]
    )
    perf_1y = _find_value_in_any_table(soup, row_fund, ["1 an", "1 year", "1y"])
    perf_3y = _find_value_in_any_table(soup, row_fund, ["3 ans", "3 years", "3 year", "3y"])

    return {
        "perf_4_semaines": _parse_fr_number(perf_4w),
        "perf_depuis_1er_janvier": _parse_fr_number(perf_ytd),
        "perf_1_an": _parse_fr_number(perf_1y),
        "perf_3_ans": _parse_fr_number(perf_3y),
    }


def parse_morningstar_html_file(html_path: Path) -> Dict[str, Any]:
    soup = make_soup(read_html(html_path))
    return {
        "site": "morningstar",
        "name": extract_morningstar_name(soup),
        "stars": extract_morningstar_stars(soup),
        **extract_morningstar_performances(soup),
        "source_file": html_path.name,
    }


# =========================
# Quantalys
# =========================
def extract_quantalys_name(soup: BeautifulSoup) -> Optional[str]:
    node = soup.select_one("h1 strong")
    if node:
        return _clean_text(node.get_text(" ", strip=True))
    h1 = soup.find("h1")
    return _clean_text(h1.get_text(" ", strip=True)) if h1 else None


def extract_quantalys_stars(soup: BeautifulSoup) -> Optional[int]:
    """
    Notation rendue via:
      <div class="spritefonds sprite-5g ...">
    ou via une image "qt-star-x-y.png"
    """
    sprite = soup.select_one(".spritefonds")
    if sprite:
        classes = " ".join(sprite.get("class", []))
        m = re.search(r"sprite-(\d)g", classes)
        if m:
            return int(m.group(1))

    img = soup.find("img", src=re.compile(r"qt-star-(\d)-(\d)\.png"))
    if img and img.get("src"):
        m = re.search(r"qt-star-(\d)-(\d)\.png", img["src"])
        if m:
            return int(m.group(1))

    return None


def extract_quantalys_performances(soup: BeautifulSoup) -> Dict[str, Optional[float]]:
    """
    Quantalys: performances dans un tableau de classe:
      table table-condensed-max table-hover
    On prend la colonne "Fonds" (détectée via l'en-tête).
    """

    tables = soup.select("table.table.table-condensed-max.table-hover")
    if not tables:
        return {
            "perf_4_semaines": None,
            "perf_depuis_1er_janvier": None,
            "perf_1_an": None,
            "perf_3_ans": None,
        }

    def table_has_perf_labels(t) -> bool:
        txt = _clean_text(t.get_text(" ", strip=True)).lower()
        return (
            "perf. 4 semaines" in txt
            or "perf. 1er janvier" in txt
            or "perf. 1 an" in txt
            or "perf. 3 ans" in txt
        )

    perf_table = next((t for t in tables if table_has_perf_labels(t)), tables[0])

    # index colonne Fonds
    idx_fonds = 1
    header_cells = []
    thead = perf_table.find("thead")
    if thead:
        header_cells = thead.find_all(["th", "td"])
    else:
        first_tr = perf_table.find("tr")
        if first_tr:
            header_cells = first_tr.find_all(["th", "td"])

    if header_cells:
        labels = [_clean_text(c.get_text(" ", strip=True)).lower() for c in header_cells]
        for i, lab in enumerate(labels):
            if lab == "fonds":
                idx_fonds = i
                break

    patterns = {
        "perf_4_semaines": re.compile(r"^perf\.\s*4\s*semaines$", re.I),
        "perf_depuis_1er_janvier": re.compile(r"^perf\.\s*1er\s*janvier$", re.I),
        "perf_1_an": re.compile(r"^perf\.\s*1\s*an$", re.I),
        "perf_3_ans": re.compile(r"^perf\.\s*3\s*ans$", re.I),
    }

    out_raw: Dict[str, Optional[str]] = {k: None for k in patterns}

    for tr in perf_table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) <= idx_fonds:
            continue

        label = _clean_text(cells[0].get_text(" ", strip=True))
        value = _clean_text(cells[idx_fonds].get_text(" ", strip=True))

        for key, rx in patterns.items():
            if rx.match(label):
                out_raw[key] = value

    return {k: _parse_fr_number(v) for k, v in out_raw.items()}


def parse_quantalys_html_file(html_path: Path) -> Dict[str, Any]:
    soup = make_soup(read_html(html_path))
    return {
        "site": "quantalys",
        "name": extract_quantalys_name(soup),
        "stars": extract_quantalys_stars(soup),
        **extract_quantalys_performances(soup),
        "source_file": html_path.name,
    }


# =========================
# Détection + exécution
# =========================
def detect_site(html: str) -> Optional[str]:
    h = html.lower()
    if "quantalys" in h:
        return "quantalys"
    if "morningstar" in h:
        return "morningstar"
    return None


def parse_any_html_file(html_path: Path) -> Dict[str, Any]:
    html = read_html(html_path)
    site = detect_site(html)

    if site == "quantalys":
        return parse_quantalys_html_file(html_path)
    if site == "morningstar":
        return parse_morningstar_html_file(html_path)

    return {
        "site": None,
        "name": None,
        "stars": None,
        "perf_4_semaines": None,
        "perf_depuis_1er_janvier": None,
        "perf_1_an": None,
        "perf_3_ans": None,
        "source_file": html_path.name,
        "error": "Site non reconnu",
    }


if __name__ == "__main__":
    script_dir = Path(__file__).resolve().parent
    captures_dir = script_dir / "captures"

    html_files = sorted(captures_dir.glob("*.html"))
    if not html_files:
        print(f"Aucun fichier .html trouvé dans: {captures_dir}")
        raise SystemExit(1)

    for p in html_files:
        data = parse_any_html_file(p)
        print(f"\n=== {p.name} ===")
        print(json.dumps(data, ensure_ascii=False, indent=2))
