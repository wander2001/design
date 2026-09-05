"""Fund moves — SEC 13F-HR holdings diffs and 13D/13G stake disclosures (基金变化).

13F is quarterly and lands ~45 days after quarter end, so on most days this
section is empty by design; 13D/13G are event-driven and show up within days.
"""

from __future__ import annotations

import logging
import statistics
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, timedelta

from ..edgar import IndexRow, strip_ns, text_of
from ..models import Item
from .base import CollectorContext

log = logging.getLogger(__name__)

THIRTEEN_F = {"13F-HR", "13F-HR/A"}
STAKE_FORMS = {"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A"}


@dataclass
class Holding:
    issuer: str
    cusip: str
    value_usd: float
    shares: float
    kind: str  # SH | PRN
    put_call: str = ""

    @property
    def slot(self) -> str:
        return f"{self.cusip}|{self.put_call}"


def parse_info_table(xml_bytes: bytes) -> list[Holding]:
    """Parse a 13F information table, normalizing the reported value to dollars.

    Filers reported ``<value>`` in thousands before the 2023 amendments and in
    whole dollars after, and old-format filings still appear in amendments, so the
    unit is inferred from the implied per-share price rather than assumed.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        log.warning("unparseable 13F information table: %s", exc)
        return []

    holdings: list[Holding] = []
    for node in root.iter():
        if strip_ns(node.tag) != "infoTable":
            continue
        shares_node = None
        for child in node:
            if strip_ns(child.tag) == "shrsOrPrnAmt":
                shares_node = child
                break
        try:
            value = float(text_of(node, "value") or 0)
        except ValueError:
            value = 0.0
        try:
            shares = float(text_of(shares_node, "sshPrnamt") or 0) if shares_node is not None else 0.0
        except ValueError:
            shares = 0.0
        holdings.append(
            Holding(
                issuer=text_of(node, "nameOfIssuer"),
                cusip=(text_of(node, "cusip") or "").upper(),
                value_usd=value,
                shares=shares,
                kind=(text_of(shares_node, "sshPrnamtType") if shares_node is not None else "") or "SH",
                put_call=text_of(node, "putCall"),
            )
        )

    prices = [h.value_usd / h.shares for h in holdings if h.shares > 0 and h.value_usd > 0 and h.kind == "SH"]
    if prices and statistics.median(prices) < 0.5:
        # Values are in thousands of dollars; scale to dollars.
        for h in holdings:
            h.value_usd *= 1000
    return holdings


@dataclass
class HoldingChange:
    kind: str  # new | exit | increase | decrease
    issuer: str
    cusip: str
    old_shares: float
    new_shares: float
    old_value: float
    new_value: float

    @property
    def delta_shares(self) -> float:
        return self.new_shares - self.old_shares

    @property
    def pct(self) -> float:
        if self.old_shares <= 0:
            return 100.0 if self.new_shares > 0 else 0.0
        return (self.new_shares - self.old_shares) / self.old_shares * 100.0

    @property
    def magnitude(self) -> float:
        return max(self.new_value, self.old_value)


def diff_holdings(
    old: list[Holding], new: list[Holding], min_pct: float = 10.0
) -> list[HoldingChange]:
    """Position-level differences between two 13F snapshots of the same filer."""
    old_map = {h.slot: h for h in old}
    new_map = {h.slot: h for h in new}
    changes: list[HoldingChange] = []

    for slot, h in new_map.items():
        prev = old_map.get(slot)
        if prev is None:
            changes.append(HoldingChange("new", h.issuer, h.cusip, 0, h.shares, 0, h.value_usd))
        elif prev.shares > 0 and abs(h.shares - prev.shares) / prev.shares * 100 >= min_pct:
            kind = "increase" if h.shares > prev.shares else "decrease"
            changes.append(
                HoldingChange(kind, h.issuer, h.cusip, prev.shares, h.shares, prev.value_usd, h.value_usd)
            )
    for slot, prev in old_map.items():
        if slot not in new_map:
            changes.append(HoldingChange("exit", prev.issuer, prev.cusip, prev.shares, 0, prev.value_usd, 0))

    changes.sort(key=lambda c: -c.magnitude)
    return changes


CHANGE_LABEL = {
    "new": ("新建仓", "New position"),
    "exit": ("清仓", "Exited"),
    "increase": ("加仓", "Increased"),
    "decrease": ("减仓", "Reduced"),
}


def _fmt_money(value: float) -> str:
    if value >= 1_000_000_000:
        return f"${value/1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value/1_000_000:.1f}M"
    return f"${value:,.0f}"


class FundCollector:
    name = "funds"

    def __init__(self, state=None) -> None:
        self.state = state

    def enabled(self, ctx: CollectorContext) -> bool:
        return bool(ctx.config.get("sources.funds.enabled", True))

    def collect(self, ctx: CollectorContext) -> list[Item]:
        items: list[Item] = []
        items.extend(self._thirteen_f(ctx))
        items.extend(self._stakes(ctx))
        return items

    # -- 13F -------------------------------------------------------------
    def _thirteen_f(self, ctx: CollectorContext) -> list[Item]:
        cfg = ctx.config
        funds = cfg.funds
        if not funds:
            return []
        lookback = int(cfg.get("sources.funds.lookback_days", 7))
        top_n = int(cfg.get("sources.funds.top_n_changes", 12))
        min_value = float(cfg.get("sources.funds.min_value_usd", 5_000_000))
        cutoff = ctx.today - timedelta(days=lookback)

        items: list[Item] = []
        for fund in funds:
            try:
                items.extend(self._one_fund(ctx, fund, cutoff, top_n, min_value))
            except Exception as exc:
                log.warning("13F for %s (%s) failed: %s", fund["name"], fund["cik"], exc)
                ctx.notes.append(f"{fund['name']} 13F 抓取失败: {exc}")
        return items

    def _one_fund(
        self, ctx: CollectorContext, fund: dict, cutoff: date, top_n: int, min_value: float
    ) -> list[Item]:
        filings = ctx.edgar.recent_filings(fund["cik"], THIRTEEN_F, limit=4)
        if not filings:
            return []
        latest = filings[0]
        if latest.filed < cutoff:
            return []  # nothing new since last quarter's filing

        snap_key = f"13f:{fund['cik']}"
        cached = self.state.get_snapshot(snap_key) if self.state else None
        if cached and cached.get("accession") == latest.accession:
            return []  # already diffed this filing on an earlier run

        new_body = ctx.edgar.primary_xml(latest)
        if not new_body:
            log.warning("13F %s: no information table found", latest.accession)
            ctx.notes.append(f"{fund['name']} 13F {latest.accession} 未找到持仓表")
            return []
        new_holdings = parse_info_table(new_body)
        if not new_holdings:
            return []

        old_holdings: list[Holding] = []
        if cached and cached.get("holdings"):
            old_holdings = [Holding(**h) for h in cached["holdings"]]
        elif len(filings) > 1:
            prev_body = ctx.edgar.primary_xml(filings[1])
            if prev_body:
                old_holdings = parse_info_table(prev_body)

        if self.state:
            self.state.put_snapshot(
                snap_key,
                {
                    "accession": latest.accession,
                    "filed": latest.filed.isoformat(),
                    "holdings": [h.__dict__ for h in new_holdings],
                },
            )

        name = fund["name"]
        if not old_holdings:
            # First time we see this filer: report its largest positions as a baseline.
            top = sorted(new_holdings, key=lambda h: -h.value_usd)[:top_n]
            lines = ", ".join(f"{h.issuer} ({_fmt_money(h.value_usd)})" for h in top)
            item = Item(
                kind="fund",
                title=f"{name} · 首次跟踪，最新 13F 前 {len(top)} 大持仓",
                url=latest.index_url,
                when=latest.filed,
                summary=lines,
                score=50.0,
                source="SEC 13F-HR",
                detail={"fund": name, "cik": fund["cik"], "accession": latest.accession, "baseline": True},
            )
            return [item.with_key(fund["cik"], latest.accession, "baseline")]

        changes = [c for c in diff_holdings(old_holdings, new_holdings) if c.magnitude >= min_value]
        items: list[Item] = []
        for change in changes[:top_n]:
            zh, en = CHANGE_LABEL[change.kind]
            if change.kind in ("new", "exit"):
                detail_txt = f"{change.new_shares or change.old_shares:,.0f} 股 · {_fmt_money(change.magnitude)}"
            else:
                detail_txt = (
                    f"{change.old_shares:,.0f} → {change.new_shares:,.0f} 股 "
                    f"({change.pct:+.1f}%) · {_fmt_money(change.new_value)}"
                )
            base = {"new": 75.0, "exit": 70.0, "increase": 55.0, "decrease": 55.0}[change.kind]
            item = Item(
                kind="fund",
                title=f"{name} {zh} {change.issuer}",
                url=latest.index_url,
                when=latest.filed,
                summary=detail_txt,
                score=base + min(20.0, change.magnitude / 100_000_000),
                source="SEC 13F-HR",
                detail={
                    "fund": name,
                    "cik": fund["cik"],
                    "change": change.kind,
                    "change_en": en,
                    "issuer": change.issuer,
                    "cusip": change.cusip,
                    "old_shares": change.old_shares,
                    "new_shares": change.new_shares,
                    "value_usd": change.magnitude,
                    "accession": latest.accession,
                },
            )
            items.append(item.with_key(fund["cik"], latest.accession, change.cusip, change.kind))
        return items

    # -- 13D / 13G -------------------------------------------------------
    def _stakes(self, ctx: CollectorContext) -> list[Item]:
        """Large-stake disclosures touching the watchlist or a tracked fund."""
        from ..edgar import business_days

        cfg = ctx.config
        tickers = cfg.tickers
        fund_ciks = {str(int(f["cik"])) for f in cfg.funds}
        if not tickers and not fund_ciks:
            return []

        lookback = int(cfg.get("sources.funds.lookback_days", 7))
        days = business_days(ctx.today, min(lookback, 10))
        rows = ctx.edgar.filings_of_type(days, STAKE_FORMS)
        if not rows:
            return []

        cik_to_ticker = {str(int(c)): t for t, c in ctx.edgar.ciks_for_tickers(tickers).items()}
        by_accession: dict[str, list[IndexRow]] = {}
        for row in rows:
            by_accession.setdefault(row.accession, []).append(row)

        items: list[Item] = []
        for accession, group in by_accession.items():
            ciks = {str(int(r.cik)) for r in group}
            hit_tickers = sorted({cik_to_ticker[c] for c in ciks if c in cik_to_ticker})
            hit_funds = ciks & fund_ciks
            if not hit_tickers and not hit_funds:
                continue
            row = group[0]
            names = " / ".join(dict.fromkeys(r.company for r in group))
            label = ", ".join(hit_tickers) if hit_tickers else names
            item = Item(
                kind="fund",
                title=f"{label} · {row.form_type} 大额持股披露",
                url=row.index_url,
                when=row.filed,
                summary=names,
                tickers=hit_tickers,
                score=80.0 if row.form_type.startswith("SC 13D") else 65.0,
                source=f"SEC {row.form_type}",
                detail={"form": row.form_type, "parties": names, "accession": accession},
            )
            items.append(item.with_key(accession, row.form_type))
        return items
