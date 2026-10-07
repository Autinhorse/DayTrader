"""Massive（原 Polygon.io）REST 客户端：1 秒聚合 bar、拆股、分红。只属于研究版。

1 秒聚合一律取未复权（adjusted=false）。Massive 的时间戳 t 是 UTC 毫秒、bar 区间起点。
"""

from __future__ import annotations

import os
import threading
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import requests

from trader.core.timeutil import NS_PER_MS
from trader.data.store import BAR_SCHEMA, empty_bars

BASE_URL = "https://api.massive.com"


class MassiveError(Exception):
    pass


class Cancelled(Exception):
    pass


def find_api_key(project_dir: Path | None = None) -> str | None:
    """依次查找：环境变量 MASSIVE_API_KEY → .env → 旧下载工具的 config.json。"""
    if key := os.environ.get("MASSIVE_API_KEY"):
        return key
    root = project_dir or Path.cwd()
    env = root / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "MASSIVE_API_KEY" and value.strip():
                return value.strip().strip("'\"")
    legacy = root / "config.json"
    if legacy.exists():
        import json

        return json.loads(legacy.read_text(encoding="utf-8")).get("api_key") or None
    return None


class MassiveClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = BASE_URL,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.base_url = base_url
        self.stop_event = stop_event or threading.Event()
        self._local = threading.local()  # requests.Session 不保证线程安全，每个线程一个
        self._api_key = api_key

    @property
    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers["Authorization"] = f"Bearer {self._api_key}"
            self._local.session = s
        return s

    def check_stop(self) -> None:
        if self.stop_event.is_set():
            raise Cancelled()

    def get_json(self, url: str, params: dict[str, Any] | None = None, retries: int = 6) -> Any:
        """带重试的 GET。限流（429）和服务器错误时退避重试；其他错误直接抛出。"""
        err = ""
        for attempt in range(retries):
            self.check_stop()
            try:
                resp = self._session.get(url, params=params, timeout=60)
            except requests.RequestException as e:
                err = str(e)
            else:
                if resp.status_code == 200:
                    return resp.json()
                err = f"HTTP {resp.status_code}: {resp.text[:300]}"
                if resp.status_code not in (429, 500, 502, 503, 504):
                    raise MassiveError(err)
            self.stop_event.wait(2**attempt)  # 可被“停止”打断
        raise MassiveError(f"多次重试后仍失败：{err}")

    def _paged(self, url: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        next_url: str | None = url
        p: dict[str, Any] | None = params
        while next_url:
            data = self.get_json(next_url, p)
            rows.extend(data.get("results") or [])
            next_url = data.get("next_url")
            p = None  # next_url 已带全部查询参数
        return rows

    def _aggs(self, symbol: str, timespan: str, start: date, end: date) -> pl.DataFrame:
        """[start, end] 两端日期内的聚合 bar（纽约日期，含盘前盘后），未复权，按 ts_start 升序。"""
        rows = self._paged(
            f"{self.base_url}/v2/aggs/ticker/{symbol}/range/1/{timespan}/"
            f"{start.isoformat()}/{end.isoformat()}",
            {"adjusted": "false", "sort": "asc", "limit": 50000},
        )
        if not rows:
            return empty_bars()
        return pl.DataFrame(
            {
                "ts_start": [r["t"] * NS_PER_MS for r in rows],
                "open": [r["o"] for r in rows],
                "high": [r["h"] for r in rows],
                "low": [r["l"] for r in rows],
                "close": [r["c"] for r in rows],
                "volume": [r.get("v", 0.0) for r in rows],
                "vwap": [r.get("vw") for r in rows],
                "trades": [r.get("n") for r in rows],
            },
            schema=BAR_SCHEMA,
        )

    def bars_1s(self, symbol: str, day: date) -> pl.DataFrame:
        """某个纽约日期的全部 1 秒 bar。"""
        return self._aggs(symbol, "second", day, day)

    def bars_1m(self, symbol: str, start: date, end: date) -> pl.DataFrame:
        """[start, end] 内的官方 1 分钟 bar。一次最多 5 万根，约 50 个交易日。"""
        return self._aggs(symbol, "minute", start, end)

    def splits(self, symbol: str) -> list[dict[str, Any]]:
        return self._paged(
            f"{self.base_url}/v3/reference/splits", {"ticker": symbol, "limit": 1000}
        )

    def dividends(self, symbol: str) -> list[dict[str, Any]]:
        return self._paged(
            f"{self.base_url}/v3/reference/dividends", {"ticker": symbol, "limit": 1000}
        )
