"""Collector-level behaviour against the fake HTTP layer."""

from datetime import date

import pytest

from stock_radar.collectors.congress import CongressCollector
from stock_radar.collectors.funds import FundCollector
from stock_radar.collectors.insiders import InsiderCollector
from stock_radar.collectors.news import NewsCollector
from stock_radar.state import State


class TestInsiders:
    def test_watchlist_scope_only_fetches_watchlist_issuers(self, ctx):
        items = InsiderCollector().collect(ctx)
        # The unrelated CIK 9999999 Form 4 must never be fetched.
        assert not any("9999999" in url for url in ctx.http.requested)
        assert items and all(i.kind == "insider" for i in items)

    def test_min_value_and_code_filters(self, ctx):
        ctx.config.data["sources"]["insiders"]["codes"] = ["S"]
        items = InsiderCollector().collect(ctx)
        assert len(items) == 1
        assert items[0].detail["code"] == "S"
        assert items[0].detail["ticker"] == "AAPL"
        assert "COOK TIMOTHY D" in items[0].title

        ctx.config.data["sources"]["insiders"]["min_value_usd"] = 200_000_000
        assert InsiderCollector().collect(ctx) == []

    def test_empty_watchlist_short_circuits(self, ctx):
        ctx.config.data["watchlist"]["tickers"] = []
        assert InsiderCollector().collect(ctx) == []

    def test_keys_are_stable_across_runs(self, ctx):
        first = {i.key for i in InsiderCollector().collect(ctx)}
        second = {i.key for i in InsiderCollector().collect(ctx)}
        assert first == second and first


class FakeEfd:
    """Stands in for the eFD session: the click-through + CSRF flow needs a browser cookie jar."""

    filings: list = []
    rows: dict = {}
    opened = 0

    def __init__(self, user_agent, timeout=30.0):
        self.user_agent = user_agent

    def open(self):
        type(self).opened += 1

    def search_ptrs(self, since, limit=100):
        return [f for f in self.filings if f.filed is None or f.filed >= since]

    def transactions(self, filing):
        return self.rows.get(filing.url, [])


def senate_fixture():
    from stock_radar.senate_efd import SenateFiling, SenateTransaction

    electronic = SenateFiling(
        first_name="SAM ", last_name="SENATOR", filer_type="Senator",
        title="Periodic Transaction Report for 09/01/2026",
        url="https://efdsearch.senate.gov/search/view/ptr/abc/", filed=date(2026, 9, 1),
    )
    paper = SenateFiling(
        first_name="PAT ", last_name="PAPER", filer_type="Senator",
        title="Periodic Transaction Report for 08/31/2026",
        url="https://efdsearch.senate.gov/search/view/paper/def/", filed=date(2026, 8, 31),
    )
    FakeEfd.filings = [electronic, paper]
    FakeEfd.rows = {
        electronic.url: [
            SenateTransaction(
                filing=electronic, traded=date(2026, 8, 28), owner="Spouse", ticker="AAPL",
                asset="Apple Inc. Common Stock", asset_type="Stock", action="Sale (Full)",
                amount_low=100001.0, amount_high=250000.0,
            ),
            SenateTransaction(
                filing=electronic, traded=date(2026, 8, 29), owner="Self", ticker="",
                asset="US Treasury Bill", asset_type="Corporate Bond", action="Purchase",
                amount_low=1001.0, amount_high=15000.0,
            ),
        ]
    }
    return electronic, paper


class TestCongressHouse:
    """The House provider goes ZIP index -> PTR PDF -> transaction rows, all official."""

    def collect(self, ctx, state=None):
        ctx.config.data["sources"]["congress"]["providers"] = ["house_clerk"]
        return CongressCollector(state=state).collect(ctx)

    def test_parses_transactions_out_of_the_pdf(self, ctx):
        items = self.collect(ctx)
        tickers = {i.detail["ticker"] for i in items if i.detail["ticker"]}
        assert tickers == {"NVDA", "AAPL", "MSFT"}
        nvda = next(i for i in items if i.detail["ticker"] == "NVDA")
        assert nvda.detail["person"] == "Jane Doe"
        assert nvda.detail["chamber"] == "House"
        assert nvda.detail["action"] == "purchase"
        assert nvda.detail["amount_high"] == 500000
        assert nvda.detail["transaction_date"] == date(2026, 8, 14)
        assert "配偶" in nvda.title  # owner code SP
        assert "延迟披露 19 天" in nvda.summary
        assert nvda.url.endswith("/ptr-pdfs/2026/20260001.pdf")

    def test_non_equity_rows_are_dropped_by_default(self, ctx):
        assets = {i.detail["asset"] for i in self.collect(ctx)}
        assert not any("Treasury" in a for a in assets)

    def test_stocks_only_can_be_turned_off(self, ctx):
        ctx.config.data["sources"]["congress"]["stocks_only"] = False
        ctx.config.data["sources"]["congress"]["lookback_days"] = 120  # that row is older
        assets = {i.detail["asset"] for i in self.collect(ctx)}
        assert any("Treasury" in a for a in assets)

    def test_scanned_pdf_degrades_to_a_filing_pointer(self, ctx):
        items = self.collect(ctx)
        scanned = [i for i in items if i.detail["person"] == "Paul Scanned"]
        assert len(scanned) == 1
        assert scanned[0].detail["action"] == "filing"
        assert "扫描件" in scanned[0].detail["asset"]
        # A bare pointer must rank below a parsed transaction.
        assert scanned[0].score < min(i.score for i in items if i.detail["ticker"])

    def test_only_periodic_reports_within_lookback(self, ctx):
        people = {i.detail["person"] for i in self.collect(ctx)}
        assert "Ann Annual" not in people   # FilingType O, an annual report
        assert "Abe Ancient" not in people  # filed in January, outside 45 days

    def test_pdfs_are_parsed_once_and_cached(self, ctx, tmp_path):
        from stock_radar.state import State

        with State(tmp_path / "c.db") as state:
            first = self.collect(ctx, state)
            fetched = sum(1 for u in ctx.http.requested if "ptr-pdfs" in u)
            second = self.collect(ctx, state)
            assert sum(1 for u in ctx.http.requested if "ptr-pdfs" in u) == fetched
        assert [i.key for i in first] == [i.key for i in second]


class TestCongressSenate:
    def collect(self, ctx, monkeypatch, state=None):
        import stock_radar.collectors.congress as C

        senate_fixture()
        monkeypatch.setattr(C, "EfdSession", FakeEfd)
        ctx.config.data["sources"]["congress"]["providers"] = ["senate_efd"]
        return CongressCollector(state=state).collect(ctx)

    def test_electronic_report_transactions(self, ctx, monkeypatch):
        items = self.collect(ctx, monkeypatch)
        aapl = next(i for i in items if i.detail["ticker"] == "AAPL")
        assert aapl.detail["person"] == "Sam Senator"
        assert aapl.detail["chamber"] == "Senate"
        assert aapl.detail["action"] == "sale_full"
        assert aapl.detail["amount_high"] == 250000
        assert "配偶" in aapl.title

    def test_paper_filing_degrades_to_a_pointer(self, ctx, monkeypatch):
        items = self.collect(ctx, monkeypatch)
        paper = next(i for i in items if i.detail["person"] == "Pat Paper")
        assert paper.detail["action"] == "filing"
        assert paper.url.endswith("/paper/def/")

    def test_bonds_dropped_but_stocks_kept(self, ctx, monkeypatch):
        items = self.collect(ctx, monkeypatch)
        assert not any("Treasury" in i.detail["asset"] for i in items)


class TestCongressMerging:
    """Both chambers must show up, and one being down must not hide the other."""

    def test_both_providers_merge(self, ctx, monkeypatch):
        import stock_radar.collectors.congress as C

        senate_fixture()
        monkeypatch.setattr(C, "EfdSession", FakeEfd)
        ctx.config.data["sources"]["congress"]["providers"] = ["house_clerk", "senate_efd"]
        items = CongressCollector().collect(ctx)
        assert {i.detail["chamber"] for i in items} == {"House", "Senate"}

    def test_one_provider_down_keeps_the_other(self, ctx, monkeypatch):
        import stock_radar.collectors.congress as C

        def boom(*args, **kwargs):
            raise RuntimeError("eFD 挂了")

        monkeypatch.setitem(C.PROVIDERS, "senate_efd", boom)
        ctx.config.data["sources"]["congress"]["providers"] = ["house_clerk", "senate_efd"]
        items = CongressCollector().collect(ctx)
        assert {i.detail["chamber"] for i in items} == {"House"}
        assert any("eFD 挂了" in note for note in ctx.notes)

    def test_all_providers_down_raises(self, ctx):
        ctx.config.data["sources"]["congress"]["providers"] = ["nope"]
        with pytest.raises(RuntimeError, match="未知的 provider"):
            CongressCollector().collect(ctx)

    def test_watchlist_only_filter(self, ctx):
        ctx.config.data["sources"]["congress"]["providers"] = ["house_clerk"]
        ctx.config.data["sources"]["congress"]["watchlist_only"] = True
        tickers = {i.detail["ticker"] for i in CongressCollector().collect(ctx)}
        assert tickers == {"NVDA", "AAPL"}  # MSFT is not on this test watchlist

    def test_watchlist_ticker_outranks_the_rest(self, ctx):
        ctx.config.data["sources"]["congress"]["providers"] = ["house_clerk"]
        items = CongressCollector().collect(ctx)
        nvda = next(i for i in items if i.detail["ticker"] == "NVDA")
        msft = next(i for i in items if i.detail["ticker"] == "MSFT")
        assert nvda.score > msft.score


class TestFunds:
    def test_13f_diff_becomes_items(self, ctx, tmp_path):
        with State(tmp_path / "s.db") as state:
            items = FundCollector(state=state)._thirteen_f(ctx)
        kinds = {i.detail.get("change") for i in items}
        assert kinds == {"increase", "new", "exit"}
        assert all(i.detail["fund"] == "Berkshire Hathaway" for i in items)
        assert any("新建仓" in i.title and "CHUBB" in i.title for i in items)

    def test_same_filing_is_not_rediffed(self, ctx, tmp_path):
        with State(tmp_path / "s.db") as state:
            collector = FundCollector(state=state)
            assert collector._thirteen_f(ctx)
            assert collector._thirteen_f(ctx) == []  # snapshot says we already did this accession

    def test_stale_filing_outside_lookback_is_skipped(self, ctx, tmp_path):
        ctx.config.data["sources"]["funds"]["lookback_days"] = 1
        with State(tmp_path / "s.db") as state:
            assert FundCollector(state=state)._thirteen_f(ctx) == []

    def test_13g_stake_filing_is_picked_up(self, ctx):
        items = FundCollector()._stakes(ctx)
        assert len(items) == 1
        assert items[0].tickers == ["AAPL"]
        assert "SC 13G/A" in items[0].title
        assert "BERKSHIRE" in items[0].summary


class TestNews:
    def test_feed_items_and_watchlist_scoring(self, ctx):
        items = NewsCollector().collect(ctx)
        titles = [i.title for i in items]
        assert any("Nvidia beats earnings" in t for t in titles)
        assert any("Fed holds rates steady" in t for t in titles)
        assert not any("Ancient story" in t for t in titles)  # older than lookback_hours
        nvda = next(i for i in items if "Nvidia" in i.title)
        fed = next(i for i in items if "Fed holds" in i.title)
        assert nvda.tickers == ["NVDA"] and nvda.score > fed.score

    def test_tracking_params_are_stripped_for_dedup(self, ctx):
        item = next(i for i in NewsCollector().collect(ctx) if "Nvidia" in i.title)
        assert "utm_source" in item.url  # the link the reader clicks stays intact
        again = next(i for i in NewsCollector().collect(ctx) if "Nvidia" in i.title)
        assert item.key == again.key

    def test_8k_for_watchlist_company(self, ctx):
        items = NewsCollector()._eight_k(ctx)
        assert len(items) == 1
        assert items[0].tickers == ["NVDA"]
        assert "8-K" in items[0].title

    def test_watchlist_only_filter(self, ctx):
        ctx.config.data["sources"]["news"]["watchlist_only"] = True
        ctx.config.data["sources"]["news"]["include_8k"] = False
        titles = [i.title for i in NewsCollector().collect(ctx)]
        assert titles and all("Nvidia" in t for t in titles)

    def test_dead_feed_does_not_kill_the_section(self, ctx):
        ctx.config.data["sources"]["news"]["feeds"].append(
            {"name": "Dead", "url": "https://dead.example.com/rss"}
        )
        assert NewsCollector().collect(ctx)  # unrouted URL -> allow_404 -> empty, others still parse


class TestProbeWiring:
    """The probe reaches into collector internals, so a rename there must break a test."""

    def build(self, ctx):
        from stock_radar.probe import Probe

        probe = Probe(ctx.config, today=ctx.today)
        probe.http = ctx.http
        probe.edgar = ctx.edgar
        return probe

    def test_congress_check_runs_against_the_official_pipeline(self, ctx):
        probe = self.build(ctx)
        probe.check_congress()
        checks = {check for _, check, _ in probe.results}
        assert "众议院年度索引" in checks
        assert "众议院 PDF 解析" in checks
        # The House half must actually parse rows out of the fixture PDF.
        parsed = next(d for lvl, c, d in probe.results if c == "众议院 PDF 解析")
        assert "0 笔交易" not in parsed
        assert not any(lvl == "FAIL" and "众议院" in c for lvl, c, _ in probe.results)

    def test_a_broken_section_does_not_end_the_probe(self, ctx, monkeypatch):
        probe = self.build(ctx)
        monkeypatch.setattr(probe, "check_13f", lambda: 1 / 0)
        probe.run()
        checks = {check for _, check, _ in probe.results}
        assert "D. 13F" in checks              # recorded as a failure
        assert any("feed" in c for c in checks)  # and later sections still ran

    def test_duplicate_rows_keep_separate_keys(self, ctx):
        items = CongressCollector().collect(ctx)
        assert len({i.key for i in items}) == len(items)


class TestCoverageGapsAreReported:
    """A scanned filing is missing data, not a quiet success — the run must say so."""

    def test_house_scanned_count_reaches_the_status(self, ctx):
        ctx.config.data["sources"]["congress"]["providers"] = ["house_clerk"]
        CongressCollector().collect(ctx)
        note = next(n for n in ctx.notes if "扫描件" in n)
        assert "1/2" in note and "众议院" in note

    def test_no_note_when_everything_parsed(self, ctx):
        ctx.config.data["sources"]["congress"]["providers"] = ["house_clerk"]
        ctx.http.routes["ptr-pdfs/2026/20260002.pdf"] = ctx.http.routes["ptr-pdfs/2026/20260001.pdf"]
        CongressCollector().collect(ctx)
        assert not any("扫描件" in n for n in ctx.notes)
