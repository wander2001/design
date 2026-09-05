"""Markdown report — the canonical rendering; Slack/Telegram reuse its wording."""

from __future__ import annotations

from ..models import Report
from .labels import SECTIONS, labels


def render_markdown(report: Report, language: str = "zh") -> str:
    L = labels(language)
    out: list[str] = [
        f"# {L['title']} · {report.generated_at:%Y-%m-%d}",
        "",
        f"*{L['generated']}: {report.generated_at:%Y-%m-%d %H:%M %Z}*",
        "",
    ]

    total = 0
    for kind in SECTIONS:
        rows = report.by_kind(kind)
        total += len(rows)
        out.append(f"## {L[kind]} ({len(rows)})")
        out.append("")
        if not rows:
            out.append(f"_{L['empty']}_")
            out.append("")
            continue
        for item in rows:
            when = f" `{item.when}`" if item.when else ""
            link = f"[{item.title}]({item.url})" if item.url else item.title
            out.append(f"- **{link}**{when}")
            if item.summary:
                out.append(f"  - {item.summary}")
            if item.source:
                out.append(f"  - _{item.source}_")
        out.append("")

    if total == 0:
        out += [f"> {L['nothing']}", ""]

    if report.statuses:
        out += [f"## {L['sources']}", ""]
        for status in report.statuses:
            mark = "✅" if status.ok else "❌"
            state = L["ok"] if status.ok else L["failed"]
            note = f" — {status.message}" if status.message else ""
            out.append(f"- {mark} `{status.name}` {state} · {status.items} {L['items']}{note}")
        out.append("")

    out += ["---", f"_{L['disclaimer']}_"]
    return "\n".join(out)
