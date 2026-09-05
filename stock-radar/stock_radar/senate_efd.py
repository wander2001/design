"""Senate Electronic Financial Disclosure (eFD) client.

eFD gates every search behind a click-through agreement plus a CSRF token, and
serves results through a DataTables endpoint. Electronic PTRs render as an HTML
table of transactions; paper filings are scans with nothing machine-readable, so
those degrade to a link.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser

import requests

log = logging.getLogger(__name__)

BASE = "https://efdsearch.senate.gov"
HOME = f"{BASE}/search/home/"
SEARCH = f"{BASE}/search/report/data/"
REPORT_TYPE_PTR = 11

CSRF_RE = re.compile(r"name=['\"]csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)")
LINK_RE = re.compile(r'href=["\'](?P<href>[^"\']+)["\'][^>]*>(?P<text>[^<]*)')


class _TableParser(HTMLParser):
    """Collect every table's header names and row cells, ignoring script/style."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[dict] = []
        self._table: dict | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._is_header = False
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "table":
            self._table = {"headers": [], "rows": []}
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            self._is_header = tag == "th"

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._table is not None and self._row is not None:
            if self._is_header and not self._table["headers"]:
                self._table["headers"] = self._row
            elif self._row:
                self._table["rows"].append(self._row)
            self._row = None
            self._is_header = False
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None

    def handle_data(self, data: str) -> None:
        if self._skip == 0 and self._cell is not None:
            self._cell.append(data)


def parse_tables(html: str) -> list[dict]:
    parser = _TableParser()
    parser.feed(html)
    return parser.tables


@dataclass
class SenateFiling:
    first_name: str
    last_name: str
    filer_type: str
    title: str
    url: str
    filed: date | None

    @property
    def person(self) -> str:
        return " ".join(part for part in (self.first_name.strip(), self.last_name.strip()) if part).title()

    @property
    def is_electronic(self) -> bool:
        """Paper filings are scans; only /view/ptr/ pages carry a parseable table."""
        return "/view/ptr/" in self.url


@dataclass
class SenateTransaction:
    filing: SenateFiling
    traded: date | None
    owner: str
    ticker: str
    asset: str
    asset_type: str
    action: str
    amount_low: float
    amount_high: float
    comment: str = ""


def _date(raw: str) -> date | None:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _amounts(raw: str) -> tuple[float, float]:
    nums = [float(n.replace(",", "")) for n in re.findall(r"[\d,]+(?:\.\d+)?", raw or "") if n.strip(",")]
    if not nums:
        return (0.0, 0.0)
    return (min(nums), max(nums))


class EfdSession:
    """A logged-in eFD session: accepts the agreement once, then searches."""

    def __init__(self, user_agent: str, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self.token = ""
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
        )

    def open(self) -> None:
        page = self.session.get(HOME, timeout=self.timeout)
        page.raise_for_status()
        match = CSRF_RE.search(page.text)
        self.token = match.group(1) if match else ""
        accepted = self.session.post(
            HOME,
            data={"prohibition_agreement": "1", "csrfmiddlewaretoken": self.token},
            headers={"Referer": HOME},
            timeout=self.timeout,
        )
        accepted.raise_for_status()
        # The agreement POST rotates the CSRF cookie; later posts must use the new one.
        self.token = self.session.cookies.get("csrftoken", self.token)

    def search_ptrs(self, since: date, limit: int = 100) -> list[SenateFiling]:
        resp = self.session.post(
            SEARCH,
            data={
                "start": "0",
                "length": str(limit),
                "report_types": f"[{REPORT_TYPE_PTR}]",
                "filer_types": "[]",
                "submitted_start_date": f"{since:%m/%d/%Y} 00:00:00",
                "submitted_end_date": "",
                "candidate_state": "",
                "senator_state": "",
                "office_id": "",
                "first_name": "",
                "last_name": "",
                "csrfmiddlewaretoken": self.token,
            },
            headers={"Referer": HOME, "X-CSRFToken": self.token},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        rows = resp.json().get("data", []) or []

        filings: list[SenateFiling] = []
        for row in rows:
            if len(row) < 5:
                continue
            link = LINK_RE.search(str(row[3]))
            href = link.group("href") if link else ""
            filings.append(
                SenateFiling(
                    first_name=str(row[0]),
                    last_name=str(row[1]),
                    filer_type=str(row[2]),
                    title=(link.group("text").strip() if link else str(row[3])),
                    url=(BASE + href) if href.startswith("/") else href,
                    filed=_date(str(row[4])),
                )
            )
        return filings

    def transactions(self, filing: SenateFiling) -> list[SenateTransaction]:
        """Parse one electronic PTR's transaction table; empty for paper filings."""
        if not filing.is_electronic:
            return []
        page = self.session.get(filing.url, timeout=self.timeout)
        page.raise_for_status()

        out: list[SenateTransaction] = []
        for table in parse_tables(page.text):
            headers = [h.lower() for h in table["headers"]]
            if not any("transaction" in h or "ticker" in h for h in headers):
                continue

            def column(*names: str) -> int:
                for name in names:
                    for idx, header in enumerate(headers):
                        if name in header:
                            return idx
                return -1

            idx_date = column("transaction date", "date")
            idx_owner = column("owner")
            idx_ticker = column("ticker")
            idx_asset = column("asset name", "asset")
            idx_type = column("asset type")
            idx_action = column("type")
            idx_amount = column("amount")
            idx_comment = column("comment")

            for row in table["rows"]:
                def cell(idx: int) -> str:
                    return row[idx].strip() if 0 <= idx < len(row) else ""

                ticker = cell(idx_ticker)
                if ticker in ("--", "N/A"):
                    ticker = ""
                low, high = _amounts(cell(idx_amount))
                action = cell(idx_action).lower().replace(" ", "_")
                out.append(
                    SenateTransaction(
                        filing=filing,
                        traded=_date(cell(idx_date)),
                        owner=cell(idx_owner),
                        ticker=ticker.upper(),
                        asset=cell(idx_asset),
                        asset_type=cell(idx_type),
                        action=action or "unknown",
                        amount_low=low,
                        amount_high=high,
                        comment=cell(idx_comment),
                    )
                )
        return out
