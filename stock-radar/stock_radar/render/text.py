"""Compact plain-text rendering for chat notifiers with tight length limits."""

from __future__ import annotations

from ..models import Report
from .labels import SECTIONS, labels


def render_text(report: Report, language: str = "zh", per_section: int = 5) -> str:
    L = labels(language)
    lines = [f"{L['title']} · {report.generated_at:%Y-%m-%d}"]
    for kind in SECTIONS:
        rows = report.by_kind(kind)
        if not rows:
            continue
        lines.append("")
        lines.append(f"{L[kind]} ({len(rows)})")
        for item in rows[:per_section]:
            lines.append(f"• {item.title}")
            if item.url:
                lines.append(f"  {item.url}")
        if len(rows) > per_section:
            lines.append(f"  … +{len(rows) - per_section}")
    if len(lines) == 1:
        lines.append(L["nothing"])
    return "\n".join(lines)
