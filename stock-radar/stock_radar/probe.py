"""Live diagnostics: hit every real data source and check our format assumptions.

Run this after changing a parser, when a section goes quiet, or on a new machine.
It is deliberately verbose — the point is to tell you *which* assumption broke,
not merely that something did.
"""

from __future__ import annotations

import statistics
import sys
import traceback
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

from .config import Config
from .edgar import Edgar, business_days, strip_ns
from .http import Http

PASS, FAIL, WARN, INFO = "PASS", "FAIL", "WARN", "INFO"


class Probe:
    def __init__(self, config: Config, today: date | None = None, verbose: bool = False) -> None:
        self.config = config
        self.today = today or datetime.now(timezone.utc).date()
        self.verbose = verbose
        self.http = Http(
            user_agent=config.user_agent or None,
            rate_per_sec=float(config.get("sec.rate_per_sec", 5.0)),
            timeout=45,
            retries=2,
        )
        self.edgar = Edgar(self.http)
        self.results: list[tuple[str, str, str]] = []
        # _house_index() takes a CollectorContext and only touches .http and .config,
        # both of which the probe already has; notes collects what collectors would log.
        self.notes: list[str] = []

    # -- reporting -------------------------------------------------------
    def record(self, level: str, check: str, detail: str = "") -> None:
        self.results.append((level, check, detail))
        icon = {PASS: "✅", FAIL: "❌", WARN: "⚠️ ", INFO: "  "}[level]
        print(f"{icon} [{level}] {check}" + (f"\n       {detail}" if detail else ""), flush=True)

    def section(self, title: str) -> None:
        print(f"\n{'=' * 78}\n== {title}\n{'=' * 78}", flush=True)

    def guard(self, check: str, fn):
        """Run one check; an exception becomes a FAIL instead of ending the probe."""
        try:
            return fn()
        except Exception as exc:
            self.record(FAIL, check, f"{type(exc).__name__}: {exc}")
            hint = getattr(exc, "hint", "")
            if hint:
                self.record(INFO, "  ↳ 怎么办", hint)
            if self.verbose:
                traceback.print_exc()
            return None

    # -- checks ----------------------------------------------------------
    def check_daily_index(self) -> list:
        self.section("A. EDGAR 日索引 (daily-index master.idx)")
        rows_by_day = []
        for day in business_days(self.today, 5):
            rows = self.guard(f"daily index {day}", lambda d=day: self.edgar.daily_index(d))
            if rows is None:
                continue
            if not rows:
                self.record(INFO, f"daily index {day}", "空（周末/假日或尚未发布）")
                continue
            counts: dict[str, int] = {}
            for row in rows:
                counts[row.form_type] = counts.get(row.form_type, 0) + 1
            interesting = {k: counts.get(k, 0) for k in ("4", "8-K", "13F-HR", "SC 13D/A", "SC 13G/A")}
            self.record(PASS, f"daily index {day}", f"{len(rows)} 行；关键表种: {interesting}")
            rows_by_day.append((day, rows))
            if not rows_by_day[:-1]:  # print raw samples once
                for row in rows[:2]:
                    self.record(INFO, "样本行", f"{row.form_type} | {row.company} | {row.accession} | {row.filename}")
        if not rows_by_day:
            self.record(FAIL, "日索引整体", "5 个工作日都没拿到数据")
        return rows_by_day

    def check_ticker_map(self) -> None:
        self.section("B. CIK ↔ ticker 映射 (company_tickers.json)")
        mapping = self.guard("company_tickers.json", self.edgar.ticker_map)
        if not mapping:
            return
        self.record(PASS, "company_tickers.json", f"{len(mapping)} 家公司")
        known = {"320193": "AAPL", "1045810": "NVDA", "789019": "MSFT"}
        for cik, expected in known.items():
            actual = mapping.get(cik)
            level = PASS if actual == expected else FAIL
            self.record(level, f"CIK {cik} → {expected}", f"实际: {actual}")
        found = self.edgar.ciks_for_tickers(["AAPL", "NVDA", "BRK-B", "BRK.B"])
        self.record(INFO, "ciks_for_tickers 反查", str(found))

    def check_form4(self, rows_by_day: list) -> None:
        self.section("C. Form 4 高管交易解析")
        from .collectors.insiders import parse_form4

        candidates = []
        for _, rows in rows_by_day:
            candidates += [r for r in rows if r.form_type == "4"]
        if not candidates:
            self.record(FAIL, "Form 4 样本", "日索引里没有 Form 4")
            return
        self.record(INFO, "Form 4 总量", f"最近几日共 {len(candidates)} 行（含 issuer/owner 重复行）")

        seen: dict[str, object] = {}
        for row in candidates:
            seen.setdefault(row.accession, row)
        sample = list(seen.values())[:8]

        parsed_ok = with_ticker = with_txn = 0
        for row in sample:
            files = self.guard(f"{row.accession} 文件列表", lambda r=row: self.edgar.filing_files(r))
            names = [f.get("name") for f in (files or [])]
            body = self.guard(f"{row.accession} XML", lambda r=row: self.edgar.ownership_xml(r))
            if not body:
                self.record(FAIL, f"Form 4 {row.accession}", f"取不到 ownership XML；目录: {names}")
                continue
            trades = parse_form4(body, row.accession, row.index_url)
            parsed_ok += 1
            if not trades:
                root_tags = []
                try:
                    root_tags = [strip_ns(c.tag) for c in ET.fromstring(body)]
                except ET.ParseError:
                    pass
                self.record(WARN, f"Form 4 {row.accession}", f"0 笔交易（可能是纯持仓申报）；根子节点: {root_tags}")
                continue
            with_txn += 1
            t = trades[0]
            if t.ticker:
                with_ticker += 1
            self.record(
                PASS,
                f"Form 4 {row.accession}",
                f"{t.ticker or '(无代码)'} | {t.issuer} | {t.owner} [{t.title}] | "
                f"{len(trades)} 笔 | 首笔 code={t.code} shares={t.shares:,.0f} px=${t.price:,.2f} "
                f"value=${t.shares * t.price:,.0f} date={t.when}",
            )
        level = PASS if with_txn >= max(1, len(sample) // 2) else FAIL
        self.record(level, "Form 4 解析成功率", f"{parsed_ok}/{len(sample)} 拿到 XML，{with_txn} 有交易，{with_ticker} 有股票代码")

    def check_13f(self) -> None:
        self.section("D. 13F 基金持仓")
        from .collectors.funds import diff_holdings, parse_info_table

        funds = self.config.funds or [{"name": "Berkshire Hathaway", "cik": "0001067983"}]
        for fund in funds[:3]:
            filings = self.guard(
                f"{fund['name']} submissions",
                lambda f=fund: self.edgar.recent_filings(f["cik"], {"13F-HR", "13F-HR/A"}, limit=4),
            )
            if not filings:
                self.record(FAIL, f"{fund['name']} 13F 列表", "submissions API 没有返回 13F-HR")
                continue
            self.record(
                PASS,
                f"{fund['name']} 13F 列表",
                ", ".join(f"{f.form_type}@{f.filed}({f.accession})" for f in filings),
            )

            snapshots = []
            for filing in filings[:2]:
                files = self.guard(f"{filing.accession} 目录", lambda r=filing: self.edgar.filing_files(r))
                self.record(INFO, f"{filing.accession} 目录", str([f.get("name") for f in (files or [])]))
                body = self.guard(f"{filing.accession} 持仓表", lambda r=filing: self.edgar.primary_xml(r))
                if not body:
                    self.record(FAIL, f"{filing.accession} 持仓表", "primary_xml 没找到信息表")
                    continue
                holdings = parse_info_table(body)
                if not holdings:
                    self.record(FAIL, f"{filing.accession} 解析", f"0 条持仓；XML 前 200 字节: {body[:200]!r}")
                    continue
                total = sum(h.value_usd for h in holdings)
                prices = [h.value_usd / h.shares for h in holdings if h.shares > 0 and h.value_usd > 0 and h.kind == "SH"]
                median_px = statistics.median(prices) if prices else 0
                top = sorted(holdings, key=lambda h: -h.value_usd)[:5]
                self.record(
                    PASS,
                    f"{filing.accession} 解析",
                    f"{len(holdings)} 条持仓；组合总市值 ${total/1e9:,.1f}B；每股隐含价中位数 ${median_px:,.2f}",
                )
                # A median implied price outside a plausible equity range means the
                # thousands-vs-dollars normalization picked wrong — a 1000x error.
                level = PASS if 1 <= median_px <= 5000 else FAIL
                self.record(level, f"{filing.accession} 金额单位判定", f"归一化后每股 ${median_px:,.2f}（合理区间 $1–$5000）")
                for h in top:
                    self.record(INFO, "  前五持仓", f"{h.issuer:38} ${h.value_usd/1e9:8,.2f}B  {h.shares:>15,.0f} 股 {h.kind}")
                snapshots.append((filing, holdings))

            if len(snapshots) == 2:
                changes = diff_holdings(snapshots[1][1], snapshots[0][1])
                self.record(PASS, f"{fund['name']} 季度 diff", f"{len(changes)} 项变动（{snapshots[1][0].filed} → {snapshots[0][0].filed}）")
                for c in changes[:8]:
                    self.record(INFO, "  变动", f"{c.kind:9} {c.issuer:34} {c.old_shares:>14,.0f} → {c.new_shares:>14,.0f} ({c.pct:+.1f}%)")

    def check_stakes(self, rows_by_day: list) -> None:
        self.section("E. 13D / 13G 大额持股披露")
        stake_rows = [r for _, rows in rows_by_day for r in rows if r.form_type.startswith("SC 13")]
        if not stake_rows:
            self.record(WARN, "13D/13G", "最近几日索引里没有（正常，事件驱动）")
            return
        self.record(PASS, "13D/13G", f"{len(stake_rows)} 行")
        for row in stake_rows[:5]:
            self.record(INFO, "  样本", f"{row.form_type} | CIK {row.cik} | {row.company} | {row.accession}")

    def check_congress(self) -> None:
        """Walk the official pipeline: index -> filing -> parsed transactions."""
        self.section("F. 国会议员交易（官方源）")
        from .collectors import congress as C
        from .house_ptr import parse_ptr_pdf

        # -- House: yearly ZIP index -> PTR PDF -> rows
        members = self.guard("众议院年度索引", lambda: C._house_index(self, self.today.year))
        if members:
            ptrs = [m for m in members if m.get("FilingType") == "P"]
            self.record(PASS, "众议院年度索引", f"{len(members)} 条申报，其中 PTR {len(ptrs)} 条")
            if ptrs:
                self.record(INFO, "  索引字段", str(sorted(ptrs[-1].keys())))
                recent = sorted(
                    ptrs, key=lambda m: C.parse_date(m.get("FilingDate")) or date.min, reverse=True
                )
                newest = C.parse_date(recent[0].get("FilingDate"))
                lag = (self.today - newest).days if newest else None
                self.record(
                    PASS if lag is not None and lag <= 30 else WARN,
                    "  众议院新鲜度",
                    f"最新 PTR 申报日 {newest}（距今 {lag} 天）",
                )
                parsed_rows = equity_rows = 0
                for filing in recent[:5]:
                    url = C.HOUSE_PTR_PDF.format(
                        year=filing.get("Year") or self.today.year, doc_id=filing.get("DocID")
                    )
                    pdf = self.guard(f"PTR {filing.get('DocID')}", lambda u=url: self.http.get(u, allow_404=True))
                    if not pdf:
                        continue
                    rows = parse_ptr_pdf(pdf)
                    parsed_rows += len(rows)
                    equity_rows += sum(1 for r in rows if r.is_equity)
                    who = f"{filing.get('First','')} {filing.get('Last','')}".strip()
                    if not rows:
                        self.record(WARN, f"  PTR {filing.get('DocID')} ({who})", "0 笔交易（多半是扫描件）")
                        continue
                    sample = rows[0]
                    self.record(
                        PASS,
                        f"  PTR {filing.get('DocID')} ({who})",
                        f"{len(rows)} 笔；示例: {sample.ticker or '(无代码)'} [{sample.asset_type}] "
                        f"{sample.action} {sample.traded} ${sample.amount_low:,.0f}-${sample.amount_high:,.0f} "
                        f"| {sample.asset[:50]}",
                    )
                self.record(
                    PASS if parsed_rows else FAIL,
                    "众议院 PDF 解析",
                    f"5 份样本共解析出 {parsed_rows} 笔交易，其中股票/期权 {equity_rows} 笔",
                )
        else:
            self.record(FAIL, "众议院年度索引", "取不到或解析不出 Member 节点")

        # -- Senate: agreement + CSRF -> search -> electronic report table
        def senate():
            session = C.EfdSession(user_agent=self.http.user_agent)
            session.open()
            filings = session.search_ptrs(self.today - timedelta(days=45), limit=25)
            return session, filings

        result = self.guard("参议院 eFD 搜索", senate)
        if not result:
            return
        session, filings = result
        if not filings:
            self.record(WARN, "参议院 eFD 搜索", "45 天内没有 PTR（可能属实，也可能是接口变了）")
            return
        electronic = [f for f in filings if f.is_electronic]
        self.record(
            PASS, "参议院 eFD 搜索",
            f"{len(filings)} 份申报，其中电子版 {len(electronic)} 份、扫描件 {len(filings) - len(electronic)} 份",
        )
        self.record(INFO, "  最新一份", f"{filings[0].person} · {filings[0].filed} · {filings[0].url}")

        parsed = 0
        for filing in electronic[:3]:
            rows = self.guard(f"  参议院报告 {filing.person}", lambda f=filing: session.transactions(f))
            if not rows:
                self.record(WARN, f"  参议院报告 {filing.person}", "电子版但解析出 0 笔，表格结构可能变了")
                continue
            parsed += len(rows)
            row = rows[0]
            self.record(
                PASS, f"  参议院报告 {filing.person}",
                f"{len(rows)} 笔；示例: {row.ticker or '(无代码)'} [{row.asset_type}] {row.action} "
                f"{row.traded} ${row.amount_low:,.0f}-${row.amount_high:,.0f} | {row.asset[:50]}",
            )
        self.record(
            PASS if parsed or not electronic else FAIL,
            "参议院表格解析", f"{len(electronic[:3])} 份电子报告共解析出 {parsed} 笔交易",
        )

    def check_news(self) -> None:
        self.section("G. 新闻 RSS/Atom 源")
        from .feeds import parse_feed

        feeds = [
            (str(f.get("name") or f["url"]), str(f["url"]))
            for f in self.config.get("sources.news.feeds", [])
            if isinstance(f, dict) and f.get("url")
        ]
        per_ticker = self.config.get("sources.news.per_ticker_feed")
        if per_ticker and self.config.tickers:
            feeds.append(("个股新闻 AAPL", str(per_ticker).format(ticker="AAPL")))

        alive = 0
        for name, url in feeds:
            body = self.guard(f"feed {name}", lambda u=url: self.http.get(u, allow_404=True))
            if not body:
                self.record(FAIL, f"feed {name}", f"无响应或 404: {url}")
                continue
            entries = parse_feed(body)
            if not entries:
                # HTTP worked but nothing parsed: that is a parser/format problem, not a network one.
                self.record(FAIL, f"feed {name}", f"HTTP OK ({len(body):,} 字节) 但解析出 0 条；前 300 字节: {body[:300]!r}")
                continue
            alive += 1
            dated = [e for e in entries if e.published]
            newest = max((e.published for e in dated), default=None)
            age = f"{(datetime.now(timezone.utc) - newest).total_seconds()/3600:.1f}h" if newest else "无时间戳"
            level = PASS if dated else WARN
            self.record(
                level,
                f"feed {name}",
                f"{len(entries)} 条，{len(dated)} 条有时间戳，最新 {age}；示例: {entries[0].title[:70]!r}",
            )
        level = PASS if alive >= max(1, len(feeds) // 2) else FAIL
        self.record(level, "新闻源总体", f"{alive}/{len(feeds)} 个源可用")

    # -- entry point -----------------------------------------------------
    def run(self) -> int:
        print(f"Stock Radar 数据源体检 · {self.today} · UA={self.http.user_agent!r}", flush=True)
        warning = self.config.user_agent_warning()
        if warning:
            self.record(FAIL, "SEC User-Agent", warning)
        else:
            self.record(PASS, "SEC User-Agent", self.http.user_agent)
        rows_by_day = self.guard("A. 日索引", self.check_daily_index) or []
        for name, check in (
            ("B. ticker 映射", self.check_ticker_map),
            ("C. Form 4", lambda: self.check_form4(rows_by_day)),
            ("D. 13F", self.check_13f),
            ("E. 13D/13G", lambda: self.check_stakes(rows_by_day)),
            ("F. 国会", self.check_congress),
            ("G. 新闻", self.check_news),
        ):
            self.guard(name, check)

        self.section("总结")
        counts = {level: sum(1 for l, _, _ in self.results if l == level) for level in (PASS, WARN, FAIL)}
        print(f"PASS={counts[PASS]}  WARN={counts[WARN]}  FAIL={counts[FAIL]}", flush=True)
        failures = [(c, d) for l, c, d in self.results if l == FAIL]
        if failures:
            print("\n失败项:", flush=True)
            for check, detail in failures:
                print(f"  ❌ {check}: {detail[:300]}", flush=True)
        return 1 if failures else 0


def main(config: Config, today: date | None = None, verbose: bool = False) -> int:
    return Probe(config, today=today, verbose=verbose).run()
