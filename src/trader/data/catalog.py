"""数据目录 data/catalog.sqlite（DESIGN.md 5.1）：每个分区（标的 × 日期）一行。

rows = 0 表示这一天已确认没有数据（例如上市之前），没有对应的文件，下载时不再重复请求。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from trader.data.validate import DayCheck

_SCHEMA = """
CREATE TABLE IF NOT EXISTS partitions (
    symbol      TEXT NOT NULL,
    kind        TEXT NOT NULL,      -- 'bar_1s'
    day         TEXT NOT NULL,      -- 纽约日期 YYYY-MM-DD
    source      TEXT NOT NULL,      -- 'massive'
    adjusted    TEXT NOT NULL,      -- 'raw'：未复权
    ts_meaning  TEXT NOT NULL,      -- 时间列含义
    rows        INTEGER NOT NULL,
    fingerprint TEXT,               -- rows = 0 时为空
    warnings    TEXT NOT NULL,      -- JSON {名称: 数量}
    gaps        TEXT NOT NULL,      -- JSON [[start_ns, end_ns], ...]
    imported_at INTEGER NOT NULL,   -- UTC 纳秒
    PRIMARY KEY (symbol, kind, day)
);
"""

TS_MEANING = "ts_start: UTC ns, bar interval start"


@dataclass(frozen=True, slots=True)
class Partition:
    symbol: str
    day: date
    rows: int
    fingerprint: str | None
    warnings: dict[str, int]
    gaps: list[tuple[int, int]]
    imported_at: int


class Catalog:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)  # 同一时间只在一个线程里使用
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def record(
        self,
        symbol: str,
        day: date,
        check: DayCheck,
        fingerprint: str | None,
        imported_at: int,
        source: str = "massive",
        kind: str = "bar_1s",
    ) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO partitions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    symbol,
                    kind,
                    day.isoformat(),
                    source,
                    "raw",
                    TS_MEANING,
                    check.rows,
                    fingerprint,
                    json.dumps(check.warnings),
                    json.dumps(check.gaps),
                    imported_at,
                ),
            )

    def known_days(self, symbol: str, kind: str = "bar_1s") -> set[date]:
        """已处理过的日期（含确认无数据的日期）。"""
        cur = self._db.execute(
            "SELECT day FROM partitions WHERE symbol = ? AND kind = ?", (symbol, kind)
        )
        return {date.fromisoformat(r[0]) for r in cur}

    def partitions(self, symbol: str, kind: str = "bar_1s") -> list[Partition]:
        cur = self._db.execute(
            "SELECT day, rows, fingerprint, warnings, gaps, imported_at FROM partitions "
            "WHERE symbol = ? AND kind = ? ORDER BY day",
            (symbol, kind),
        )
        return [
            Partition(
                symbol=symbol,
                day=date.fromisoformat(d),
                rows=rows,
                fingerprint=fp,
                warnings=json.loads(w),
                gaps=[tuple(g) for g in json.loads(gaps)],
                imported_at=t,
            )
            for d, rows, fp, w, gaps, t in cur
        ]

    def fingerprint(self, symbol: str, day: date, kind: str = "bar_1s") -> str | None:
        row = self._db.execute(
            "SELECT fingerprint FROM partitions WHERE symbol = ? AND kind = ? AND day = ?",
            (symbol, kind, day.isoformat()),
        ).fetchone()
        return row[0] if row else None

    def symbols(self, kind: str = "bar_1s") -> list[str]:
        cur = self._db.execute(
            "SELECT DISTINCT symbol FROM partitions WHERE kind = ? ORDER BY symbol", (kind,)
        )
        return [r[0] for r in cur]
