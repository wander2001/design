"""Congressional stock trades (国会议员投资变化).

The STOCK Act requires disclosure within 45 days, but neither chamber publishes a
structured feed: the House posts PDFs behind a yearly ZIP index, and the Senate
puts electronic reports behind a click-through agreement. Both official sources
are read here directly, because the community mirrors that used to normalize them
now answer AccessDenied.

Parsed filings are cached in the state DB — a PTR's contents never change, and
re-downloading 45 days of PDFs every morning would be both slow and rude.
"""

from __future__ import annotations

import io
import logging
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..house_ptr import parse_ptr_pdf
from ..models import Item
from ..senate_efd import EfdSession
from .base import CollectorContext

log = logging.getLogger(__name__)

HOUSE_CLERK_ZIP = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.ZIP"
HOUSE_PTR_PDF = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
QUIVER_URL = "https://api.quiverquant.com/beta/live/congresstrading"
# Kept for users who mirror the old dataset themselves; the public buckets are gone.
HOUSE_MIRROR = "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json"
SENATE_MIRROR = "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json"

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d")


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
    "filing": ("提交申报", "Filed a report"),
}

# The House uses two-letter codes, the Senate spells the word out.
OWNER_LABEL = {
    "sp": "配偶", "spouse": "配偶",
    "jt": "共同", "joint": "共同",
    "dc": "子女", "child": "子女", "dependent child": "子女",
    "self": "", "": "",
}


def owner_label(raw: str) -> str:
    return OWNER_LABEL.get(str(raw or "").strip().lower(), str(raw or "").strip())


def normalize_type(raw: str | None) -> str:
    key = str(raw or "").strip().lower().replace(" ", "_")
    if key.startswith("sale"):
        if "full" in key:
            return "sale_full"
        if "partial" in key:
            return "sale_partial"
        return "sale"
    if key.startswith("purchase") or key == "buy" or key == "p":
        return "purchase"
    if key.startswith("exchange") or key == "e":
        return "exchange"
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
    asset_type: str = ""
    source: str = ""
    is_equity: bool = True

    @property
    def amount_text(self) -> str:
        if self.amount_low and self.amount_high and self.amount_low != self.amount_high:
            return f"${self.amount_low:,.0f} - ${self.amount_high:,.0f}"
        value = self.amount_high or self.amount_low
        return f"${value:,.0f}" if value else "金额未披露"


# -- House (official Clerk PDFs) ----------------------------------------


def _house_index(ctx: CollectorContext, year: int) -> list[dict[str, str]]:
    """Filing metadata for one year, from the Clerk's ZIP of the disclosure index."""
    raw = ctx.http.get(HOUSE_CLERK_ZIP.format(year=year), allow_404=True)
    if not raw:
        return []
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            xml_bytes = next((zf.read(n) for n in zf.namelist() if n.lower().endswith(".xml")), b"")
    except zipfile.BadZipFile:
        log.warning("House Clerk %sFD.ZIP 不是有效压缩包", year)
        return []
    if not xml_bytes:
        return []
    root = ET.fromstring(xml_bytes)
    return [{child.tag: (child.text or "").strip() for child in member} for member in root.iter("Member")]


def _from_house_clerk(ctx: CollectorContext, state=None) -> list[CongressTrade]:
    lookback = int(ctx.config.get("sources.congress.lookback_days", 45))
    max_filings = int(ctx.config.get("sources.congress.max_filings", 80))
    cutoff = ctx.today - timedelta(days=lookback)

    filings: list[dict[str, str]] = []
    years = {ctx.today.year}
    if (ctx.today - timedelta(days=lookback)).year != ctx.today.year:
        years.add(ctx.today.year - 1)  # a lookback window that straddles New Year
    for year in sorted(years, reverse=True):
        filings.extend(_house_index(ctx, year))
    if not filings:
        raise RuntimeError("众议院官方索引取不到数据")

    recent = [
        f
        for f in filings
        if f.get("FilingType") == "P"
        and (parse_date(f.get("FilingDate")) or date.min) >= cutoff
    ]
    recent.sort(key=lambda f: parse_date(f.get("FilingDate")) or date.min, reverse=True)
    if len(recent) > max_filings:
        log.info("众议院 PTR %d 份，按 max_filings 截断到 %d", len(recent), max_filings)
        recent = recent[:max_filings]

    trades: list[CongressTrade] = []
    for filing in recent:
        doc_id = filing.get("DocID", "")
        year = filing.get("Year") or str(ctx.today.year)
        if not doc_id:
            continue
        url = HOUSE_PTR_PDF.format(year=year, doc_id=doc_id)
        person = " ".join(
            p for p in (filing.get("First", ""), filing.get("Last", ""), filing.get("Suffix", "")) if p
        ).strip()
        filed = parse_date(filing.get("FilingDate"))

        rows = _cached_ptr(ctx, state, doc_id, url)
        if rows is None:
            continue
        if not rows:
            # A scanned PDF has no machine-readable table; the link is still useful.
            trades.append(
                CongressTrade(
                    person=person, chamber="House", ticker="",
                    asset="Periodic Transaction Report（扫描件，需人工查看 PDF）",
                    action="filing", amount_low=0.0, amount_high=0.0,
                    transaction_date=None, disclosure_date=filed, url=url,
                    source="house_clerk", is_equity=True,
                )
            )
            continue
        for row in rows:
            trades.append(
                CongressTrade(
                    person=person,
                    chamber="House",
                    ticker=row["ticker"],
                    asset=row["asset"],
                    action=row["action"],
                    amount_low=row["amount_low"],
                    amount_high=row["amount_high"],
                    transaction_date=parse_date(row["traded"]),
                    disclosure_date=parse_date(row["notified"]) or filed,
                    url=url,
                    owner=row["owner"],
                    asset_type=row["asset_type"],
                    source="house_clerk",
                    is_equity=row["is_equity"],
                )
            )
    return trades


def _cached_ptr(ctx: CollectorContext, state, doc_id: str, url: str) -> list[dict] | None:
    """Parsed rows for one PTR, fetching and caching on first sight. None = fetch failed."""
    key = f"ptr:house:{doc_id}"
    if state is not None:
        cached = state.get_snapshot(key)
        if cached is not None:
            return cached.get("rows", [])
    try:
        pdf = ctx.http.get(url, allow_404=True)
    except Exception as exc:
        log.warning("PTR %s 下载失败: %s", doc_id, exc)
        ctx.notes.append(f"PTR {doc_id} 下载失败: {exc}")
        return None
    if not pdf:
        return None
    rows = [
        {
            "ticker": t.ticker,
            "asset": t.asset,
            "asset_type": t.asset_type,
            "owner": t.owner,
            "action": t.action,
            "traded": t.traded.isoformat() if t.traded else "",
            "notified": t.notified.isoformat() if t.notified else "",
            "amount_low": t.amount_low,
            "amount_high": t.amount_high,
            "is_equity": t.is_equity,
        }
        for t in parse_ptr_pdf(pdf)
    ]
    if state is not None:
        state.put_snapshot(key, {"rows": rows})
    return rows


# -- Senate (official eFD) ----------------------------------------------


def _from_senate_efd(ctx: CollectorContext, state=None) -> list[CongressTrade]:
    lookback = int(ctx.config.get("sources.congress.lookback_days", 45))
    max_filings = int(ctx.config.get("sources.congress.max_filings", 80))
    since = ctx.today - timedelta(days=lookback)

    session = EfdSession(user_agent=ctx.http.user_agent)
    session.open()
    filings = session.search_ptrs(since, limit=max_filings)
    if not filings:
        return []

    trades: list[CongressTrade] = []
    for filing in filings:
        key = f"ptr:senate:{filing.url.rstrip('/').rsplit('/', 1)[-1]}"
        cached = state.get_snapshot(key) if state is not None else None
        if cached is not None:
            rows = cached.get("rows", [])
        else:
            try:
                parsed = session.transactions(filing)
            except Exception as exc:
                log.warning("参议院报告 %s 解析失败: %s", filing.url, exc)
                ctx.notes.append(f"参议院报告解析失败: {exc}")
                continue
            rows = [
                {
                    "ticker": t.ticker,
                    "asset": t.asset,
                    "asset_type": t.asset_type,
                    "owner": t.owner,
                    "action": normalize_type(t.action),
                    "traded": t.traded.isoformat() if t.traded else "",
                    "amount_low": t.amount_low,
                    "amount_high": t.amount_high,
                }
                for t in parsed
            ]
            if state is not None:
                state.put_snapshot(key, {"rows": rows})

        if not rows:
            trades.append(
                CongressTrade(
                    person=filing.person, chamber="Senate", ticker="",
                    asset=filing.title or "Periodic Transaction Report"
                    + ("" if filing.is_electronic else "（扫描件，需人工查看）"),
                    action="filing", amount_low=0.0, amount_high=0.0,
                    transaction_date=None, disclosure_date=filing.filed, url=filing.url,
                    source="senate_efd",
                )
            )
            continue
        for row in rows:
            asset_type = str(row.get("asset_type", ""))
            trades.append(
                CongressTrade(
                    person=filing.person,
                    chamber="Senate",
                    ticker=row["ticker"],
                    asset=row["asset"],
                    action=row["action"],
                    amount_low=row["amount_low"],
                    amount_high=row["amount_high"],
                    transaction_date=parse_date(row["traded"]),
                    disclosure_date=filing.filed,
                    url=filing.url,
                    owner=row["owner"],
                    asset_type=asset_type,
                    source="senate_efd",
                    is_equity=bool(row["ticker"]) or "stock" in asset_type.lower(),
                )
            )
    return trades


# -- optional providers --------------------------------------------------


def _from_quiver(ctx: CollectorContext, state=None) -> list[CongressTrade]:
    import json

    token = os.environ.get("QUIVER_API_KEY", "")
    if not token:
        raise RuntimeError("QUIVER_API_KEY 未设置")
    body = ctx.http.get(QUIVER_URL, headers={"Authorization": f"Bearer {token}"})
    trades: list[CongressTrade] = []
    for row in json.loads(body or b"[]"):
        low, high = parse_amount(row.get("Range"))
        ticker = str(row.get("Ticker") or "").upper().strip()
        trades.append(
            CongressTrade(
                person=str(row.get("Representative") or "Unknown").strip(),
                chamber=str(row.get("House") or "").title() or "Congress",
                ticker=ticker,
                asset=ticker,
                action=normalize_type(row.get("Transaction")),
                amount_low=low,
                amount_high=high,
                transaction_date=parse_date(row.get("TransactionDate")),
                disclosure_date=parse_date(row.get("ReportDate")),
                url="https://www.quiverquant.com/congresstrading/",
                source="quiver",
                is_equity=bool(ticker),
            )
        )
    return trades


def _from_stockwatcher(ctx: CollectorContext, state=None) -> list[CongressTrade]:
    """The old community mirrors. Public access was withdrawn; kept for self-hosted copies."""
    trades: list[CongressTrade] = []
    for url, chamber, person_key in (
        (ctx.config.get("sources.congress.house_mirror_url") or HOUSE_MIRROR, "House", "representative"),
        (ctx.config.get("sources.congress.senate_mirror_url") or SENATE_MIRROR, "Senate", "senator"),
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
                    is_equity=bool(ticker),
                )
            )
    return trades


PROVIDERS = {
    "house_clerk": _from_house_clerk,
    "senate_efd": _from_senate_efd,
    "quiver": _from_quiver,
    "stockwatcher": _from_stockwatcher,
}


class CongressCollector:
    name = "congress"

    def __init__(self, state=None) -> None:
        self.state = state

    def enabled(self, ctx: CollectorContext) -> bool:
        return bool(ctx.config.get("sources.congress.enabled", True))

    def collect(self, ctx: CollectorContext) -> list[Item]:
        cfg = ctx.config
        names = [str(p) for p in cfg.get("sources.congress.providers", ["house_clerk", "senate_efd"])]
        lookback = int(cfg.get("sources.congress.lookback_days", 45))
        min_amount = float(cfg.get("sources.congress.min_amount_usd", 1000))
        stocks_only = bool(cfg.get("sources.congress.stocks_only", True))
        watchlist_only = bool(cfg.get("sources.congress.watchlist_only", False))
        tickers = set(cfg.tickers)
        people = {str(p).lower() for p in cfg.get("watchlist.people", [])}
        cutoff = ctx.today - timedelta(days=lookback)

        # Providers cover different chambers, so all of them run and the results merge;
        # one chamber being down must not hide the other.
        trades: list[CongressTrade] = []
        errors: list[str] = []
        for provider_name in names:
            provider = PROVIDERS.get(provider_name)
            if provider is None:
                errors.append(f"{provider_name}: 未知的 provider")
                continue
            try:
                got = provider(ctx, self.state)
                log.info("congress provider %s 返回 %d 条", provider_name, len(got))
                trades.extend(got)
            except Exception as exc:
                log.warning("congress provider %s 失败: %s", provider_name, exc)
                errors.append(f"{provider_name}: {exc}")

        if not trades:
            raise RuntimeError("; ".join(errors) or "所有 provider 都没有返回数据")
        for message in errors:
            ctx.notes.append(message)

        items: list[Item] = []
        for trade in trades:
            stamp = trade.disclosure_date or trade.transaction_date
            if stamp is None or stamp < cutoff or stamp > ctx.today + timedelta(days=1):
                continue
            if stocks_only and not trade.is_equity:
                continue
            if trade.amount_high and trade.amount_high < min_amount:
                continue
            watched_person = trade.person.lower() in people
            watched_ticker = bool(trade.ticker and trade.ticker in tickers)
            if watchlist_only and not (watched_person or watched_ticker):
                continue

            zh, en = TYPE_LABEL.get(trade.action, (trade.action, trade.action))
            label = trade.ticker or (trade.asset[:44] or "未披露标的")
            owner = owner_label(trade.owner)
            owner_tag = f"[{owner}]" if owner and owner != "--" else ""
            title = f"{trade.person} ({trade.chamber}) {zh} {label} {owner_tag}".strip()
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
            if trade.action == "filing":
                score -= 25  # a bare filing pointer is weaker than a parsed transaction

            summary_bits = [trade.amount_text if trade.action != "filing" else "详见 PDF 原文"]
            if trade.transaction_date:
                summary_bits.append(f"交易日 {trade.transaction_date}{lag}")
            if trade.ticker and trade.asset and trade.ticker not in trade.asset:
                summary_bits.append(trade.asset[:60])

            item = Item(
                kind="congress",
                title=title,
                url=trade.url,
                when=when,
                summary=" · ".join(b for b in summary_bits if b),
                tickers=[trade.ticker] if trade.ticker else [],
                score=score,
                source=f"{trade.chamber} 官方披露 ({trade.source})",
                detail={
                    "person": trade.person,
                    "chamber": trade.chamber,
                    "ticker": trade.ticker,
                    "asset": trade.asset,
                    "asset_type": trade.asset_type,
                    "action": trade.action,
                    "action_en": en,
                    "amount_low": trade.amount_low,
                    "amount_high": trade.amount_high,
                    "transaction_date": trade.transaction_date,
                    "disclosure_date": trade.disclosure_date,
                    "owner": trade.owner,
                    "provider": trade.source,
                },
            )
            items.append(
                item.with_key(
                    trade.source, trade.person, trade.ticker, trade.asset[:40], trade.action,
                    trade.transaction_date, trade.disclosure_date, trade.amount_high,
                )
            )
        return items
