"""Offline test rig: an Http stand-in that serves fixtures by URL pattern."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURES = Path(__file__).resolve().parent / "fixtures"

TODAY = date(2026, 9, 3)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


MASTER_IDX = b"""Description:           Master Index of EDGAR Dissemination Feed
CIK|Company Name|Form Type|Date Filed|Filename
--------------------------------------------------------------------------------
320193|Apple Inc.|4|2026-09-03|edgar/data/320193/0000320193-26-000075.txt
1051401|COOK TIMOTHY D|4|2026-09-03|edgar/data/320193/0000320193-26-000075.txt
1045810|NVIDIA CORP|8-K|2026-09-03|edgar/data/1045810/0001045810-26-000200.txt
320193|Apple Inc.|SC 13G/A|2026-09-03|edgar/data/320193/0001067983-26-000030.txt
1067983|BERKSHIRE HATHAWAY INC|SC 13G/A|2026-09-03|edgar/data/320193/0001067983-26-000030.txt
9999999|SOME OTHER CO|4|2026-09-03|edgar/data/9999999/0009999999-26-000001.txt
"""

COMPANY_TICKERS = json.dumps(
    {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
        "2": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
    }
).encode()

SUBMISSIONS_BRK = json.dumps(
    {
        "name": "BERKSHIRE HATHAWAY INC",
        "filings": {
            "recent": {
                "form": ["13F-HR", "8-K", "13F-HR"],
                "accessionNumber": ["0001067983-26-000020", "0001067983-26-000019", "0001067983-26-000010"],
                "filingDate": ["2026-09-01", "2026-08-15", "2026-05-15"],
                "primaryDocument": ["primary_doc.xml", "a.htm", "primary_doc.xml"],
            }
        },
    }
).encode()

CONGRESS_HOUSE = json.dumps(
    [
        {
            "disclosure_year": 2026,
            "disclosure_date": "09/02/2026",
            "transaction_date": "2026-08-14",
            "owner": "joint",
            "ticker": "NVDA",
            "asset_description": "NVIDIA Corporation",
            "type": "purchase",
            "amount": "$250,001 - $500,000",
            "representative": "Hon. Jane Doe",
            "district": "CA12",
            "ptr_link": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20260001.pdf",
        },
        {
            "disclosure_date": "09/02/2026",
            "transaction_date": "2026-08-20",
            "ticker": "--",
            "asset_description": "US Treasury Bill",
            "type": "sale_full",
            "amount": "$1,001 - $15,000",
            "representative": "Hon. John Roe",
            "ptr_link": "https://example.com/b.pdf",
        },
        {
            "disclosure_date": "01/05/2024",
            "transaction_date": "2023-12-01",
            "ticker": "AAPL",
            "asset_description": "Apple Inc",
            "type": "purchase",
            "amount": "$1,000,001 - $5,000,000",
            "representative": "Hon. Old Timer",
            "ptr_link": "https://example.com/c.pdf",
        },
    ]
).encode()

CONGRESS_SENATE = json.dumps(
    [
        {
            "transaction_date": "08/28/2026",
            "disclosure_date": "09/01/2026",
            "owner": "Spouse",
            "ticker": "AAPL",
            "asset_description": "Apple Inc",
            "asset_type": "Stock",
            "type": "Sale (Full)",
            "amount": "$100,001 - $250,000",
            "senator": "Sam Senator",
            "ptr_link": "https://efdsearch.senate.gov/x",
        }
    ]
).encode()


class FakeHttp:
    """Routes URLs to fixture bytes; records every request for assertions."""

    def __init__(self, routes: dict[str, bytes] | None = None) -> None:
        self.routes: dict[str, bytes] = dict(routes or {})
        self.requested: list[str] = []
        self.user_agent = "test"

    def add(self, pattern: str, body: bytes) -> "FakeHttp":
        self.routes[pattern] = body
        return self

    def get(self, url: str, *, allow_404: bool = False, headers: dict | None = None) -> bytes | None:
        self.requested.append(url)
        for pattern, body in self.routes.items():
            if pattern in url:
                return body
        if allow_404:
            return None
        raise AssertionError(f"unrouted URL in test: {url}")

    def get_text(self, url: str, *, allow_404: bool = False, encoding: str = "utf-8") -> str | None:
        body = self.get(url, allow_404=allow_404)
        return None if body is None else body.decode(encoding, errors="replace")

    def get_json(self, url: str, *, allow_404: bool = False):
        body = self.get(url, allow_404=allow_404)
        return None if body is None else json.loads(body)


@pytest.fixture
def http() -> FakeHttp:
    return FakeHttp(
        {
            "daily-index": MASTER_IDX,
            "company_tickers.json": COMPANY_TICKERS,
            "data.sec.gov/submissions/CIK0001067983": SUBMISSIONS_BRK,
            # Form 4 filing folder
            "000032019326000075/index.json": json.dumps(
                {"directory": {"item": [{"name": "primary_doc.xml"}, {"name": "wf-form4.xml"}]}}
            ).encode(),
            "000032019326000075/wf-form4.xml": fixture("form4_sample.xml"),
            # 13F filings
            "000106798326000020/index.json": json.dumps(
                {"directory": {"item": [{"name": "primary_doc.xml"}, {"name": "infotable.xml"}]}}
            ).encode(),
            "000106798326000020/infotable.xml": fixture("13f_q2.xml"),
            "000106798326000010/index.json": json.dumps(
                {"directory": {"item": [{"name": "primary_doc.xml"}, {"name": "infotable.xml"}]}}
            ).encode(),
            "000106798326000010/infotable.xml": fixture("13f_q1.xml"),
            # congress
            "house-stock-watcher": CONGRESS_HOUSE,
            "senate-stock-watcher": CONGRESS_SENATE,
            # news
            "cnbc.com": fixture("rss_sample.xml"),
            "sec.gov/news/pressreleases.rss": fixture("atom_sample.xml"),
        }
    )


@pytest.fixture
def ctx(http, tmp_path):
    from stock_radar.collectors.base import CollectorContext
    from stock_radar.config import Config
    from stock_radar.edgar import Edgar

    config = Config.load(None)
    config.data["sec"]["user_agent"] = "Test Runner tests@example.invalid"
    config.data["watchlist"]["tickers"] = ["AAPL", "NVDA"]
    config.data["watchlist"]["funds"] = [{"name": "Berkshire Hathaway", "cik": "0001067983"}]
    config.data["sources"]["news"]["feeds"] = [
        {"name": "CNBC", "url": "https://www.cnbc.com/id/1/rss"},
        {"name": "SEC Press", "url": "https://www.sec.gov/news/pressreleases.rss"},
    ]
    config.data["sources"]["news"]["per_ticker_feed"] = ""
    config.data["output"]["dir"] = str(tmp_path / "out")
    config.data["state"]["path"] = str(tmp_path / "state.db")
    return CollectorContext(config=config, http=http, edgar=Edgar(http), today=TODAY)
