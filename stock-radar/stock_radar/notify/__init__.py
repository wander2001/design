"""Delivery channels. Each returns (name, ok, message) and never raises."""

from __future__ import annotations

import logging

from ..config import Config
from ..models import Report
from .channels import send_email, send_slack, send_telegram, send_webhook

log = logging.getLogger(__name__)

CHANNELS = {
    "slack": send_slack,
    "telegram": send_telegram,
    "webhook": send_webhook,
    "email": send_email,
}


def dispatch(config: Config, report: Report, markdown: str, html_body: str) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    for name, sender in CHANNELS.items():
        if not config.get(f"notify.{name}.enabled", False):
            continue
        try:
            message = sender(config, report, markdown, html_body)
            results.append((name, True, message))
        except Exception as exc:
            log.error("notifier %s failed: %s", name, exc)
            results.append((name, False, str(exc)))
    return results


__all__ = ["dispatch", "CHANNELS"]
