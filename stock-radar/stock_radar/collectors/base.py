"""Collector contract: every source returns Items and never raises into the runner."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from ..config import Config
from ..edgar import Edgar
from ..http import Http
from ..models import Item

log = logging.getLogger(__name__)


@dataclass
class CollectorContext:
    config: Config
    http: Http
    edgar: Edgar
    today: date
    # Non-fatal problems the current collector hit; the runner folds these into
    # the report's source-status block so a section that failed never renders as
    # an ordinary quiet day.
    notes: list[str] = field(default_factory=list)


class Collector(Protocol):
    name: str

    def enabled(self, ctx: CollectorContext) -> bool: ...

    def collect(self, ctx: CollectorContext) -> list[Item]: ...


def match_watchlist(text: str, tickers: list[str], keywords: list[str]) -> list[str]:
    """Tickers whose symbol or configured keyword appears in ``text``.

    Symbols are matched on word boundaries so 'A' or 'IT' don't light up on prose.
    """
    import re

    found: list[str] = []
    upper = text.upper()
    for ticker in tickers:
        if re.search(rf"(?<![A-Z0-9._]){re.escape(ticker)}(?![A-Z0-9.])", upper):
            found.append(ticker)
    lower = text.lower()
    for kw in keywords:
        if kw.lower() in lower and kw.upper() not in found:
            found.append(kw)
    return found
