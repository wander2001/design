"""HTTP layer: one shared session with a rate limiter, retries and an optional disk cache.

The SEC asks automated clients for a descriptive User-Agent with a contact address
and no more than 10 requests/second; both are enforced here rather than left to
each collector.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

DEFAULT_UA = "stock-radar/0.1 (contact: set SEC_USER_AGENT)"


class RateLimiter:
    """Token-free, simple spacing limiter: at most ``rate`` requests per second."""

    def __init__(self, rate: float) -> None:
        self._min_gap = 1.0 / rate if rate > 0 else 0.0
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self) -> None:
        if self._min_gap <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            self._next_at = max(now, self._next_at) + self._min_gap
        if wait > 0:
            time.sleep(wait)


# SEC serves both of these as a 403 HTML page through Akamai. They mean different
# things and have different remedies, so the page is read rather than guessed at.
SEC_BLOCK_HINTS = (
    (
        "Request Rate Threshold Exceeded",
        "SEC 判定该 IP 请求频率超限。共享出口 IP（GitHub Actions、云主机）常年处于超限状态，"
        "换 User-Agent 没用；把任务放到自己的机器上跑，或降低 sec.rate_per_sec",
    ),
    (
        "Undeclared Automated Tool",
        "SEC 认为请求没有声明来源。把 sec.user_agent（或 SEC_USER_AGENT 环境变量）"
        "设成 '你的名字 你的邮箱@域名' 这种带联系方式的格式",
    ),
)


class HttpError(RuntimeError):
    def __init__(self, url: str, status: int, hint: str = "") -> None:
        super().__init__(f"HTTP {status} for {url}" + (f" — {hint}" if hint else ""))
        self.url = url
        self.status = status
        self.hint = hint


def sec_block_hint(body: bytes) -> str:
    """The remedy SEC's own rejection page implies, or '' if it is not one of them."""
    head = body[:4000].decode("utf-8", errors="replace")
    for marker, hint in SEC_BLOCK_HINTS:
        if marker in head:
            return hint
    return ""


class Http:
    def __init__(
        self,
        user_agent: str | None = None,
        rate_per_sec: float = 5.0,
        timeout: float = 30.0,
        retries: int = 3,
        cache_dir: str | os.PathLike[str] | None = None,
        cache_ttl: int = 0,
    ) -> None:
        self.user_agent = user_agent or os.environ.get("SEC_USER_AGENT") or DEFAULT_UA
        self.timeout = timeout
        self.retries = retries
        self.limiter = RateLimiter(rate_per_sec)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.cache_ttl = cache_ttl
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.user_agent,
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
            }
        )

    # -- cache -----------------------------------------------------------
    def _cache_path(self, url: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".bin")

    def _cache_read(self, url: str) -> bytes | None:
        path = self._cache_path(url)
        if not path or not path.exists():
            return None
        if self.cache_ttl and time.time() - path.stat().st_mtime > self.cache_ttl:
            return None
        return path.read_bytes()

    def _cache_write(self, url: str, body: bytes) -> None:
        path = self._cache_path(url)
        if path:
            path.write_bytes(body)

    # -- requests --------------------------------------------------------
    def get(self, url: str, *, allow_404: bool = False, headers: dict | None = None) -> bytes | None:
        """Fetch ``url``; return the body, or None when a tolerated 404 is hit."""
        cached = self._cache_read(url)
        if cached is not None:
            return cached

        last_exc: Exception | None = None
        for attempt in range(self.retries):
            self.limiter.acquire()
            try:
                resp = self.session.get(url, timeout=self.timeout, headers=headers)
            except requests.RequestException as exc:  # network-level failure
                last_exc = exc
                log.debug("GET %s failed (%s), attempt %d", url, exc, attempt + 1)
                time.sleep(2**attempt)
                continue

            if resp.status_code == 404 and allow_404:
                return None
            if resp.status_code in (429, 500, 502, 503, 504):
                log.debug("GET %s -> %d, backing off", url, resp.status_code)
                time.sleep(2**attempt + 1)
                last_exc = HttpError(url, resp.status_code)
                continue
            if not resp.ok:
                raise HttpError(url, resp.status_code, sec_block_hint(resp.content))
            self._cache_write(url, resp.content)
            return resp.content

        if isinstance(last_exc, Exception):
            raise last_exc
        raise HttpError(url, 0)

    def get_text(self, url: str, *, allow_404: bool = False, encoding: str = "utf-8") -> str | None:
        body = self.get(url, allow_404=allow_404)
        return None if body is None else body.decode(encoding, errors="replace")

    def get_json(self, url: str, *, allow_404: bool = False) -> Any:
        body = self.get(url, allow_404=allow_404)
        return None if body is None else json.loads(body)
