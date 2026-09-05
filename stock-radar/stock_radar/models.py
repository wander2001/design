"""Normalized data models shared by every collector and renderer."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any


def _iso(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


@dataclass
class Item:
    """One thing worth telling the user about.

    ``kind`` picks the report section; ``key`` is the dedup identity across runs;
    ``score`` drives ordering inside a section (higher first).
    """

    kind: str  # congress | insider | fund | news
    title: str
    url: str = ""
    when: date | None = None  # the event date, not the run date
    summary: str = ""
    tickers: list[str] = field(default_factory=list)
    score: float = 0.0
    source: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    _key: str | None = None

    @property
    def key(self) -> str:
        if self._key:
            return self._key
        raw = "|".join([self.kind, self.title, self.url, str(self.when)])
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def with_key(self, *parts: Any) -> "Item":
        raw = "|".join(str(p) for p in parts)
        self._key = f"{self.kind}:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
        return self

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("_key", None)
        d["key"] = self.key
        d["when"] = _iso(self.when)
        d["detail"] = {k: _iso(v) for k, v in self.detail.items()}
        return d


@dataclass
class SourceStatus:
    """Per-collector outcome, so a silent failure never looks like 'no news'."""

    name: str
    ok: bool
    items: int = 0
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Report:
    generated_at: datetime
    items: list[Item] = field(default_factory=list)
    statuses: list[SourceStatus] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[Item]:
        rows = [i for i in self.items if i.kind == kind]
        rows.sort(key=lambda i: (-i.score, -(i.when.toordinal() if i.when else 0)))
        return rows

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "items": [i.to_dict() for i in self.items],
            "statuses": [s.to_dict() for s in self.statuses],
        }
