"""Unit tests for the format parsers — the parts most likely to silently drift."""

from datetime import date, datetime, timezone

from conftest import fixture

from stock_radar.collectors.congress import normalize_type, parse_amount, parse_date
from stock_radar.collectors.funds import diff_holdings, parse_info_table
from stock_radar.collectors.insiders import parse_form4
from stock_radar.edgar import extract_ownership_block, parse_master_idx
from stock_radar.feeds import parse_datetime, parse_feed, strip_html


class TestForm4:
    def setup_method(self):
        self.trades = parse_form4(fixture("form4_sample.xml"), "acc-1", "http://x")

    def test_all_transactions_found(self):
        assert len(self.trades) == 3
        assert {t.code for t in self.trades} == {"S", "F", "P"}

    def test_issuer_and_owner(self):
        t = self.trades[0]
        assert t.ticker == "AAPL"
        assert t.issuer == "Apple Inc."
        assert t.owner == "COOK TIMOTHY D"
        assert "Chief Executive Officer" in t.title
        assert "Director" in t.title

    def test_amounts_and_dates(self):
        t = self.trades[0]
        assert t.shares == 511000
        assert t.price == 226.42
        assert round(t.value) == 115700620
        assert t.when == date(2026, 9, 3)
        assert t.shares_after == 3280000
        assert t.acquired is False

    def test_derivative_table_is_flagged(self):
        deriv = [t for t in self.trades if t.derivative]
        assert len(deriv) == 1 and deriv[0].code == "P"

    def test_bad_xml_is_survivable(self):
        assert parse_form4(b"<not-xml", "acc", "u") == []

    def test_extract_from_full_submission(self):
        wrapper = b"<SEC-DOCUMENT>junk<XML>" + fixture("form4_sample.xml") + b"</XML>tail"
        block = extract_ownership_block(wrapper)
        assert block is not None
        assert parse_form4(block, "acc", "u")[0].ticker == "AAPL"
        assert extract_ownership_block(b"nothing here") is None


class TestThirteenF:
    def test_values_reported_in_thousands_are_normalized(self):
        holdings = parse_info_table(fixture("13f_q1.xml"))
        apple = next(h for h in holdings if h.issuer == "APPLE INC")
        # 69,016,000 (thousands) -> $69.016B, i.e. ~$230/share on 300M shares
        assert 69_000_000_000 < apple.value_usd < 69_100_000_000
        assert 200 < apple.value_usd / apple.shares < 250

    def test_values_already_in_dollars_are_left_alone(self):
        raw = fixture("13f_q1.xml").replace(b"<value>69016000</value>", b"<value>69016000000</value>")
        raw = raw.replace(b"<value>41100000</value>", b"<value>41100000000</value>")
        raw = raw.replace(b"<value>241000</value>", b"<value>241000000</value>")
        apple = next(h for h in parse_info_table(raw) if h.issuer == "APPLE INC")
        assert apple.value_usd == 69_016_000_000

    def test_diff_classifies_every_kind(self):
        changes = diff_holdings(parse_info_table(fixture("13f_q1.xml")), parse_info_table(fixture("13f_q2.xml")))
        by_kind = {c.kind: c for c in changes}
        assert set(by_kind) == {"increase", "new", "exit"}
        assert by_kind["increase"].issuer == "APPLE INC"
        assert round(by_kind["increase"].pct, 1) == 33.3
        assert by_kind["new"].issuer == "CHUBB LIMITED"
        assert by_kind["exit"].issuer == "PARAMOUNT GLOBAL"
        # Bank of America is unchanged and must not be reported.
        assert not any("BANK OF AMERICA" in c.issuer for c in changes)

    def test_small_moves_below_threshold_are_ignored(self):
        q1 = parse_info_table(fixture("13f_q1.xml"))
        q2 = [type(h)(**{**h.__dict__}) for h in q1]
        q2[0].shares *= 1.02  # +2%, under the 10% floor
        assert diff_holdings(q1, q2, min_pct=10.0) == []

    def test_sorted_by_magnitude(self):
        changes = diff_holdings(parse_info_table(fixture("13f_q1.xml")), parse_info_table(fixture("13f_q2.xml")))
        assert [c.magnitude for c in changes] == sorted((c.magnitude for c in changes), reverse=True)


class TestCongressHelpers:
    def test_amount_ranges(self):
        assert parse_amount("$1,001 - $15,000") == (1001.0, 15000.0)
        assert parse_amount("$50,000,001 +") == (50000001.0, 50000001.0)
        assert parse_amount(None) == (0.0, 0.0)
        assert parse_amount("--") == (0.0, 0.0)

    def test_dates_in_both_published_formats(self):
        assert parse_date("10/04/2021") == date(2021, 10, 4)
        assert parse_date("2021-09-27") == date(2021, 9, 27)
        assert parse_date("--") is None
        assert parse_date(None) is None

    def test_transaction_types(self):
        assert normalize_type("Sale (Full)") == "sale_full"
        assert normalize_type("Sale (Partial)") == "sale_partial"
        assert normalize_type("sale_partial") == "sale_partial"
        assert normalize_type("Sale") == "sale"
        assert normalize_type("Purchase") == "purchase"
        assert normalize_type("P") == "purchase"
        assert normalize_type("E") == "exchange"


class TestFeeds:
    def test_rss(self):
        entries = parse_feed(fixture("rss_sample.xml"))
        assert len(entries) == 3
        assert entries[0].title.startswith("Nvidia beats earnings")
        assert entries[0].published == datetime(2026, 9, 3, 21, 30, tzinfo=timezone.utc)
        assert "record data-center revenue" in entries[0].summary
        assert "<p>" not in entries[0].summary

    def test_atom_link_attribute(self):
        entries = parse_feed(fixture("atom_sample.xml"))
        assert len(entries) == 1
        assert entries[0].link == "https://www.sec.gov/news/press-release/2026-100"
        assert entries[0].published == datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc)

    def test_garbage_is_empty_not_an_exception(self):
        assert parse_feed(b"<html>not a feed") == []
        assert parse_feed(b"") == []

    def test_leading_bom_is_tolerated(self):
        assert len(parse_feed("﻿" + fixture("rss_sample.xml").decode())) == 3

    def test_naive_timestamps_get_utc(self):
        assert parse_datetime("2026-09-03T14:00:00").tzinfo is timezone.utc
        assert parse_datetime("garbage") is None

    def test_strip_html_truncates(self):
        assert strip_html("<b>hi</b> &amp; bye") == "hi & bye"
        assert len(strip_html("x" * 500, limit=100)) == 100


class TestMasterIndex:
    def test_parses_and_derives_urls(self):
        from conftest import MASTER_IDX

        rows = list(parse_master_idx(MASTER_IDX.decode()))
        assert len(rows) == 6
        row = rows[0]
        assert row.form_type == "4"
        assert row.accession == "0000320193-26-000075"
        assert row.folder_url.endswith("/edgar/data/320193/000032019326000075")
        assert row.index_url.endswith("0000320193-26-000075-index.htm")
        assert row.filed == date(2026, 9, 3)

    def test_header_lines_are_skipped(self):
        assert list(parse_master_idx("garbage\nheader|only|three")) == []


class TestSecBlockDetection:
    """SEC's two 403 pages mean different things; the report must say which."""

    def test_rate_limit_page(self):
        from stock_radar.http import sec_block_hint

        body = b"<html><title>SEC.gov | Request Rate Threshold Exceeded</title></html>"
        assert "频率超限" in sec_block_hint(body)
        assert "自己的机器" in sec_block_hint(body)

    def test_undeclared_tool_page(self):
        from stock_radar.http import sec_block_hint

        body = b"<html><title>SEC.gov | Your Request Originates from an Undeclared Automated Tool</title></html>"
        assert "user_agent" in sec_block_hint(body).lower()

    def test_unrelated_body_gets_no_hint(self):
        from stock_radar.http import sec_block_hint

        assert sec_block_hint(b"<html>404 not found</html>") == ""

    def test_hint_reaches_the_error_message(self):
        from stock_radar.http import HttpError, sec_block_hint

        hint = sec_block_hint(b"SEC.gov | Request Rate Threshold Exceeded")
        error = HttpError("https://www.sec.gov/x", 403, hint)
        assert "403" in str(error) and "频率超限" in str(error)
        assert error.hint == hint
