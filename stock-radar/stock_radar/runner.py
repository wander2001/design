"""Orchestration: run the collectors, drop what was already reported, render, deliver."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from .collectors.base import CollectorContext
from .collectors.congress import CongressCollector
from .collectors.funds import FundCollector
from .collectors.insiders import InsiderCollector
from .collectors.news import NewsCollector
from .config import Config
from .edgar import Edgar
from .http import Http
from .models import Item, Report, SourceStatus
from .notify import dispatch
from .render import render_html, render_markdown
from .state import State

log = logging.getLogger(__name__)


def build_collectors(state: State | None):
    return [
        CongressCollector(state=state),
        InsiderCollector(),
        FundCollector(state=state),
        NewsCollector(),
    ]


def run(
    config: Config,
    *,
    today: date | None = None,
    dedup: bool = True,
    only: list[str] | None = None,
    notify: bool = True,
    write_files: bool = True,
) -> tuple[Report, list[Path]]:
    today = today or datetime.now(timezone.utc).date()
    state = State(config.get("state.path", ".stock-radar/state.db")) if dedup else None

    http = Http(
        user_agent=config.user_agent or None,
        rate_per_sec=float(config.get("sec.rate_per_sec", 5.0)),
        cache_dir=config.get("cache.dir") or None,
        cache_ttl=int(config.get("cache.ttl_seconds", 0)),
    )
    ctx = CollectorContext(config=config, http=http, edgar=Edgar(http), today=today)

    items: list[Item] = []
    statuses: list[SourceStatus] = []
    ua_warning = config.user_agent_warning()
    if ua_warning:
        log.warning("%s", ua_warning)
        statuses.append(SourceStatus("sec.user_agent", False, 0, ua_warning))
    try:
        for collector in build_collectors(state):
            if only and collector.name not in only:
                continue
            if not collector.enabled(ctx):
                statuses.append(SourceStatus(collector.name, True, 0, "disabled"))
                continue
            ctx.notes.clear()
            errors_before = len(ctx.edgar.errors)
            try:
                collected = collector.collect(ctx)
            except Exception as exc:
                log.exception("collector %s failed", collector.name)
                statuses.append(SourceStatus(collector.name, False, 0, str(exc)[:300]))
                continue
            problems = ctx.notes + ctx.edgar.errors[errors_before:]

            fresh = collected
            if state is not None:
                new_keys = state.filter_new([i.key for i in collected])
                fresh = [i for i in collected if i.key in new_keys]
                state.mark_seen([(i.key, i.kind) for i in fresh])
            items.extend(fresh)
            statuses.append(_status(collector.name, fresh, collected, problems))
    finally:
        if state is not None:
            state.prune()
            state.close()

    report = Report(generated_at=datetime.now(timezone.utc), items=items, statuses=statuses)

    markdown = render_markdown(report, config.get("output.language", "zh"))
    html_body = render_html(report, config.get("output.language", "zh"))

    written: list[Path] = []
    if write_files:
        written = _write(config, report, markdown, html_body, today)
    if notify:
        for name, ok, message in dispatch(config, report, markdown, html_body):
            level = log.info if ok else log.error
            level("notify %s: %s", name, message)
    return report, written


def _status(name: str, fresh: list[Item], collected: list[Item], problems: list[str]) -> SourceStatus:
    """Fold swallowed sub-failures into the status, so 'quiet day' never masks 'broken'."""
    notes: list[str] = []
    if len(fresh) != len(collected):
        notes.append(f"{len(collected) - len(fresh)} 条已在往期报告出现")
    if problems:
        # Proxy/TLS errors are enormous; keep each one readable so three still fit.
        shown = "; ".join(p[:110] + ("…" if len(p) > 110 else "") for p in problems[:3])
        more = f" (另有 {len(problems) - 3} 个)" if len(problems) > 3 else ""
        notes.append(f"{len(problems)} 个子来源失败: {shown}{more}")
    # Nothing collected *and* something broke means the section is unreliable, not empty.
    ok = not (problems and not collected)
    return SourceStatus(name, ok, len(fresh), " · ".join(notes)[:400])


def _write(config: Config, report: Report, markdown: str, html_body: str, today: date) -> list[Path]:
    out_dir = Path(config.get("output.dir", "out"))
    out_dir.mkdir(parents=True, exist_ok=True)
    formats = {str(f).lower() for f in config.get("output.formats", ["markdown", "html"])}
    keep_history = bool(config.get("output.keep_history", True))

    payloads = {
        "markdown": ("md", markdown),
        "html": ("html", html_body),
        "json": ("json", json.dumps(report.to_dict(), ensure_ascii=False, indent=2)),
    }
    written: list[Path] = []
    for fmt, (ext, body) in payloads.items():
        if fmt not in formats:
            continue
        latest = out_dir / f"latest.{ext}"
        latest.write_text(body, encoding="utf-8")
        written.append(latest)
        if keep_history:
            dated = out_dir / f"{today:%Y-%m-%d}.{ext}"
            dated.write_text(body, encoding="utf-8")
            written.append(dated)
    return written
