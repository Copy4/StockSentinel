import re
from bs4 import BeautifulSoup
from pathlib import Path
from typing import Optional, Dict, Any


def _clean_text(s: str) -> str:
    return (
        s.replace("\u202f", " ")  # narrow no-break space
         .replace("\xa0", " ")   # no-break space
         .strip()
    )


def _parse_fr_number(s: str) -> Optional[float]:
    """
    Convertit '2,75', '2,75 %', '—' -> float ou None.
    """
    s = _clean_text(s).replace("%", "").strip()
    if s in {"—", "-", ""}:
        return None
    s = s.replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def extract_star_rating(soup: BeautifulSoup) -> Optional[float]:
    """
    Cherche aria-label du type: "rating of 5 out of 5 stars"
    """
    el = soup.find(attrs={"aria-label": re.compile(r"rating of\s+\d+(\.\d+)?\s+out of\s+5\s+stars", re.I)})
    if not el:
        return None

    aria = el.get("aria-label", "")
    m = re.search(r"rating of\s+(\d+(?:\.\d+)?)\s+out of\s+5\s+stars", aria, flags=re.I)
    return float(m.group(1)) if m else None


def _table_to_matrix(table) -> tuple[list[str], list[list[str]]]:
    headers = [_clean_text(th.get_text(" ", strip=True)) for th in table.select("thead th")]
    rows = []
    for tr in table.select("tbody tr"):
        cells = [_clean_text(td.get_text(" ", strip=True)) for td in tr.find_all(["th", "td"])]
        if cells:
            rows.append(cells)
    return headers, rows


def _find_value_in_any_table(
    soup: BeautifulSoup,
    row_label_candidates: list[str],
    col_label_candidates: list[str],
) -> Optional[str]:
    """
    Parcourt toutes les tables et tente de trouver:
    - une ligne dont la 1ère cellule == (Fonds / Fund / etc.)
    - une colonne dont le header == (YTD / 1 an / 3 ans / etc.)
    Retourne la valeur brute (string) si trouvée.
    """
    row_label_candidates_l = [r.lower() for r in row_label_candidates]
    col_label_candidates_l = [c.lower() for c in col_label_candidates]

    for table in soup.find_all("table"):
        headers, rows = _table_to_matrix(table)
        if not headers or not rows:
            continue

        headers_l = [h.lower() for h in headers]

        # cherche une colonne compatible
        col_idx = None
        for c in col_label_candidates_l:
            if c in headers_l:
                col_idx = headers_l.index(c)
                break
        if col_idx is None:
            continue

        # cherche la ligne (Fonds / Fund)
        for r in rows:
            if not r:
                continue
            if r[0].lower() in row_label_candidates_l:
                if col_idx < len(r):
                    return r[col_idx]
    return None


def extract_performances(soup: BeautifulSoup) -> Dict[str, Optional[float]]:
    """
    Cherche:
    - perf_4_semaines
    - perf_ytd (depuis 1er janvier)
    - perf_1_an
    - perf_3_ans
    """
    # Synonymes possibles selon locale Morningstar
    row_fund = ["Fonds", "Fund"]

    perf_4w = _find_value_in_any_table(
        soup,
        row_label_candidates=row_fund,
        col_label_candidates=["4 sem.", "4 sem", "4 weeks", "4w", "1 mois", "1 month", "1m"],
    )

    perf_ytd = _find_value_in_any_table(
        soup,
        row_label_candidates=row_fund,
        col_label_candidates=["YTD", "year to date", "depuis le début", "depuis le début de l'année"],
    )

    perf_1y = _find_value_in_any_table(
        soup,
        row_label_candidates=row_fund,
        col_label_candidates=["1 an", "1 year", "1y"],
    )

    perf_3y = _find_value_in_any_table(
        soup,
        row_label_candidates=row_fund,
        col_label_candidates=["3 ans", "3 years", "3 year", "3y"],
    )

    return {
        "perf_4_semaines": _parse_fr_number(perf_4w) if perf_4w is not None else None,
        "perf_depuis_1er_janvier": _parse_fr_number(perf_ytd) if perf_ytd is not None else None,
        "perf_1_an": _parse_fr_number(perf_1y) if perf_1y is not None else None,
        "perf_3_ans": _parse_fr_number(perf_3y) if perf_3y is not None else None,
    }


def parse_morningstar_html_file(html_path: str) -> Dict[str, Any]:
    html = Path(html_path).read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html, "lxml")

    stars = extract_star_rating(soup)
    perfs = extract_performances(soup)

    return {
        "stars": stars,
        **perfs,
    }


if __name__ == "__main__":
    script_dir = Path(__file__).resolve().parent
    html_path = script_dir / "captures" / "morningstar_capture.html"
    data = parse_morningstar_html_file(str(html_path))
    print(data)
