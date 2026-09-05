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
from datetime import date, datetime, timezone

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
        self.section("F. 国会议员交易数据源")
        from .collectors import congress as C

        for label, url, person_key in (
            ("House Stock Watcher", C.HOUSE_URL, "representative"),
            ("Senate Stock Watcher", C.SENATE_URL, "senator"),
        ):
            rows = self.guard(label, lambda u=url: self.http.get_json(u, allow_404=True))
            if rows is None:
                continue
            if not isinstance(rows, list) or not rows:
                self.record(FAIL, label, f"返回的不是非空列表: {type(rows).__name__}")
                continue
            self.record(PASS, label, f"{len(rows)} 条记录")
            sample = rows[-1] if isinstance(rows[-1], dict) else rows[0]
            self.record(INFO, f"  {label} 字段", str(sorted(sample.keys())))
            self.record(INFO, f"  {label} 样本", str(sample)[:400])
            # Field-name drift is the failure mode that silently empties this section.
            for field in (person_key, "ticker", "amount", "type", "transaction_date", "disclosure_date"):
                present = sum(1 for r in rows[-200:] if isinstance(r, dict) and field in r)
                level = PASS if present > 150 else FAIL
                self.record(level, f"  {label} 字段 '{field}'", f"最近 200 条里出现 {present} 次")
            dates = [C.parse_date(r.get("disclosure_date")) for r in rows[-500:] if isinstance(r, dict)]
            dates = [d for d in dates if d]
            if dates:
                newest = max(dates)
                lag = (self.today - newest).days
                level = PASS if lag <= 21 else WARN
                self.record(level, f"  {label} 新鲜度", f"最新披露日 {newest}（距今 {lag} 天）")
            else:
                self.record(FAIL, f"  {label} 新鲜度", "解析不出任何 disclosure_date")

        raw = self.guard("House Clerk ZIP", lambda: self.http.get(C.HOUSE_CLERK_ZIP.format(year=self.today.year), allow_404=True))
        if raw:
            from .edgar import unzip_first

            xml_bytes = unzip_first(raw, ".xml")
            if xml_bytes:
                root = ET.fromstring(xml_bytes)
                members = list(root.iter("Member"))
                ptrs = [m for m in members if (m.findtext("FilingType") or "").strip() == "P"]
                self.record(PASS, "House Clerk 官方索引", f"ZIP {len(raw):,} 字节；{len(members)} 条申报，其中 PTR {len(ptrs)} 条")
                if ptrs:
                    m = ptrs[-1]
                    fields = {c.tag: (c.text or "").strip() for c in m}
                    self.record(INFO, "  最新 PTR", str(fields))
            else:
                self.record(FAIL, "House Clerk 官方索引", "ZIP 里没有 XML")
        else:
            self.record(WARN, "House Clerk 官方索引", "取不到 ZIP")

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
        rows_by_day = self.check_daily_index() or []
        self.check_ticker_map()
        self.check_form4(rows_by_day)
        self.check_13f()
        self.check_stakes(rows_by_day)
        self.check_congress()
        self.check_news()

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
