"""
下载美股 1 分钟 K 线（Massive，原 Polygon.io），按标的存为 Parquet 文件。

用法:
    1. pip install requests pandas pyarrow
    2. 设置环境变量 MASSIVE_API_KEY（在 Massive 后台的 Keys 页面复制）
         Windows PowerShell:  $env:MASSIVE_API_KEY="你的key"
         macOS / Linux:       export MASSIVE_API_KEY="你的key"
    3. python download_minute_bars.py

输出:
    data/SPY_1min.parquet, data/QQQ_1min.parquet ...
    中断后重新运行会跳过已下载的月份。
"""

import os
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests

# ---------------- 可修改的参数 ----------------
TICKERS = ["SPY", "QQQ"]
YEARS = 5                 # Stocks Starter 提供 5 年历史
ADJUSTED = True           # True = 拆股复权（不含分红调整）
OUT_DIR = Path("data")
# 两个域名并行可用，密钥通用；如果第一个不通可换成 https://api.polygon.io
BASE_URL = "https://api.massive.com"
# ---------------------------------------------

API_KEY = os.environ.get("MASSIVE_API_KEY")
if not API_KEY:
    sys.exit("请先设置环境变量 MASSIVE_API_KEY")

SESSION = requests.Session()
SESSION.headers["Authorization"] = f"Bearer {API_KEY}"


def get_json(url, params=None, retries=5):
    """带重试的 GET。遇到限流(429)或服务器错误时等待后重试。"""
    for attempt in range(retries):
        resp = SESSION.get(url, params=params, timeout=60)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** attempt)
            continue
        sys.exit(f"请求失败 {resp.status_code}: {resp.text[:300]}")
    sys.exit(f"多次重试后仍失败: {url}")


def fetch_month(ticker, start, end):
    """下载一个月的 1 分钟 K 线，自动翻页。"""
    url = f"{BASE_URL}/v2/aggs/ticker/{ticker}/range/1/minute/{start}/{end}"
    params = {"adjusted": str(ADJUSTED).lower(), "sort": "asc", "limit": 50000}
    rows = []
    while url:
        data = get_json(url, params)
        rows.extend(data.get("results") or [])
        url = data.get("next_url")   # 有下一页时返回完整链接
        params = None                # next_url 已包含查询参数
    return rows


def month_ranges(years):
    """生成从 years 年前到今天的 (月初, 月末) 列表。"""
    today = date.today()
    first = date(today.year - years, today.month, 1)
    starts = pd.date_range(first, today, freq="MS")
    for s in starts:
        e = min((s + pd.offsets.MonthEnd(0)).date(), today)
        yield s.date(), e


def to_frame(rows):
    df = pd.DataFrame(rows).rename(columns={
        "t": "ts", "o": "open", "h": "high", "l": "low", "c": "close",
        "v": "volume", "vw": "vwap", "n": "trades",
    })
    # 时间戳是 UTC 毫秒，表示该分钟的起始时刻；转成美东时间
    df["ts"] = (pd.to_datetime(df["ts"], unit="ms", utc=True)
                  .dt.tz_convert("America/New_York"))
    df = df.drop_duplicates("ts").sort_values("ts").set_index("ts")
    # 标记常规交易时段 09:30-16:00，其余为盘前盘后
    minutes = df.index.hour * 60 + df.index.minute
    df["regular"] = (minutes >= 570) & (minutes < 960)
    cols = ["open", "high", "low", "close", "volume", "vwap", "trades", "regular"]
    return df[[c for c in cols if c in df.columns]]


def download(ticker):
    part_dir = OUT_DIR / "_parts" / ticker
    part_dir.mkdir(parents=True, exist_ok=True)
    today = date.today()

    for start, end in month_ranges(YEARS):
        part = part_dir / f"{start:%Y-%m}.parquet"
        is_current_month = (start.year, start.month) == (today.year, today.month)
        if part.exists() and not is_current_month:
            continue  # 已下载过的完整月份直接跳过
        rows = fetch_month(ticker, start, end)
        if not rows:
            print(f"  {ticker} {start:%Y-%m}: 无数据")
            continue
        to_frame(rows).to_parquet(part)
        print(f"  {ticker} {start:%Y-%m}: {len(rows):>6} 根")

    parts = sorted(part_dir.glob("*.parquet"))
    if not parts:
        print(f"{ticker}: 没有下载到任何数据")
        return
    df = pd.concat(pd.read_parquet(p) for p in parts)
    df = df[~df.index.duplicated()].sort_index()
    out = OUT_DIR / f"{ticker}_1min.parquet"
    df.to_parquet(out)

    days = df.index.normalize().nunique()
    print(f"{ticker}: 共 {len(df):,} 根, {days} 个交易日, "
          f"{df.index[0]:%Y-%m-%d} 至 {df.index[-1]:%Y-%m-%d} -> {out}")


if __name__ == "__main__":
    OUT_DIR.mkdir(exist_ok=True)
    for t in TICKERS:
        print(f"下载 {t} ...")
        download(t)
