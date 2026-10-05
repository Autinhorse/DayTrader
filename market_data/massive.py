"""
从 Massive（原 Polygon.io）下载美股 K 线。

每只股票、每种 K 线周期一套数据，包含盘前/盘中/盘后全部时段（session 列标记）:
    秒线按月分文件   data/<TICKER>/<TICKER>_1second_adj/2026-09.parquet ...
    其它周期一个文件 data/<TICKER>/<TICKER>_1minute_adj.parquet
    已下载完成的日期 data/<TICKER>/<TICKER>_<周期>_<adj|raw>.meta.json
再次下载时只请求缺少的日期（增量）。

不依赖 GUI，可在研究脚本中直接调用:
    from market_data.massive import load_bars
    df = load_bars("SPY", "1second", start="2026-09-01", sessions={"regular"})
"""

import json
import os
import threading
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

# 两个域名并行可用，密钥通用；如果第一个不通可换成 https://api.polygon.io
BASE_URL = "https://api.massive.com"
TZ = "America/New_York"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# 交易时段（美东时间，自 0 点起的分钟数）
SESSIONS = {
    "pre": (4 * 60, 9 * 60 + 30),          # 盘前 04:00-09:30
    "regular": (9 * 60 + 30, 16 * 60),     # 盘中 09:30-16:00
    "post": (16 * 60, 20 * 60),            # 盘后 16:00-20:00
}
INTRADAY = {"second", "minute", "hour"}

# 内存中累计到这么多根新 K 线就保存一次，避免一次下载很长区间时占用过多内存
FLUSH_ROWS = 3_000_000


class MassiveError(Exception):
    pass


class Cancelled(Exception):
    pass


class MassiveClient:
    def __init__(self, api_key, base_url=BASE_URL, stop_event=None):
        self.base_url = base_url
        self.stop_event = stop_event or threading.Event()
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {api_key}"

    def _check_stop(self):
        if self.stop_event.is_set():
            raise Cancelled()

    def get_json(self, url, params=None, retries=5):
        """带重试的 GET。遇到限流(429)或服务器错误时等待后重试。"""
        for attempt in range(retries):
            self._check_stop()
            try:
                resp = self.session.get(url, params=params, timeout=60)
            except requests.RequestException as e:
                err = str(e)
            else:
                if resp.status_code == 200:
                    return resp.json()
                err = f"HTTP {resp.status_code}: {resp.text[:300]}"
                if resp.status_code not in (429, 500, 502, 503, 504):
                    raise MassiveError(err)
            self.stop_event.wait(2 ** attempt)   # 可被“停止”打断
        raise MassiveError(f"多次重试后仍失败: {err}")

    def fetch_aggs(self, ticker, multiplier, timespan, start, end, adjusted=True):
        """下载 [start, end] 区间的聚合 K 线，自动翻页。"""
        url = (f"{self.base_url}/v2/aggs/ticker/{ticker}/range/"
               f"{multiplier}/{timespan}/{start}/{end}")
        params = {"adjusted": str(adjusted).lower(), "sort": "asc", "limit": 50000}
        rows = []
        while url:
            data = self.get_json(url, params)
            rows.extend(data.get("results") or [])
            url = data.get("next_url")   # 有下一页时返回完整链接
            params = None                # next_url 已包含查询参数
        return rows


@dataclass
class DownloadRequest:
    ticker: str
    start: date
    end: date
    multiplier: int = 1
    timespan: str = "second"            # second / minute / hour / day / week
    adjusted: bool = True

    @property
    def span_name(self):
        return f"{self.multiplier}{self.timespan}"


def dataset_base(data_dir, ticker, span_name, adjusted=True):
    """数据集的基础路径（不含扩展名）。秒线是同名目录，其它周期加 .parquet。"""
    adj = "adj" if adjusted else "raw"
    return Path(data_dir) / ticker / f"{ticker}_{span_name}_{adj}"


def meta_path(base):
    return base.with_name(base.name + ".meta.json")


def load_done(base):
    """读取已下载完成的日期集合。"""
    try:
        meta = json.loads(meta_path(base).read_text(encoding="utf-8"))
        return {date.fromisoformat(d) for d in meta["done"]}
    except (OSError, ValueError, KeyError):
        return set()


def missing_chunks(start, end, done, timespan):
    """找出 [start, end] 内尚未下载的工作日，并按粒度合并成请求区间:
    秒线每天一个请求，分钟线同一个月合并，其余同一年合并。"""
    days = [start + timedelta(i) for i in range((end - start).days + 1)]
    days = [d for d in days if d.weekday() < 5 and d not in done]
    groups = {}
    for d in days:
        key = d if timespan == "second" else (d.year, d.month) if timespan == "minute" else d.year
        groups.setdefault(key, []).append(d)
    return [(g[0], g[-1]) for g in groups.values()]


def session_of(index):
    """根据 K 线起始时刻标记所属时段: pre / regular / post。"""
    minutes = index.hour * 60 + index.minute
    out = pd.Series("post", index=index)
    out[minutes < SESSIONS["regular"][1]] = "regular"
    out[minutes < SESSIONS["regular"][0]] = "pre"
    return out.values


COLUMNS = ["open", "high", "low", "close", "volume", "vwap", "trades"]


def to_frame(rows, intraday):
    df = pd.DataFrame(rows, columns=["t", "o", "h", "l", "c", "v", "vw", "n"])
    df = df.rename(columns={
        "t": "ts", "o": "open", "h": "high", "l": "low", "c": "close",
        "v": "volume", "vw": "vwap", "n": "trades",
    })
    # 时间戳是 UTC 毫秒，表示该 K 线的起始时刻；转成美东时间
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert(TZ)
    df = df.drop_duplicates("ts").sort_values("ts").set_index("ts")[COLUMNS]
    if intraday:
        df["session"] = session_of(df.index)
    return df


def merge_into(path, new):
    """把新数据并入 parquet 文件（同一时刻以新数据为准）。
    先写临时文件再替换，中途出错不会损坏原文件。"""
    if path.exists():
        new = pd.concat([pd.read_parquet(path), new])
    new = new[~new.index.duplicated(keep="last")].sort_index()
    tmp = path.with_name(path.name + ".tmp")
    new.to_parquet(tmp, row_group_size=500_000)
    os.replace(tmp, path)


def save(base, partitioned, frames, done):
    """保存新数据，再记录完成的日期。秒线只重写涉及到的月份文件。"""
    if frames:
        new = pd.concat(frames)
        if partitioned:
            base.mkdir(exist_ok=True)
            for month, part in new.groupby(new.index.strftime("%Y-%m")):
                merge_into(base / f"{month}.parquet", part)
        else:
            merge_into(base.with_name(base.name + ".parquet"), new)
    meta = {"done": sorted(d.isoformat() for d in done)}
    meta_path(base).write_text(json.dumps(meta, indent=0), encoding="utf-8")


def download(client, req, data_dir=DATA_DIR, log=print, progress=None):
    """增量下载一个标的，返回数据路径（秒线为目录，其它为文件）。"""
    ticker = req.ticker.upper()
    intraday = req.timespan in INTRADAY
    partitioned = req.timespan == "second"
    base = dataset_base(data_dir, ticker, req.span_name, req.adjusted)
    base.parent.mkdir(parents=True, exist_ok=True)
    out = base if partitioned else base.with_name(base.name + ".parquet")

    today = pd.Timestamp.now(TZ).date()   # 以美东日期判断某天是否已收盘
    end = min(req.end, today)
    done = load_done(base)
    chunks = missing_chunks(req.start, end, done, req.timespan)
    if not chunks:
        log(f"{ticker}: {req.start} ~ {end} 已全部下载过，无需更新")
        return out
    log(f"{ticker}: 需要下载 {len(chunks)} 段")

    frames, pending, n_rows, total = [], set(), 0, 0
    try:
        for i, (s, e) in enumerate(chunks, 1):
            client._check_stop()
            df = to_frame(client.fetch_aggs(ticker, req.multiplier, req.timespan,
                                            s, e, req.adjusted), intraday)
            if len(df):
                frames.append(df)
                n_rows += len(df)
                total += len(df)
            # 今天还没结束，不算完成，下次会重新下载
            pending.update(s + timedelta(k) for k in range((e - s).days + 1)
                           if s + timedelta(k) < today)
            log(f"  {ticker} {s}~{e}: {len(df):>9,} 根")
            if progress:
                progress(i, len(chunks))
            if n_rows >= FLUSH_ROWS:
                save(base, partitioned, frames, done | pending)
                done |= pending
                frames, pending, n_rows = [], set(), 0
    finally:
        # 中途停止或出错时，也把已下载的部分保存下来
        if frames or pending:
            save(base, partitioned, frames, done | pending)
            done |= pending

    span = f"{min(done)} ~ {max(done)}" if done else "无"
    log(f"{ticker}: 本次新增 {total:,} 根，已覆盖日期 {span} -> {out}")
    return out


FREQ_UNITS = {"second": "s", "minute": "min", "hour": "h"}


def fill_gaps(df, freq):
    """补齐没有成交的空缺 K 线，使时间等间隔。
    只在每天每个时段的第一根和最后一根之间补（不跨日、不跨时段，提前收盘的日子也不会多补）。
    补出的 K 线: open/high/low/close/vwap 都等于上一根的收盘价，volume 和 trades 为 0。"""
    parts = []
    for (_, session), g in df.groupby([df.index.date, df["session"]], sort=False):
        full = pd.date_range(g.index[0], g.index[-1], freq=freq, name=df.index.name)
        g = g.reindex(full)
        close = g["close"].ffill()
        for col in ("open", "high", "low", "vwap"):
            g[col] = g[col].fillna(close)
        g["close"] = close
        g[["volume", "trades"]] = g[["volume", "trades"]].fillna(0)
        g["session"] = session
        parts.append(g)
    if not parts:
        return df
    return pd.concat(parts).sort_index().astype(df.dtypes.to_dict())


def load_bars(ticker, span_name="1second", start=None, end=None, sessions=None,
              adjusted=True, data_dir=DATA_DIR, fill=False):
    """读取 K 线数据。start/end 为日期字符串（含两端），sessions 如 {"regular"}。
    只读取所需日期范围，不会把整个大文件读进内存。
    fill=True 时补齐没有成交的空缺 K 线（仅日内周期，见 fill_gaps），可用 volume == 0 识别补出的行。"""
    base = dataset_base(data_dir, ticker.upper(), span_name, adjusted)
    if base.is_dir():   # 秒线：只读涉及到的月份文件
        lo = pd.Timestamp(start).strftime("%Y-%m") if start else ""
        hi = pd.Timestamp(end).strftime("%Y-%m") if end else "9999"
        paths = [p for p in sorted(base.glob("????-??.parquet")) if lo <= p.stem <= hi]
    else:
        paths = [base.with_name(base.name + ".parquet")]
    filters = []
    if start:
        filters.append(("ts", ">=", pd.Timestamp(start, tz=TZ)))
    if end:
        filters.append(("ts", "<", pd.Timestamp(end, tz=TZ) + pd.Timedelta(days=1)))
    if sessions:
        filters.append(("session", "in", list(sessions)))
    frames = [pd.read_parquet(p, filters=filters or None) for p in paths]
    if not frames:
        raise FileNotFoundError(f"没有找到数据: {base}")
    df = pd.concat(frames)
    if fill:
        n = len(span_name.rstrip("abcdefghijklmnopqrstuvwxyz"))
        unit = FREQ_UNITS.get(span_name[n:])
        if unit:   # 日线及以上不补
            df = fill_gaps(df, f"{span_name[:n]}{unit}")
    return df
