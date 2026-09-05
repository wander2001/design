"""Corporate insider trades — SEC Form 4 (企业高管买卖).

Form 4 is filed within two business days of an officer/director/10%-owner trade,
so the daily index is the freshest public view of executive buying and selling.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from ..edgar import IndexRow, strip_ns, text_of
from ..models import Item
from .base import CollectorContext

log = logging.getLogger(__name__)

FORMS = {"4", "4/A"}

# Table I/II transaction codes worth a line in a daily digest.
CODE_LABEL = {
    "P": ("买入", "Open-market purchase"),
    "S": ("卖出", "Open-market sale"),
    "A": ("授予", "Grant / award"),
    "D": ("处置", "Disposition to issuer"),
    "F": ("缴税扣股", "Shares withheld for taxes"),
    "M": ("期权行权", "Option exercise"),
    "G": ("赠与", "Gift"),
    "C": ("转换", "Conversion"),
    "X": ("行权", "Exercise of in-the-money derivative"),
}


@dataclass
class InsiderTrade:
    accession: str
    issuer: str
    ticker: str
    owner: str
    title: str
    code: str
    shares: float
    price: float
    value: float
    when: date | None
    acquired: bool
    derivative: bool
    url: str
    shares_after: float = 0.0
    # A 4/A restates an earlier Form 4; counting both double-counts the trade.
    amendment: bool = False
    original_filed: str = ""
    # Shares held through a trust or another entity rather than in the insider's
    # own name — the same dollar amount means something different.
    indirect: bool = False
    ownership_note: str = ""


def _num(raw: str) -> float:
    try:
        return float(raw.replace(",", "").replace("$", ""))
    except (ValueError, AttributeError):
        return 0.0


def _owner_title(rel: ET.Element | None) -> str:
    if rel is None:
        return ""
    bits = []
    if text_of(rel, "isDirector") in ("1", "true"):
        bits.append("Director")
    if text_of(rel, "isOfficer") in ("1", "true"):
        bits.append(text_of(rel, "officerTitle") or "Officer")
    if text_of(rel, "isTenPercentOwner") in ("1", "true"):
        bits.append("10% Owner")
    if text_of(rel, "isOther") in ("1", "true"):
        bits.append(text_of(rel, "otherText") or "Other")
    return ", ".join(bits)


def _find(node: ET.Element, name: str) -> ET.Element | None:
    for child in node:
        if strip_ns(child.tag) == name:
            return child
    return None


def parse_form4(xml_bytes: bytes, accession: str, url: str) -> list[InsiderTrade]:
    """Parse one ownership document into per-transaction rows."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        log.warning("unparseable Form 4 %s: %s", accession, exc)
        return []

    original_filed = text_of(root, "dateOfOriginalSubmission")
    is_amendment = bool(original_filed) or text_of(root, "documentType").endswith("/A")

    issuer_node = _find(root, "issuer")
    issuer = text_of(issuer_node, "issuerName")
    ticker = (text_of(issuer_node, "issuerTradingSymbol") or "").upper().strip()

    owners: list[tuple[str, str]] = []
    for child in root:
        if strip_ns(child.tag) != "reportingOwner":
            continue
        name = text_of(_find(child, "reportingOwnerId"), "rptOwnerName")
        owners.append((name, _owner_title(_find(child, "reportingOwnerRelationship"))))
    owner_name = "; ".join(n for n, _ in owners if n) or "Unknown"
    owner_title = "; ".join(dict.fromkeys(t for _, t in owners if t))

    trades: list[InsiderTrade] = []
    for table_name, is_deriv in (("nonDerivativeTable", False), ("derivativeTable", True)):
        table = _find(root, table_name)
        if table is None:
            continue
        for txn in table:
            if not strip_ns(txn.tag).endswith("Transaction"):
                continue
            coding = _find(txn, "transactionCoding")
            code = text_of(coding, "transactionCode")
            amounts = _find(txn, "transactionAmounts")
            shares = _num(text_of(amounts, "transactionShares"))
            price = _num(text_of(amounts, "transactionPricePerShare"))
            acquired = text_of(amounts, "transactionAcquiredDisposedCode").upper() == "A"
            nature = _find(txn, "ownershipNature")
            indirect = text_of(nature, "directOrIndirectOwnership").upper().startswith("I")
            ownership_note = text_of(nature, "natureOfOwnership")
            when_raw = text_of(txn, "transactionDate")
            try:
                when = date.fromisoformat(when_raw) if when_raw else None
            except ValueError:
                when = None
            trades.append(
                InsiderTrade(
                    accession=accession,
                    issuer=issuer,
                    ticker=ticker,
                    owner=owner_name,
                    title=owner_title,
                    code=code,
                    shares=shares,
                    price=price,
                    value=shares * price,
                    when=when,
                    acquired=acquired,
                    derivative=is_deriv,
                    url=url,
                    shares_after=_num(
                        text_of(_find(txn, "postTransactionAmounts"), "sharesOwnedFollowingTransaction")
                    ),
                    amendment=is_amendment,
                    original_filed=original_filed,
                    indirect=indirect,
                    ownership_note=ownership_note,
                )
            )
    return trades


def _fmt_money(value: float) -> str:
    if value >= 1_000_000_000:
        return f"${value/1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value/1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value/1_000:.0f}K"
    return f"${value:,.0f}"


class InsiderCollector:
    name = "insiders"

    def enabled(self, ctx: CollectorContext) -> bool:
        return bool(ctx.config.get("sources.insiders.enabled", True))

    def _select_rows(self, ctx: CollectorContext, rows: list[IndexRow]) -> list[IndexRow]:
        """One row per accession, restricted to the watchlist unless scope=all."""
        cfg = ctx.config
        scope = str(cfg.get("sources.insiders.scope", "watchlist")).lower()
        max_filings = int(cfg.get("sources.insiders.max_filings", 400))

        if scope == "watchlist":
            tickers = cfg.tickers
            if not tickers:
                log.info("insiders: scope=watchlist but watchlist is empty; nothing to do")
                return []
            wanted_ciks = {str(int(c)) for c in ctx.edgar.ciks_for_tickers(tickers).values()}
            rows = [r for r in rows if str(int(r.cik)) in wanted_ciks]

        by_accession: dict[str, IndexRow] = {}
        for row in rows:
            by_accession.setdefault(row.accession, row)
        selected = sorted(by_accession.values(), key=lambda r: r.filed, reverse=True)
        if len(selected) > max_filings:
            log.warning("insiders: %d filings exceed max_filings=%d, truncating", len(selected), max_filings)
            selected = selected[:max_filings]
        return selected

    def collect(self, ctx: CollectorContext) -> list[Item]:
        from ..edgar import business_days

        cfg = ctx.config
        lookback = int(cfg.get("sources.insiders.lookback_days", 3))
        codes = {str(c).upper() for c in cfg.get("sources.insiders.codes", ["P", "S"])}
        min_value = float(cfg.get("sources.insiders.min_value_usd", 100000))
        workers = int(cfg.get("sources.insiders.workers", 6))

        days = business_days(ctx.today, lookback)
        rows = self._select_rows(ctx, ctx.edgar.filings_of_type(days, FORMS))
        if not rows:
            return []

        def fetch(row: IndexRow) -> list[InsiderTrade]:
            try:
                body = ctx.edgar.ownership_xml(row)
            except Exception as exc:
                log.warning("Form 4 %s fetch failed: %s", row.accession, exc)
                ctx.notes.append(f"Form 4 {row.accession} 抓取失败: {exc}")
                return []
            if not body:
                return []
            return parse_form4(body, row.accession, row.index_url)

        trades: list[InsiderTrade] = []
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for result in pool.map(fetch, rows):
                trades.extend(result)

        return list(self._to_items(trades, codes, min_value))

    @staticmethod
    def _to_items(trades: Iterable[InsiderTrade], codes: set[str], min_value: float) -> Iterable[Item]:
        for idx, t in enumerate(trades):
            if codes and t.code.upper() not in codes:
                continue
            if t.value < min_value:
                continue
            zh, en = CODE_LABEL.get(t.code.upper(), (t.code, t.code))
            direction = zh if not t.derivative else f"{zh}(衍生品)"
            label = t.ticker or t.issuer
            tags = "".join(
                [
                    "【修正申报】" if t.amendment else "",
                    "[间接持有]" if t.indirect else "",
                ]
            )
            title = f"{label} · {t.owner} {direction} {t.shares:,.0f} 股 ({_fmt_money(t.value)}){tags}"
            summary_bits = [f"{t.title}" if t.title else "", f"成交价 ${t.price:,.2f}" if t.price else ""]
            if t.shares_after:
                summary_bits.append(f"交易后持股 {t.shares_after:,.0f}")
            if t.amendment:
                summary_bits.append(
                    f"这是对{('（原申报日 ' + t.original_filed + '）') if t.original_filed else ''}既有申报的修正，"
                    "不是新增交易，统计时勿与原件重复计数"
                )
            if t.indirect and t.ownership_note:
                summary_bits.append(f"持有形式: {t.ownership_note}")
            # A purchase by an officer is the highest-signal insider event; scale by size.
            base = 60.0 if t.code.upper() == "P" else 40.0
            score = base + min(30.0, t.value / 1_000_000)
            item = Item(
                kind="insider",
                title=title,
                url=t.url,
                when=t.when,
                summary=" · ".join(b for b in summary_bits if b),
                tickers=[t.ticker] if t.ticker else [],
                score=score,
                source="SEC Form 4",
                detail={
                    "issuer": t.issuer,
                    "ticker": t.ticker,
                    "owner": t.owner,
                    "owner_title": t.title,
                    "code": t.code,
                    "code_en": en,
                    "shares": t.shares,
                    "price": t.price,
                    "value_usd": t.value,
                    "acquired": t.acquired,
                    "derivative": t.derivative,
                    "accession": t.accession,
                    "amendment": t.amendment,
                    "original_filed": t.original_filed,
                    "indirect": t.indirect,
                    "ownership_note": t.ownership_note,
                },
            )
            yield item.with_key(t.accession, idx, t.code, t.shares, t.price)
