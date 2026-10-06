import json
from datetime import date, time

import polars as pl

from trader.core.timeutil import NS_PER_SEC, ny_to_ns
from trader.data.compare import bars_from_snapshots, bars_from_trades, compare_day, diff_stats
from trader.data.store import BAR_SCHEMA, BarStore

DAY = date(2026, 1, 5)
T0 = ny_to_ns(DAY, time(10, 0))


def test_bars_from_trades():
    trades = pl.DataFrame(
        {"ts": [T0, T0 + 1, T0 + NS_PER_SEC], "price": [10.0, 11.0, 9.0], "size": [1, 2, 3]}
    )
    bars = bars_from_trades(trades)
    assert bars.rows() == [
        (T0, 10.0, 11.0, 10.0, 11.0, 3.0),
        (T0 + NS_PER_SEC, 9.0, 9.0, 9.0, 9.0, 3.0),
    ]


def test_bars_from_snapshots_volume_is_cumulative_diff():
    snaps = pl.DataFrame(
        {
            "recv": [T0, T0 + 10, T0 + NS_PER_SEC, T0 + 2 * NS_PER_SEC],
            "last": [10.0, 10.5, None, 10.2],
            "volume": [1000.0, 1200.0, 1300.0, 1500.0],
        }
    )
    bars = bars_from_snapshots(snaps)
    assert bars["volume"].to_list() == [0.0, 300.0]
    assert bars["high"].to_list() == [10.5, 10.2]


def test_diff_stats():
    ref = pl.DataFrame(
        {
            "ts_start": [0, 1, 2],
            "open": [1.0] * 3,
            "high": [2.0] * 3,
            "low": [0.5] * 3,
            "close": [1.0, 1.0, 1.0],
            "volume": [100.0] * 3,
        }
    )
    other = ref.head(2).with_columns(pl.Series("close", [1.0, 1.02]))
    st = diff_stats(ref, other)
    assert st["matched"] == 2 and st["only_massive"] == 1
    assert abs(st["close_max_cents"] - 2.0) < 1e-9
    assert st["close_exact_pct"] == 50.0


def test_compare_day_end_to_end(tmp_path):
    ts = [T0 + i * NS_PER_SEC for i in range(120)]
    massive = pl.DataFrame(
        {
            "ts_start": ts,
            "open": 10.0,
            "high": 10.0,
            "low": 10.0,
            "close": 10.0,
            "volume": 5.0,
            "vwap": 10.0,
            "trades": 1,
        },
        schema=BAR_SCHEMA,
    )
    BarStore(tmp_path).write_day("SPY", DAY, massive)
    live = tmp_path / "live" / DAY.isoformat()
    live.mkdir(parents=True)
    with (live / "tbt.jsonl").open("w") as f:
        for t in ts[:100]:
            f.write(json.dumps({"sym": "SPY", "ts": t, "price": 10.0, "size": 5}) + "\n")
    out = tmp_path / "out"
    assert compare_day("SPY", DAY, tmp_path, out) == 0
    text = (out / f"{DAY}_SPY.txt").read_text(encoding="utf-8")
    assert "共同 100" in text and "完全相同 100.0%" in text
    assert "1 分钟 bar" in text
