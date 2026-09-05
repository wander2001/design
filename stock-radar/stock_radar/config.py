"""Config loading: YAML file layered over built-in defaults, secrets from env."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "watchlist": {
        "tickers": [],
        "keywords": [],
        "people": [],
        "funds": [],
    },
    "sec": {
        # SEC requires a descriptive UA with a real contact address.
        "user_agent": "",
        "rate_per_sec": 5.0,
    },
    "sources": {
        "congress": {
            "enabled": True,
            "providers": ["house_clerk", "senate_efd"],
            "lookback_days": 45,
            "min_amount_usd": 1000,   # PTR ranges start at $1,001
            "stocks_only": True,
            "watchlist_only": False,
            "max_filings": 80,
            "house_mirror_url": "",
            "senate_mirror_url": "",
        },
        "insiders": {
            "enabled": True,
            "lookback_days": 3,
            "scope": "watchlist",  # watchlist | all
            "codes": ["P", "S"],
            "min_value_usd": 100000,
            "max_filings": 400,
            "workers": 6,
        },
        "funds": {
            "enabled": True,
            "lookback_days": 7,
            "top_n_changes": 12,
            "min_value_usd": 5_000_000,
        },
        "news": {
            "enabled": True,
            "lookback_hours": 30,
            "max_items": 40,
            "watchlist_only": False,
            "feeds": [
                {"name": "CNBC Markets", "url": "https://www.cnbc.com/id/20910258/device/rss/rss.html"},
                {"name": "CNBC Top News", "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html"},
                {"name": "Yahoo Finance", "url": "https://finance.yahoo.com/news/rssindex"},
                {"name": "MarketWatch Top", "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories"},
                {"name": "SEC Press", "url": "https://www.sec.gov/news/pressreleases.rss"},
                {"name": "Federal Reserve", "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
            ],
            "per_ticker_feed": "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US",
            "include_8k": True,
        },
    },
    "output": {
        "dir": "out",
        "formats": ["markdown", "html"],
        "language": "zh",
        "keep_history": True,
    },
    "state": {"path": ".stock-radar/state.db"},
    "cache": {"dir": "", "ttl_seconds": 0},
    "notify": {
        "slack": {"enabled": False, "webhook_env": "SLACK_WEBHOOK_URL"},
        "telegram": {"enabled": False, "token_env": "TELEGRAM_BOT_TOKEN", "chat_id_env": "TELEGRAM_CHAT_ID"},
        "webhook": {"enabled": False, "url_env": "STOCK_RADAR_WEBHOOK"},
        "email": {
            "enabled": False,
            "host_env": "SMTP_HOST",
            "port": 587,
            "user_env": "SMTP_USER",
            "password_env": "SMTP_PASSWORD",
            "from_addr": "",
            "to_addrs": [],
        },
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    def __init__(self, data: dict[str, Any], path: Path | None = None) -> None:
        self.data = data
        self.path = path

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None) -> "Config":
        if not path:
            return cls(copy.deepcopy(DEFAULTS))
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"config not found: {p}")
        loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"config root must be a mapping: {p}")
        return cls(_deep_merge(DEFAULTS, loaded), p)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    # -- convenience -----------------------------------------------------
    @property
    def tickers(self) -> list[str]:
        return [str(t).upper().strip() for t in self.get("watchlist.tickers", []) if str(t).strip()]

    @property
    def keywords(self) -> list[str]:
        return [str(k).strip() for k in self.get("watchlist.keywords", []) if str(k).strip()]

    @property
    def funds(self) -> list[dict[str, str]]:
        out = []
        for f in self.get("watchlist.funds", []) or []:
            if isinstance(f, dict) and f.get("cik"):
                out.append({"name": str(f.get("name") or f["cik"]), "cik": str(f["cik"]).zfill(10)})
        return out

    # Shipped placeholders that must never reach SEC as a real contact address.
    _UA_PLACEHOLDERS = ("your-email@example.com", "your name", "set sec_user_agent")

    @property
    def user_agent(self) -> str:
        configured = str(self.get("sec.user_agent") or "").strip()
        if configured and not self.user_agent_looks_placeholder(configured):
            return configured
        return os.environ.get("SEC_USER_AGENT", "").strip() or configured

    @classmethod
    def user_agent_looks_placeholder(cls, value: str) -> bool:
        lowered = value.lower()
        return any(marker in lowered for marker in cls._UA_PLACEHOLDERS)

    def user_agent_warning(self) -> str:
        """Empty when the UA is usable; otherwise why SEC is likely to reject it."""
        ua = self.user_agent
        if not ua:
            return "sec.user_agent 未设置（也没有 SEC_USER_AGENT 环境变量）；SEC 会限流甚至封禁匿名请求"
        if self.user_agent_looks_placeholder(ua):
            return f"sec.user_agent 还是模板占位值 {ua!r}；请改成你自己的姓名+邮箱"
        if "@" not in ua and "http" not in ua.lower():
            return f"sec.user_agent {ua!r} 里没有邮箱或网址，SEC 要求能联系到请求方"
        return ""

    def secret(self, dotted_env_key: str) -> str:
        """Read a secret by indirection: config holds the env var *name*, not the value."""
        env_name = self.get(dotted_env_key)
        return os.environ.get(env_name, "") if env_name else ""
