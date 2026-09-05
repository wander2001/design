from __future__ import annotations

import json
import os
import smtplib
from email.message import EmailMessage

import requests

from ..config import Config
from ..models import Report
from ..render.text import render_text

TIMEOUT = 30


def send_slack(config: Config, report: Report, markdown: str, html_body: str) -> str:
    url = config.secret("notify.slack.webhook_env")
    if not url:
        raise RuntimeError(f"env {config.get('notify.slack.webhook_env')} is empty")
    text = render_text(report, config.get("output.language", "zh"))
    resp = requests.post(url, json={"text": text[:39000]}, timeout=TIMEOUT)
    resp.raise_for_status()
    return "posted to Slack"


def send_telegram(config: Config, report: Report, markdown: str, html_body: str) -> str:
    token = config.secret("notify.telegram.token_env")
    chat_id = config.secret("notify.telegram.chat_id_env")
    if not token or not chat_id:
        raise RuntimeError("telegram token or chat id env is empty")
    text = render_text(report, config.get("output.language", "zh"))
    # Telegram caps a message at 4096 chars; split on blank lines to keep sections whole.
    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        if len(current) + len(block) + 2 > 3800:
            chunks.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        chunks.append(current)

    for chunk in chunks:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
    return f"sent {len(chunks)} telegram message(s)"


def send_webhook(config: Config, report: Report, markdown: str, html_body: str) -> str:
    url = config.secret("notify.webhook.url_env")
    if not url:
        raise RuntimeError(f"env {config.get('notify.webhook.url_env')} is empty")
    payload = report.to_dict()
    payload["markdown"] = markdown
    resp = requests.post(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return f"POSTed to webhook ({resp.status_code})"


def send_email(config: Config, report: Report, markdown: str, html_body: str) -> str:
    host = config.secret("notify.email.host_env") or os.environ.get("SMTP_HOST", "")
    user = config.secret("notify.email.user_env")
    password = config.secret("notify.email.password_env")
    port = int(config.get("notify.email.port", 587))
    sender = config.get("notify.email.from_addr") or user
    recipients = [str(r) for r in config.get("notify.email.to_addrs", []) if str(r).strip()]
    if not host or not recipients or not sender:
        raise RuntimeError("email needs host, from_addr and to_addrs")

    msg = EmailMessage()
    lang = config.get("output.language", "zh")
    subject_base = "每日股票雷达" if lang == "zh" else "Daily Stock Radar"
    msg["Subject"] = f"{subject_base} · {report.generated_at:%Y-%m-%d}"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(render_text(report, lang))
    msg.add_alternative(html_body, subtype="html")

    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT)
    else:
        server = smtplib.SMTP(host, port, timeout=TIMEOUT)
    with server:
        if port != 465:
            server.starttls()
        if user and password:
            server.login(user, password)
        server.send_message(msg)
    return f"emailed {len(recipients)} recipient(s)"
