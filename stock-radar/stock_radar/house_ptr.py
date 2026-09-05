"""Parse a House Periodic Transaction Report PDF into transactions.

The Clerk publishes only filing metadata in machine-readable form; the trades
themselves exist solely inside these PDFs. Text extracted from them wraps
mid-row and renders the small-caps headings as NUL glyphs, so the parser works
on the flattened text and anchors on the one shape that is always intact: the
transaction code, the two dates, and the amount range.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime

log = logging.getLogger(__name__)

# "SP P 08/14/2026 09/02/2026 $250,001 - $500,000" — owner is optional (blank = the member).
TXN_RE = re.compile(
    r"(?:(?P<owner>SP|JT|DC)\s+)?"
    r"(?P<code>S \(partial\)|[PSE])\s+"
    r"(?P<traded>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?P<notified>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"\$(?P<low>[\d,]+)\s*-\s*\$(?P<high>[\d,]+)",
)
TICKER_RE = re.compile(r"\((?P<ticker>[A-Z][A-Z.\-]{0,5})\)")
ASSET_TYPE_RE = re.compile(r"\[(?P<code>[A-Z]{2}[A-Z0-9]?)\]")
FILING_ID_RE = re.compile(r"Filing ID\s*#?\s*(\d+)")

CODE_LABEL = {
    "P": "purchase",
    "S": "sale",
    "S (partial)": "sale_partial",
    "E": "exchange",
}

# https://fd.house.gov/reference/asset-type-codes.aspx — the ones that are equities.
EQUITY_ASSET_TYPES = {"ST", "OP", "CS", "AB", "ET", "MF", "OL", "RS", "SO"}


@dataclass
class PtrTransaction:
    asset: str
    ticker: str
    asset_type: str
    owner: str
    action: str
    traded: date | None
    notified: date | None
    amount_low: float
    amount_high: float

    @property
    def is_equity(self) -> bool:
        """True for things a stock digest should surface (a ticker, or an equity code)."""
        return bool(self.ticker) or self.asset_type in EQUITY_ASSET_TYPES


def _date(raw: str) -> date | None:
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


# A row's trailing notes ("Filing Status: New", "Source of: Schwab", "Location: US")
# and the table headings sit between transactions. They are what separates one row's
# asset name from the next, so they are treated as boundaries rather than scrubbed.
NOTE_RE = re.compile(r"^[A-Za-z][A-Za-z.]{0,14}(?:\s+[A-Za-z.]{1,14}){0,3}\s*:")
HEADER_RE = re.compile(
    r"^(?:ID\b|Owner\b|Asset\b|Transaction\b|Type$|Date\b|Notification\b|Amount\b|Gains\b|"
    r"Name:|Status:|State/District:|Clerk of the House|Digitally Signed|Filing ID)"
)


def clean_lines(text: str) -> list[str]:
    """Readable lines from the PDF text, with wrapped amount tails rejoined.

    The small-caps headings render as NUL glyphs and the amount range wraps onto its
    own line ("$15,001 -" then "$50,000"), so both are normalized here; everything
    else keeps its line structure, which is what tells one row's asset from the next.
    """
    lines: list[str] = []
    for raw in text.replace("\x00", "").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            continue
        # A bare "$50,000" is the tail of the previous line's amount range.
        if re.fullmatch(r"\$[\d,]+", line) and lines and lines[-1].rstrip().endswith("-"):
            lines[-1] = f"{lines[-1]} {line}"
            continue
        lines.append(line)
    return lines


def _is_boundary(line: str) -> bool:
    return bool(
        TXN_RE.search(line)
        or NOTE_RE.match(line)
        or HEADER_RE.match(line)
        or line.startswith(("$", "*", "•"))
    )


def parse_ptr_text(text: str) -> list[PtrTransaction]:
    """Extract every transaction row from one PTR's extracted text."""
    lines = clean_lines(text)
    transactions: list[PtrTransaction] = []

    for index, line in enumerate(lines):
        match = TXN_RE.search(line)
        if not match:
            continue
        # The asset name is the run of lines directly above the transaction, back to
        # the previous row's notes or the table heading.
        asset_lines: list[str] = []
        cursor = index - 1
        while cursor >= 0 and len(asset_lines) < 4 and not _is_boundary(lines[cursor]):
            asset_lines.insert(0, lines[cursor])
            cursor -= 1
        segment = " ".join(asset_lines).strip()

        ticker_match = TICKER_RE.search(segment)
        type_match = ASSET_TYPE_RE.search(segment)
        name = segment[: type_match.start()] if type_match else segment
        try:
            low = float(match.group("low").replace(",", ""))
            high = float(match.group("high").replace(",", ""))
        except ValueError:
            low = high = 0.0
        transactions.append(
            PtrTransaction(
                asset=re.sub(r"^[\d.\s]*", "", name).strip(" .:;•|")[:120],
                ticker=(ticker_match.group("ticker") if ticker_match else ""),
                asset_type=(type_match.group("code") if type_match else ""),
                owner=(match.group("owner") or ""),
                action=CODE_LABEL.get(match.group("code"), match.group("code")),
                traded=_date(match.group("traded")),
                notified=_date(match.group("notified")),
                amount_low=low,
                amount_high=high,
            )
        )
    return transactions


def extract_pdf_text(data: bytes) -> str:
    """Text of a PTR PDF, or '' when it is a scan or pypdf is unavailable."""
    try:
        from pypdf import PdfReader
    except ImportError:
        log.warning("pypdf 未安装，无法解析 PTR PDF 正文；pip install pypdf")
        return ""
    import io

    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:
        log.warning("PTR PDF 解析失败: %s", exc)
        return ""


def parse_ptr_pdf(data: bytes) -> list[PtrTransaction]:
    text = extract_pdf_text(data)
    return parse_ptr_text(text) if text.strip() else []
