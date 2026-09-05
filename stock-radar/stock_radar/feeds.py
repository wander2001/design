"""Minimal RSS 2.0 / Atom parser.

Written against stdlib rather than pulling in feedparser: the digest only needs
title, link, timestamp and summary, and fewer dependencies means fewer ways for
an unattended daily job to break.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

TAG_RE = re.compile(r"<[^>]+>")


def strip_html(raw: str, limit: int = 280) -> str:
    text = html.unescape(TAG_RE.sub(" ", raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[: limit - 1] + "…" if len(text) > limit else text


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _child_text(node: ET.Element, *names: str) -> str:
    wanted = set(names)
    for child in node:
        if _local(child.tag) in wanted:
            if child.text:
                return child.text.strip()
            # Atom content can carry inline XHTML children instead of text.
            inner = "".join(ET.tostring(g, encoding="unicode") for g in child)
            if inner:
                return inner
    return ""


def parse_datetime(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)  # RFC 822, used by RSS <pubDate>
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))  # ISO 8601, used by Atom
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass
class FeedEntry:
    title: str
    link: str
    published: datetime | None
    summary: str


def parse_feed(raw: bytes | str) -> list[FeedEntry]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    # Some publishers emit a BOM or leading whitespace ahead of the declaration.
    raw = raw.lstrip("﻿ \t\r\n")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []

    entries: list[FeedEntry] = []
    for node in root.iter():
        name = _local(node.tag)
        if name not in ("item", "entry"):
            continue
        title = strip_html(_child_text(node, "title"), 200)
        link = _child_text(node, "link")
        if not link:  # Atom puts the URL in an attribute
            for child in node:
                if _local(child.tag) == "link":
                    href = child.get("href")
                    rel = child.get("rel", "alternate")
                    if href and rel == "alternate":
                        link = href
                        break
        published = parse_datetime(
            _child_text(node, "pubDate", "published", "updated", "date")
        )
        summary = strip_html(_child_text(node, "description", "summary", "content"))
        if title or link:
            entries.append(FeedEntry(title=title, link=link.strip(), published=published, summary=summary))
    return entries
