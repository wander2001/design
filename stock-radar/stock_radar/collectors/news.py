"""Market news and 8-K disclosures (重要新闻).

Headlines come from publisher RSS; anything touching the watchlist is scored up
so the section stays readable when the feeds are noisy.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone

from ..feeds import parse_feed
from ..models import Item
from .base import CollectorContext, match_watchlist

log = logging.getLogger(__name__)

EIGHT_K = {"8-K", "8-K/A"}


def _normalize_link(url: str) -> str:
    """Drop tracking query strings so the same story dedups across runs."""
    base = url.split("?")[0].split("#")[0]
    return base.rstrip("/")


class NewsCollector:
    name = "news"

    def enabled(self, ctx: CollectorContext) -> bool:
        return bool(ctx.config.get("sources.news.enabled", True))

    def collect(self, ctx: CollectorContext) -> list[Item]:
        cfg = ctx.config
        lookback_hours = int(cfg.get("sources.news.lookback_hours", 30))
        max_items = int(cfg.get("sources.news.max_items", 40))
        watchlist_only = bool(cfg.get("sources.news.watchlist_only", False))
        tickers = cfg.tickers
        keywords = cfg.keywords
        # Anchor the window to ctx.today so `--date` backfills read the right window
        # instead of silently filtering everything out against the wall clock.
        now = datetime.now(timezone.utc)
        reference = (
            now
            if ctx.today >= now.date()
            else datetime.combine(ctx.today, time(23, 59, 59), tzinfo=timezone.utc)
        )
        cutoff = reference - timedelta(hours=lookback_hours)

        sources: list[tuple[str, str]] = [
            (str(f.get("name") or f.get("url")), str(f["url"]))
            for f in cfg.get("sources.news.feeds", [])
            if isinstance(f, dict) and f.get("url")
        ]
        per_ticker = cfg.get("sources.news.per_ticker_feed")
        if per_ticker:
            sources += [(f"{t} 个股新闻", str(per_ticker).format(ticker=t)) for t in tickers]

        def fetch(pair: tuple[str, str]):
            name, url = pair
            try:
                body = ctx.http.get(url, allow_404=True)
            except Exception as exc:
                log.warning("feed %s failed: %s", url, exc)
                ctx.notes.append(f"RSS {name} 抓取失败: {exc}")
                return name, []
            return name, parse_feed(body or b"")

        items: list[Item] = []
        seen_links: set[str] = set()
        with ThreadPoolExecutor(max_workers=6) as pool:
            for name, entries in pool.map(fetch, sources):
                for entry in entries:
                    if entry.published and not (cutoff <= entry.published <= reference + timedelta(days=1)):
                        continue
                    link = _normalize_link(entry.link)
                    if not link or link in seen_links:
                        continue
                    hits = match_watchlist(f"{entry.title} {entry.summary}", tickers, keywords)
                    if watchlist_only and not hits:
                        continue
                    seen_links.add(link)
                    score = 20.0 + 25.0 * len(hits)
                    if entry.published:
                        age_h = max(0.0, (reference - entry.published).total_seconds() / 3600)
                        score += max(0.0, 10.0 - age_h / 3)
                    item = Item(
                        kind="news",
                        title=entry.title or link,
                        url=entry.link,
                        when=entry.published.date() if entry.published else None,
                        summary=entry.summary,
                        tickers=hits,
                        score=score,
                        source=name,
                        detail={"published_at": entry.published, "feed": name},
                    )
                    items.append(item.with_key(link))

        items.sort(key=lambda i: -i.score)
        items = items[:max_items]

        if cfg.get("sources.news.include_8k", True):
            items.extend(self._eight_k(ctx))
        return items

    def _eight_k(self, ctx: CollectorContext) -> list[Item]:
        """8-K current reports from watchlist companies — material events, same day."""
        from ..edgar import business_days

        tickers = ctx.config.tickers
        if not tickers:
            return []
        try:
            cik_to_ticker = {str(int(c)): t for t, c in ctx.edgar.ciks_for_tickers(tickers).items()}
            rows = ctx.edgar.filings_of_type(business_days(ctx.today, 2), EIGHT_K)
        except Exception as exc:
            log.warning("8-K scan failed: %s", exc)
            ctx.notes.append(f"8-K 扫描失败: {exc}")
            return []

        items: list[Item] = []
        for row in rows:
            ticker = cik_to_ticker.get(str(int(row.cik)))
            if not ticker:
                continue
            item = Item(
                kind="news",
                title=f"{ticker} · {row.company} 提交 {row.form_type}（重大事件公告）",
                url=row.index_url,
                when=row.filed,
                summary="8-K 为美股上市公司重大事件的即时披露，点击查看原文条款。",
                tickers=[ticker],
                score=70.0,
                source="SEC 8-K",
                detail={"form": row.form_type, "accession": row.accession, "company": row.company},
            )
            items.append(item.with_key(row.accession))
        return items
