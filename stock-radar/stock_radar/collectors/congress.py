"""Congressional stock trades (国会议员投资变化).

The STOCK Act requires members to disclose trades within 45 days, but the House
and Senate publish them as PDFs with no official structured feed. Providers are
therefore pluggable and tried in order, so one dead mirror degrades the section
instead of the run.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..models import Item
from .base import CollectorContext

log = logging.getLogger(__name__)

HOUSE_URL = "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json"
SENATE_URL = "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json"
QUIVER_URL = "https://api.quiverquant.com/beta/live/congresstrading"
HOUSE_CLERK_ZIP = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.ZIP"

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d/%m/%Y")


def parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    raw = str(raw).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse_amount(raw: str | None) -> tuple[float, float]:
    """'$1,001 - $15,000' -> (1001.0, 15000.0); open-ended ranges repeat the bound."""
    if not raw:
        return (0.0, 0.0)
    nums = [float(n.replace(",", "")) for n in re.findall(r"[\d,]+(?:\.\d+)?", str(raw)) if n.strip(",")]
    if not nums:
        return (0.0, 0.0)
    return (min(nums), max(nums))


TYPE_LABEL = {
    "purchase": ("买入", "Purchase"),
    "sale": ("卖出", "Sale"),
    "sale_full": ("清仓卖出", "Full sale"),
    "sale_partial": ("部分卖出", "Partial sale"),
    "exchange": ("置换", "Exchange"),
    "receive": ("受让", "Received"),
}


def normalize_type(raw: str | None) -> str:
    key = str(raw or "").strip().lower().replace(" ", "_")
    if key.startswith("sale"):
        return key if key in TYPE_LABEL else "sale"
    if key.startswith("purchase") or key == "buy":
        return "purchase"
    return key or "unknown"


@dataclass
class CongressTrade:
    person: str
    chamber: str  # House | Senate
    ticker: str
    asset: str
    action: str
    amount_low: float
    amount_high: float
    transaction_date: date | None
    disclosure_date: date | None
    url: str
    owner: str = ""
    source: str = ""

    @property
    def amount_text(self) -> str:
        if self.amount_low and self.amount_high and self.amount_low != self.amount_high:
            return f"${self.amount_low:,.0f} - ${self.amount_high:,.0f}"
        value = self.amount_high or self.amount_low
        return f"${value:,.0f}" if value else "金额未披露"


# -- providers -----------------------------------------------------------


def _from_stockwatcher(ctx: CollectorContext) -> list[CongressTrade]:
    trades: list[CongressTrade] = []
    for url, chamber, person_key in (
        (HOUSE_URL, "House", "representative"),
        (SENATE_URL, "Senate", "senator"),
    ):
        rows = ctx.http.get_json(url, allow_404=True) or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or "").upper().strip()
            if ticker in ("--", "N/A", "NONE"):
                ticker = ""
            low, high = parse_amount(row.get("amount"))
            trades.append(
                CongressTrade(
                    person=str(row.get(person_key) or "Unknown").replace("Hon. ", "").strip(),
                    chamber=chamber,
                    ticker=ticker,
                    asset=str(row.get("asset_description") or "").strip(),
                    action=normalize_type(row.get("type")),
                    amount_low=low,
                    amount_high=high,
                    transaction_date=parse_date(row.get("transaction_date")),
                    disclosure_date=parse_date(row.get("disclosure_date")),
                    url=str(row.get("ptr_link") or ""),
                    owner=str(row.get("owner") or ""),
                    source="stockwatcher",
                )
            )
    return trades


def _from_quiver(ctx: CollectorContext) -> list[CongressTrade]:
    token = os.environ.get("QUIVER_API_KEY", "")
    if not token:
        raise RuntimeError("QUIVER_API_KEY not set")
    body = ctx.http.get(QUIVER_URL, headers={"Authorization": f"Bearer {token}"})
    import json

    rows = json.loads(body or b"[]")
    trades: list[CongressTrade] = []
    for row in rows:
        low, high = parse_amount(row.get("Range"))
        trades.append(
            CongressTrade(
                person=str(row.get("Representative") or "Unknown").strip(),
                chamber=str(row.get("House") or "").title() or "Congress",
                ticker=str(row.get("Ticker") or "").upper().strip(),
                asset=str(row.get("Ticker") or "").strip(),
                action=normalize_type(row.get("Transaction")),
                amount_low=low,
                amount_high=high,
                transaction_date=parse_date(row.get("TransactionDate")),
                disclosure_date=parse_date(row.get("ReportDate")),
                url="https://www.quiverquant.com/congresstrading/",
                source="quiver",
            )
        )
    return trades


def _from_house_clerk(ctx: CollectorContext) -> list[CongressTrade]:
    """Official House disclosure index.

    The Clerk publishes only filing metadata in machine-readable form (the trades
    themselves stay in PDFs), so this yields 'member filed a PTR' pointers rather
    than line items — a last-resort source that is always authoritative.
    """
    import xml.etree.ElementTree as ET

    from ..edgar import unzip_first

    # In early January the current year's file is empty or absent, so fall back a year.
    xml_bytes = None
    year = ctx.today.year
    for candidate in (ctx.today.year, ctx.today.year - 1):
        raw = ctx.http.get(HOUSE_CLERK_ZIP.format(year=candidate), allow_404=True)
        if raw:
            xml_bytes = unzip_first(raw, ".xml")
            if xml_bytes:
                year = candidate
                break
    if not xml_bytes:
        return []
    root = ET.fromstring(xml_bytes)
    trades: list[CongressTrade] = []
    for member in root.iter("Member"):

        def field(tag: str) -> str:
            node = member.find(tag)
            return (node.text or "").strip() if node is not None and node.text else ""

        if field("FilingType") != "P":  # P = Periodic Transaction Report
            continue
        doc_id = field("DocID")
        name = " ".join(x for x in (field("First"), field("Last")) if x) or field("Last")
        trades.append(
            CongressTrade(
                person=name,
                chamber="House",
                ticker="",
                asset="Periodic Transaction Report (PDF)",
                action="filing",
                amount_low=0.0,
                amount_high=0.0,
                transaction_date=None,
                disclosure_date=parse_date(field("FilingDate")),
                url=f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{field('Year') or year}/{doc_id}.pdf",
                source="house_clerk",
            )
        )
    return trades


PROVIDERS = {
    "stockwatcher": _from_stockwatcher,
    "quiver": _from_quiver,
    "house_clerk": _from_house_clerk,
}


class CongressCollector:
    name = "congress"

    def enabled(self, ctx: CollectorContext) -> bool:
        return bool(ctx.config.get("sources.congress.enabled", True))

    def collect(self, ctx: CollectorContext) -> list[Item]:
        cfg = ctx.config
        names = [str(p) for p in cfg.get("sources.congress.providers", ["stockwatcher"])]
        lookback = int(cfg.get("sources.congress.lookback_days", 45))
        min_amount = float(cfg.get("sources.congress.min_amount_usd", 15000))
        watchlist_only = bool(cfg.get("sources.congress.watchlist_only", False))
        tickers = set(cfg.tickers)
        people = {str(p).lower() for p in cfg.get("watchlist.people", [])}
        cutoff = ctx.today - timedelta(days=lookback)

        trades: list[CongressTrade] = []
        errors: list[str] = []
        for provider_name in names:
            provider = PROVIDERS.get(provider_name)
            if provider is None:
                errors.append(f"{provider_name}: unknown provider")
                continue
            try:
                got = provider(ctx)
                log.info("congress provider %s returned %d rows", provider_name, len(got))
                trades.extend(got)
                if got:
                    break  # first provider that produces data wins
            except Exception as exc:
                log.warning("congress provider %s failed: %s", provider_name, exc)
                errors.append(f"{provider_name}: {exc}")

        if not trades and errors:
            raise RuntimeError("; ".join(errors))

        items: list[Item] = []
        for trade in trades:
            stamp = trade.disclosure_date or trade.transaction_date
            if stamp is None or stamp < cutoff or stamp > ctx.today + timedelta(days=1):
                continue
            if trade.amount_high and trade.amount_high < min_amount:
                continue
            watched_person = trade.person.lower() in people
            watched_ticker = bool(trade.ticker and trade.ticker in tickers)
            if watchlist_only and not (watched_person or watched_ticker):
                continue

            zh, en = TYPE_LABEL.get(trade.action, (trade.action, trade.action))
            label = trade.ticker or (trade.asset[:40] or "未披露标的")
            owner = f"[{trade.owner}]" if trade.owner and trade.owner.lower() not in ("self", "--") else ""
            title = f"{trade.person} ({trade.chamber}) {zh} {label} {owner}".strip()
            when = trade.transaction_date or trade.disclosure_date
            lag = ""
            if trade.transaction_date and trade.disclosure_date:
                lag = f" · 延迟披露 {(trade.disclosure_date - trade.transaction_date).days} 天"

            score = 40.0
            score += min(25.0, trade.amount_high / 100_000) if trade.amount_high else 0
            if watched_ticker:
                score += 30
            if watched_person:
                score += 20
            if trade.action == "purchase":
                score += 5

            item = Item(
                kind="congress",
                title=title,
                url=trade.url,
                when=when,
                summary=f"{trade.amount_text} · 交易日 {trade.transaction_date or '未知'}{lag}",
                tickers=[trade.ticker] if trade.ticker else [],
                score=score,
                source=f"Congress/{trade.chamber} ({trade.source})",
                detail={
                    "person": trade.person,
                    "chamber": trade.chamber,
                    "ticker": trade.ticker,
                    "asset": trade.asset,
                    "action": trade.action,
                    "action_en": en,
                    "amount_low": trade.amount_low,
                    "amount_high": trade.amount_high,
                    "transaction_date": trade.transaction_date,
                    "disclosure_date": trade.disclosure_date,
                    "owner": trade.owner,
                },
            )
            items.append(
                item.with_key(
                    trade.person, trade.ticker, trade.action,
                    trade.transaction_date, trade.disclosure_date, trade.amount_high,
                )
            )
        return items
