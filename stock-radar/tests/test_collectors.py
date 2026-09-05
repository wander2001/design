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


class TestCongress:
    def test_collects_house_and_senate_within_lookback(self, ctx):
        items = CongressCollector().collect(ctx)
        people = {i.detail["person"] for i in items}
        assert "Jane Doe" in people  # "Hon." prefix stripped
        assert "Sam Senator" in people
        assert "Old Timer" not in people  # 2024 disclosure is outside the 45-day window

    def test_amount_filter_and_scoring(self, ctx):
        items = CongressCollector().collect(ctx)
        nvda = next(i for i in items if i.detail["ticker"] == "NVDA")
        assert nvda.detail["amount_high"] == 500000
        assert "延迟披露" in nvda.summary
        # NVDA is on the watchlist, so it must outrank the untickered treasury sale.
        other = next(i for i in items if i.detail["person"] == "John Roe")
        assert nvda.score > other.score

    def test_watchlist_only_drops_untracked(self, ctx):
        ctx.config.data["sources"]["congress"]["watchlist_only"] = True
        tickers = {i.detail["ticker"] for i in CongressCollector().collect(ctx)}
        assert tickers == {"NVDA", "AAPL"}

    def test_provider_failure_raises_so_status_shows_it(self, ctx):
        ctx.config.data["sources"]["congress"]["providers"] = ["nope"]
        with pytest.raises(RuntimeError, match="unknown provider"):
            CongressCollector().collect(ctx)

    def test_falls_through_to_next_provider(self, ctx):
        ctx.http.routes.pop("house-stock-watcher")
        ctx.http.routes.pop("senate-stock-watcher")
        ctx.config.data["sources"]["congress"]["providers"] = ["stockwatcher", "house_clerk"]
        # Both mirrors 404 and the clerk ZIP is unrouted: no data, no crash.
        assert CongressCollector().collect(ctx) == []


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
