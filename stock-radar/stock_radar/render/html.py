"""Standalone HTML report.

Styles are inlined on the elements rather than kept in a stylesheet because the
same markup is sent as an email body, and several mail clients drop <style>.
"""

from __future__ import annotations

import html

from ..models import Report
from .labels import SECTIONS, labels

ACCENT = {
    "congress": "#7c5cff",
    "insider": "#0f9d76",
    "fund": "#d97706",
    "news": "#2563eb",
}

PAGE = (
    "margin:0;padding:24px 12px;background:#f5f6f8;"
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',"
    "'Hiragino Sans GB','Microsoft YaHei',Roboto,Helvetica,Arial,sans-serif;color:#16181d;"
)
CARD = "max-width:760px;margin:0 auto;background:#ffffff;border-radius:14px;padding:28px 30px;border:1px solid #e4e6eb;"


def _esc(text: str) -> str:
    return html.escape(str(text or ""), quote=True)


def _chip(text: str, color: str) -> str:
    return (
        f'<span style="display:inline-block;padding:1px 7px;margin-right:5px;border-radius:999px;'
        f'background:{color}1a;color:{color};font-size:11px;font-weight:600;">{_esc(text)}</span>'
    )


def render_html(report: Report, language: str = "zh") -> str:
    L = labels(language)
    parts: list[str] = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>{_esc(L['title'])} {report.generated_at:%Y-%m-%d}</title></head>",
        f"<body style=\"{PAGE}\"><div style=\"{CARD}\">",
        f'<h1 style="margin:0 0 4px;font-size:22px;letter-spacing:-0.01em;">{_esc(L["title"])}</h1>',
        f'<p style="margin:0 0 22px;color:#6b7280;font-size:13px;">'
        f'{_esc(L["generated"])}: {report.generated_at:%Y-%m-%d %H:%M %Z}</p>',
    ]

    total = 0
    for kind in SECTIONS:
        rows = report.by_kind(kind)
        total += len(rows)
        color = ACCENT[kind]
        parts.append(
            f'<h2 style="margin:26px 0 10px;font-size:15px;padding-left:9px;'
            f'border-left:3px solid {color};">{_esc(L[kind])} '
            f'<span style="color:#9ca3af;font-weight:400;">({len(rows)})</span></h2>'
        )
        if not rows:
            parts.append(f'<p style="margin:0;color:#9ca3af;font-size:13px;">{_esc(L["empty"])}</p>')
            continue
        for item in rows:
            title = _esc(item.title)
            if item.url:
                title = (
                    f'<a href="{_esc(item.url)}" style="color:#16181d;text-decoration:none;'
                    f'border-bottom:1px solid #d8dbe0;">{title}</a>'
                )
            meta = " · ".join(filter(None, [str(item.when) if item.when else "", _esc(item.source)]))
            chips = "".join(_chip(t, color) for t in item.tickers[:4])
            parts.append(
                '<div style="padding:10px 0;border-bottom:1px solid #f0f1f3;">'
                f'<div style="font-size:14px;font-weight:600;line-height:1.45;">{chips}{title}</div>'
                + (
                    f'<div style="margin-top:3px;font-size:13px;color:#4b5563;line-height:1.5;">'
                    f"{_esc(item.summary)}</div>"
                    if item.summary
                    else ""
                )
                + (
                    f'<div style="margin-top:3px;font-size:11px;color:#9ca3af;">{meta}</div>'
                    if meta
                    else ""
                )
                + "</div>"
            )

    if total == 0:
        parts.append(
            f'<p style="margin:20px 0;padding:14px;background:#f8f9fb;border-radius:8px;'
            f'color:#6b7280;font-size:13px;">{_esc(L["nothing"])}</p>'
        )

    if report.statuses:
        parts.append(
            f'<h2 style="margin:30px 0 8px;font-size:13px;color:#6b7280;">{_esc(L["sources"])}</h2>'
            '<div style="font-size:12px;color:#6b7280;line-height:1.7;">'
        )
        for status in report.statuses:
            mark = "✅" if status.ok else "❌"
            note = f" — {_esc(status.message)}" if status.message else ""
            parts.append(
                f'{mark} <code style="background:#f1f2f4;padding:1px 5px;border-radius:4px;">'
                f"{_esc(status.name)}</code> {status.items} {_esc(L['items'])}{note}<br>"
            )
        parts.append("</div>")

    parts.append(
        f'<p style="margin:26px 0 0;padding-top:14px;border-top:1px solid #e9eaee;'
        f'color:#9ca3af;font-size:11px;line-height:1.6;">{_esc(L["disclaimer"])}</p>'
    )
    parts.append("</div></body></html>")
    return "".join(parts)
