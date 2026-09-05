"""Thin EDGAR client: daily indexes, ticker map, submissions, filing documents.

Everything here speaks plain HTTP against www.sec.gov / data.sec.gov, which are
free and keyless but require a contact User-Agent (see http.Http).
"""

from __future__ import annotations

import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterator

from .http import Http

log = logging.getLogger(__name__)

ARCHIVES = "https://www.sec.gov/Archives"
DATA = "https://data.sec.gov"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


def quarter(d: date) -> int:
    return (d.month - 1) // 3 + 1


def business_days(end: date, count: int) -> list[date]:
    """The ``count`` most recent Mon-Fri dates ending at ``end`` (inclusive)."""
    out: list[date] = []
    cur = end
    while len(out) < count:
        if cur.weekday() < 5:
            out.append(cur)
        cur -= timedelta(days=1)
    return out


def strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def text_of(node: ET.Element | None, *path: str) -> str:
    """Namespace-insensitive nested lookup returning stripped text ('' if absent).

    EDGAR ownership/13F documents wrap most leaves in a <value> child, and the
    namespace on 13F info tables varies by filer, so both are handled here.
    """
    cur = node
    for name in path:
        if cur is None:
            return ""
        nxt = None
        for child in cur:
            if strip_ns(child.tag) == name:
                nxt = child
                break
        cur = nxt
    if cur is None:
        return ""
    if len(cur):
        for child in cur:
            if strip_ns(child.tag) == "value":
                return (child.text or "").strip()
    return (cur.text or "").strip()


@dataclass
class IndexRow:
    cik: str
    company: str
    form_type: str
    filed: date
    filename: str  # e.g. edgar/data/320193/0000320193-26-000075.txt

    @property
    def accession(self) -> str:
        m = re.search(r"(\d{10}-\d{2}-\d{6})", self.filename)
        return m.group(1) if m else ""

    @property
    def folder_url(self) -> str:
        return f"{ARCHIVES}/edgar/data/{int(self.cik)}/{self.accession.replace('-', '')}"

    @property
    def index_url(self) -> str:
        return f"{self.folder_url}/{self.accession}-index.htm"

    @property
    def submission_url(self) -> str:
        return f"{ARCHIVES}/{self.filename}"


def parse_master_idx(text: str) -> Iterator[IndexRow]:
    """Parse a pipe-delimited EDGAR ``master.idx``: CIK|Name|Form|Date|Filename."""
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) != 5 or not parts[0].strip().isdigit():
            continue
        cik, company, form_type, filed, filename = (p.strip() for p in parts)
        try:
            filed_date = date.fromisoformat(filed)
        except ValueError:
            continue
        yield IndexRow(cik=cik, company=company, form_type=form_type, filed=filed_date, filename=filename)


class Edgar:
    def __init__(self, http: Http) -> None:
        self.http = http
        self._ticker_map: dict[str, str] | None = None
        # Non-fatal failures worth reporting in the digest's source-status block.
        self.errors: list[str] = []

    # -- indexes ---------------------------------------------------------
    def daily_index(self, day: date) -> list[IndexRow]:
        """All filings indexed for ``day``; empty on weekends/holidays (404)."""
        url = (
            f"{ARCHIVES}/edgar/daily-index/{day.year}/QTR{quarter(day)}/"
            f"master.{day:%Y%m%d}.idx"
        )
        text = self.http.get_text(url, allow_404=True, encoding="latin-1")
        if text is None:
            return []
        return list(parse_master_idx(text))

    def filings_of_type(self, days: list[date], forms: set[str]) -> list[IndexRow]:
        """Matching filings across ``days``, deduplicated.

        A filing can be indexed on more than one day (late dissemination, amended
        indexes), and the same accession appears once per filer, so identical rows
        are collapsed here rather than in every caller.
        """
        rows: list[IndexRow] = []
        seen: set[tuple[str, str, str]] = set()
        for day in days:
            try:
                found = self.daily_index(day)
            except Exception as exc:  # one bad day must not kill the run
                log.warning("daily index for %s failed: %s", day, exc)
                self.errors.append(f"EDGAR 日索引 {day} 抓取失败: {exc}")
                continue
            for row in found:
                if row.form_type not in forms:
                    continue
                marker = (row.accession, row.cik, row.form_type)
                if marker in seen:
                    continue
                seen.add(marker)
                rows.append(row)
        return rows

    # -- ticker map ------------------------------------------------------
    def ticker_map(self) -> dict[str, str]:
        """CIK (as int-string, unpadded) -> ticker."""
        if self._ticker_map is None:
            data = self.http.get_json(TICKERS_URL, allow_404=True) or {}
            self._ticker_map = {
                str(row["cik_str"]): str(row["ticker"]).upper()
                for row in data.values()
                if row.get("cik_str") and row.get("ticker")
            }
        return self._ticker_map

    def ciks_for_tickers(self, tickers: list[str]) -> dict[str, str]:
        """ticker -> zero-padded 10-digit CIK, for the tickers EDGAR knows."""
        want = {t.upper() for t in tickers}
        out: dict[str, str] = {}
        for cik, ticker in self.ticker_map().items():
            if ticker in want:
                out[ticker] = cik.zfill(10)
        return out

    # -- submissions -----------------------------------------------------
    def submissions(self, cik: str) -> dict:
        cik10 = str(cik).zfill(10)
        return self.http.get_json(f"{DATA}/submissions/CIK{cik10}.json", allow_404=True) or {}

    def recent_filings(self, cik: str, forms: set[str], limit: int = 8) -> list[IndexRow]:
        """Recent filings of the given form types for one filer, newest first."""
        data = self.submissions(cik)
        recent = (data.get("filings") or {}).get("recent") or {}
        name = data.get("name", "")
        rows: list[IndexRow] = []
        for form, acc, filed, doc in zip(
            recent.get("form", []),
            recent.get("accessionNumber", []),
            recent.get("filingDate", []),
            recent.get("primaryDocument", []),
        ):
            if form not in forms:
                continue
            try:
                filed_date = date.fromisoformat(filed)
            except ValueError:
                continue
            rows.append(
                IndexRow(
                    cik=str(int(cik)),
                    company=name,
                    form_type=form,
                    filed=filed_date,
                    filename=f"edgar/data/{int(cik)}/{acc}.txt",
                )
            )
            if len(rows) >= limit:
                break
        return rows

    # -- documents -------------------------------------------------------
    def filing_files(self, row: IndexRow) -> list[dict]:
        data = self.http.get_json(f"{row.folder_url}/index.json", allow_404=True) or {}
        return ((data.get("directory") or {}).get("item")) or []

    def primary_xml(self, row: IndexRow, exclude: tuple[str, ...] = ("primary_doc.xml",)) -> bytes | None:
        """The filing's main XML payload (ownership doc, or a 13F information table)."""
        for item in self.filing_files(row):
            name = str(item.get("name", ""))
            if name.lower().endswith(".xml") and name.lower() not in exclude:
                body = self.http.get(f"{row.folder_url}/{name}", allow_404=True)
                if body:
                    return body
        return None

    def ownership_xml(self, row: IndexRow) -> bytes | None:
        """Form 3/4/5 ownership document, falling back to the full text submission."""
        for item in self.filing_files(row):
            name = str(item.get("name", ""))
            if name.lower().endswith(".xml") and name.lower() != "primary_doc.xml":
                body = self.http.get(f"{row.folder_url}/{name}", allow_404=True)
                if body and b"ownershipDocument" in body:
                    return body
        raw = self.http.get(row.submission_url, allow_404=True)
        return extract_ownership_block(raw) if raw else None


    # -- company search --------------------------------------------------
    def search_companies(self, name: str, form_type: str = "13F-HR") -> list[tuple[str, str]]:
        """(CIK, conformed name) for filers matching ``name``, via EDGAR company search."""
        import urllib.parse

        url = (
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&company={urllib.parse.quote(name)}&type={urllib.parse.quote(form_type)}"
            "&dateb=&owner=include&count=40&output=atom"
        )
        body = self.http.get(url, allow_404=True)
        if not body:
            return []
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return []
        results: list[tuple[str, str]] = []
        for info in root.iter():
            if strip_ns(info.tag) != "company-info":
                continue
            cik = text_of(info, "cik")
            conformed = text_of(info, "conformed-name")
            if cik:
                results.append((cik.zfill(10), conformed))
        if not results:
            # A single exact match redirects to the filer page, which carries the CIK inline.
            m = re.search(rb"CIK=(\d{10})", body)
            if m:
                results.append((m.group(1).decode(), name))
        return results


def extract_ownership_block(raw: bytes) -> bytes | None:
    """Pull <ownershipDocument>...</ownershipDocument> out of a full submission text."""
    start = raw.find(b"<ownershipDocument")
    end = raw.find(b"</ownershipDocument>")
    if start == -1 or end == -1:
        return None
    return raw[start : end + len(b"</ownershipDocument>")]


def unzip_first(data: bytes, suffix: str) -> bytes | None:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            if name.lower().endswith(suffix):
                return zf.read(name)
    return None
