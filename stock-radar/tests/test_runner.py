"""End-to-end: runner wiring, cross-run dedup, rendering and file output."""

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from conftest import TODAY
from stock_radar.models import Item, Report, SourceStatus
from stock_radar.render import render_html, render_markdown, render_text
from stock_radar.runner import run


@pytest.fixture
def patched_run(ctx, monkeypatch):
    """run() with its real collectors but the fake HTTP layer underneath."""
    import stock_radar.runner as runner

    monkeypatch.setattr(runner, "Http", lambda **kwargs: ctx.http)
    return lambda **kwargs: run(ctx.config, today=TODAY, notify=False, **kwargs)


class TestRun:
    def test_first_run_collects_every_section(self, patched_run, ctx):
        report, written = patched_run()
        kinds = {i.kind for i in report.items}
        assert kinds == {"congress", "insider", "fund", "news"}
        assert all(s.ok for s in report.statuses), [s.message for s in report.statuses if not s.ok]
        assert {p.name for p in written} >= {"latest.md", "latest.html", f"{TODAY}.md"}

    def test_second_run_reports_nothing_new(self, patched_run):
        first, _ = patched_run()
        assert first.items
        second, _ = patched_run()
        assert second.items == []
        assert all(s.ok for s in second.statuses)
        assert any("已在往期报告出现" in s.message for s in second.statuses)

    def test_no_dedup_flag_repeats_everything(self, patched_run):
        first, _ = patched_run()
        second, _ = patched_run(dedup=False)
        assert len(second.items) == len(first.items)

    def test_only_filter_runs_one_collector(self, patched_run):
        report, _ = patched_run(only=["news"])
        assert {i.kind for i in report.items} == {"news"}
        assert [s.name for s in report.statuses] == ["news"]

    def test_collector_failure_is_isolated_and_reported(self, patched_run, ctx):
        ctx.config.data["sources"]["congress"]["providers"] = ["nope"]
        report, _ = patched_run()
        congress = next(s for s in report.statuses if s.name == "congress")
        assert not congress.ok and "未知的 provider" in congress.message
        # The other sections still produced their items.
        assert {i.kind for i in report.items} >= {"insider", "news"}

    def test_disabled_source_is_marked_not_failed(self, patched_run, ctx):
        ctx.config.data["sources"]["funds"]["enabled"] = False
        report, _ = patched_run()
        funds = next(s for s in report.statuses if s.name == "funds")
        assert funds.ok and funds.message == "disabled"
        assert not any(i.kind == "fund" for i in report.items)

    def test_json_output_round_trips(self, patched_run, ctx):
        ctx.config.data["output"]["formats"] = ["json"]
        report, written = patched_run()
        payload = json.loads(Path(ctx.config.get("output.dir"), "latest.json").read_text("utf-8"))
        assert len(payload["items"]) == len(report.items)
        assert {"kind", "title", "url", "when", "key"} <= set(payload["items"][0])

    def test_dry_run_writes_nothing(self, patched_run, ctx):
        _, written = patched_run(write_files=False)
        assert written == []
        assert not Path(ctx.config.get("output.dir")).exists()


class TestRender:
    def sample(self) -> Report:
        return Report(
            generated_at=datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc),
            items=[
                Item(kind="news", title="Fed holds rates", url="https://e.com/a", when=date(2026, 9, 3),
                     summary="No change", tickers=["SPY"], score=10, source="CNBC"),
                Item(kind="insider", title="AAPL CEO sold", url="", when=date(2026, 9, 2), score=90),
            ],
            statuses=[SourceStatus("news", True, 1), SourceStatus("congress", False, 0, "mirror down")],
        )

    def test_markdown_has_all_sections_and_status(self):
        md = render_markdown(self.sample())
        for heading in ("国会议员交易", "公司高管买卖", "基金持仓变化", "重要新闻"):
            assert f"## " in md and heading in md
        assert "[Fed holds rates](https://e.com/a)" in md
        assert "AAPL CEO sold" in md and "](" not in md.split("AAPL CEO sold")[0][-40:]
        assert "❌ `congress`" in md and "mirror down" in md
        assert "_今日无新增。_" in md  # empty congress/fund sections

    def test_english_labels(self):
        md = render_markdown(self.sample(), language="en")
        assert "Daily Stock Radar" in md and "Congressional Trades" in md
        assert "Nothing new today." in md

    def test_html_is_selfcontained_and_escaped(self):
        report = self.sample()
        report.items[0].title = 'Fed <script>alert("x")</script>'
        html = render_html(report)
        assert html.startswith("<!doctype html>") and html.rstrip().endswith("</html>")
        assert "<script>" not in html and "&lt;script&gt;" in html
        assert "https://e.com/a" in html
        assert "❌" in html and "mirror down" in html

    def test_text_is_compact_and_truncates(self):
        report = self.sample()
        report.items += [Item(kind="news", title=f"story {n}", url="u", score=1) for n in range(10)]
        text = render_text(report, per_section=3)
        assert "每日股票雷达" in text
        assert "… +" in text
        assert "国会议员交易" not in text  # empty sections are omitted entirely

    def test_empty_report_says_so(self):
        empty = Report(generated_at=datetime.now(timezone.utc))
        assert "所有板块今日均无新增内容" in render_markdown(empty)
        assert "所有板块今日均无新增内容" in render_html(empty)


class TestUserAgentGuard:
    """SEC throttles contactless clients, so a bad UA must be loud, not silent."""

    def test_missing_user_agent_is_reported(self, patched_run, ctx):
        ctx.config.data["sec"]["user_agent"] = ""
        report, _ = patched_run(only=["news"])
        ua = next(s for s in report.statuses if s.name == "sec.user_agent")
        assert not ua.ok and "未设置" in ua.message

    def test_placeholder_user_agent_is_reported(self, patched_run, ctx, monkeypatch):
        monkeypatch.delenv("SEC_USER_AGENT", raising=False)
        ctx.config.data["sec"]["user_agent"] = "Your Name your-email@example.com"
        report, _ = patched_run(only=["news"])
        ua = next(s for s in report.statuses if s.name == "sec.user_agent")
        assert not ua.ok and "占位值" in ua.message

    def test_env_var_overrides_the_placeholder(self, ctx, monkeypatch):
        monkeypatch.setenv("SEC_USER_AGENT", "Real Person me@example.invalid")
        ctx.config.data["sec"]["user_agent"] = "Your Name your-email@example.com"
        assert ctx.config.user_agent == "Real Person me@example.invalid"
        assert ctx.config.user_agent_warning() == ""

    def test_good_user_agent_adds_no_status(self, patched_run, ctx):
        report, _ = patched_run(only=["news"])
        assert not any(s.name == "sec.user_agent" for s in report.statuses)


class TestPartialFailureReporting:
    """A section whose sub-sources all failed must not render as a quiet day."""

    def test_all_feeds_dead_marks_news_failed(self, patched_run, ctx, monkeypatch):
        from stock_radar.http import HttpError

        real_get = ctx.http.get

        def blow_up(url, *, allow_404=False, headers=None):
            if "cnbc" in url or "pressreleases" in url:
                raise HttpError(url, 403)
            return real_get(url, allow_404=allow_404, headers=headers)

        monkeypatch.setattr(ctx.http, "get", blow_up)
        ctx.config.data["sources"]["news"]["include_8k"] = False
        report, _ = patched_run(only=["news"])
        news = next(s for s in report.statuses if s.name == "news")
        assert not news.ok
        assert "子来源失败" in news.message and "403" in news.message

    def test_partial_failure_still_ok_when_items_arrived(self, patched_run, ctx, monkeypatch):
        from stock_radar.http import HttpError

        real_get = ctx.http.get

        def one_dead(url, *, allow_404=False, headers=None):
            if "pressreleases" in url:
                raise HttpError(url, 500)
            return real_get(url, allow_404=allow_404, headers=headers)

        monkeypatch.setattr(ctx.http, "get", one_dead)
        report, _ = patched_run(only=["news"])
        news = next(s for s in report.statuses if s.name == "news")
        assert news.ok and news.items > 0
        assert "1 个子来源失败" in news.message

    def test_edgar_index_failure_reaches_the_status(self, patched_run, ctx, monkeypatch):
        real_get = ctx.http.get

        def index_dead(url, *, allow_404=False, headers=None):
            if "daily-index" in url:
                raise ConnectionError("proxy said no")
            return real_get(url, allow_404=allow_404, headers=headers)

        monkeypatch.setattr(ctx.http, "get", index_dead)
        report, _ = patched_run(only=["insiders"])
        insiders = next(s for s in report.statuses if s.name == "insiders")
        assert not insiders.ok
        assert "日索引" in insiders.message and "proxy said no" in insiders.message

    def test_dedup_note_and_failure_note_coexist(self, patched_run, ctx, monkeypatch):
        patched_run(only=["news"])  # first run marks everything as seen
        from stock_radar.http import HttpError

        real_get = ctx.http.get

        def one_dead(url, *, allow_404=False, headers=None):
            if "pressreleases" in url:
                raise HttpError(url, 500)
            return real_get(url, allow_404=allow_404, headers=headers)

        monkeypatch.setattr(ctx.http, "get", one_dead)
        report, _ = patched_run(only=["news"])
        news = next(s for s in report.statuses if s.name == "news")
        assert "已在往期报告出现" in news.message and "子来源失败" in news.message
