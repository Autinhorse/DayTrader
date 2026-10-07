# 日内交易系统：分模块设计说明书

Oct 6, 2026 · @Rex

## 1. 概述

本系统是一套个人使用的美股日内交易系统：一份核心代码，生成研究版和实盘版两个应用。研究版用本地历史数据做看盘、回放练习和策略回测；实盘版通过 IBKR 接实时行情并下单。使用者的主要精力放在研究分析上，代码由 Claude Code 开发和维护。

### 1.1 目标

- 研究闭环优先：导入数据 → 选择策略与参数 → 回测 → 查看成本与交易原因 → 点击交易跳到图表复盘 → 保存和比较实验。日常操作全部在界面完成。
- 统一行情层：本地历史数据（Massive，秒级 OHLCV，可选 tick）和 IBKR 实时行情，向上输出同一种事件，并显式携带来源与质量信息。
- 图表：K 线、多周期、内置技术指标；用户新增指标只需写一个 Python 文件，只要它使用前端已支持的绘图类型，前端无需改动。
- 下单：手动下单和自动策略走同一条订单路径，经过同一套风控。
- 策略插件：策略是可挂载的 Python 模块，同一份代码用于回测、回放、模拟盘和实盘。
- 回测：对任意历史时段自动回跑，输出每笔订单的方向、数量、触发原因和盈亏，以及汇总统计和成本敏感性。
- 实盘先做半自动：策略给出信号，由人逐笔确认后才发单；运行稳定后再按策略逐个放开全自动。
- Notebook 接口作为补充：需要自由分析时，在 Jupyter 中调用回测和数据接口，结果以 DataFrame 返回。

### 1.2 非目标（第一版不做）

- 亚秒级延迟敏感的高频策略、Level 2 盘口。
- 期权、期货、外汇；多账户、多用户、云端部署。
- 机器学习训练管线、组合优化。

### 1.3 设计原则

1. **策略只有一份。** 策略代码中不允许出现按运行模式分支的判断；差异全部由注入的行情源、执行器和时钟承担。
2. **共享的是策略决策和订单语义，不是成交结果。** 回测用模拟撮合，实盘由券商和市场成交。代码相同不代表回测收益等于实盘收益；成交、延迟、数据和成本的差异要在报告中可见。
3. **共享代码，隔离运行。** 研究版和实盘版是独立进程，使用各自的配置、运行数据库、端口和依赖环境。研究侧的任何操作不能影响交易引擎。
4. **事件驱动，确定性优先。** 同样的配置、数据和代码，回测的业务结果必须完全相同（比较时排除运行编号、生成时间等字段）。
5. **禁止未来数据。** 策略只能看到已收盘且已到达可用时间的数据。
6. **数据质量差异不隐藏。** 合成数据、降级行情、缺口都带标记；不满足策略声明的数据要求时暂停策略，不在后台悄悄替换。
7. **实盘保守。** 状态对不上时停止策略并报警，不自动修正；实盘应用体积最小、依赖最少。
8. **每笔订单必须带原因。** 原因字段在下单时写入，贯穿订单、成交和回测报告。

## 2. 总体架构

引擎核心只有一份，五种运行组合的区别只在注入的三个组件：行情源、执行器、时钟。

&#91;embedded content: 系统架构 · 共享核心与两端可替换组件\]

中间的引擎核心和上方的界面在所有模式下是同一份代码；左右两端各有两个实现，研究版和实盘版各取一个。

### 2.1 运行组合

| 运行组合 | 用途 | 行情源 | 执行器 | 时钟 | 所属应用 |
| --- | --- | --- | --- | --- | --- |
| backtest | 策略批量回测 | 历史回放，全速 | 模拟撮合 | 模拟时钟 | 研究版 |
| replay | 看盘练习、手动模拟交易、复盘 | 历史回放，可调速、暂停、单步 | 模拟撮合 | 模拟时钟 | 研究版 |
| local\_paper | 在实时行情下验证聚合和撮合假设 | IBKR 实时 | 模拟撮合 | 真实时钟 | 实盘版 |
| broker\_paper | 验证券商接口、恢复和对账 | IBKR 实时 | IBKR 模拟账户 | 真实时钟 | 实盘版 |
| live | 真实交易 | IBKR 实时 | IBKR 实盘账户 | 真实时钟 | 实盘版 |

这些是行情源与执行器的组合，策略代码只有一套。IBKR 模拟账户的成交机制与实盘不同，不能作为真实成交质量的证明。

### 2.2 事件流

1. 行情源产生 `BarEvent`（1 秒 bar）或 `TickEvent`，按时间戳进入引擎。
2. Bar 聚合器把 1 秒 bar 合成策略订阅的周期（如 1 分钟、5 分钟），并更新指标。
3. 引擎把收盘的 bar 分发给策略；策略返回订单意图 `OrderIntent`。
4. 订单管理模块做风控检查，通过后交给执行器。
5. 执行器返回订单状态和成交；引擎更新持仓，并回调策略的 `on_fill`。
6. 所有事件同时写入事件日志，并通过 WebSocket 推送到界面。

手动下单从界面进入第 4 步，之后与策略订单完全一致。

### 2.3 引擎主循环

引擎提供唯一的事件处理入口 `Engine.process(event)`，两种驱动方式调用同一个入口：

- `run_backtest()`：同步循环，从确定性调度器取事件，不依赖 asyncio，保证速度和确定性。
- `run_realtime()`：asyncio 循环，从队列取事件；replay、local\_paper、broker\_paper、live 共用。

行情、订单回报、定时器和交易时段事件全部进入同一个调度器，顺序由 `(可用时间, 阶段, 序号)` 决定。同一时刻的阶段顺序固定为：

1. 撮合：用刚结束的市场区间处理此前已生效的挂单。
2. 交付该区间产生的成交和订单更新，回调策略的 `on_fill` 和 `on_order`。
3. 发布该区间收盘的 bar，更新指标，回调 `on_bar`。
4. 交易时段事件，然后是定时器。

策略在第 2 到第 4 步中发出的新订单，只能参与之后的市场区间。例如策略在 10:00:01 得知 \[10:00:00, 10:00:01) 的收盘价，它的订单不能用这根 bar 的任何价格成交。模拟时钟在行情稀疏时照常推进定时器和交易时段事件。

### 2.4 两个应用的边界

- 两个应用是独立进程，各自使用独立的配置、运行数据库（SQLite 文件）、端口和依赖环境；历史行情目录可以共享，实盘版只读。
- 研究版入口的组件注册表里不包含 IBKR 执行器，无论配置怎么写都不可能发出真实订单。
- 实盘版入口不导入 notebook、参数扫描、绘图等研究依赖，也不支持策略热加载。
- 回测和参数扫描在独立的 worker 进程中运行，不占用界面服务的事件循环。
- 交易引擎不等待界面：浏览器断开、关闭或消费消息过慢，都不影响引擎运行；推送队列满时丢弃旧的行情推送，不阻塞引擎。
- 实盘版从单独的目录运行（见第 12.5 节），研究目录里的改动和切换分支不影响它。
- 前端只构建一次；界面根据后端 `/api/meta` 返回的模式和能力开关显示或隐藏功能。

## 3. 技术选型与项目结构

后端 Python，前端 Web，本机运行。库的版本一律取开发时的最新稳定版并锁定在 lock 文件里。

### 3.1 技术选型

| 部分 | 选型 | 说明 |
| --- | --- | --- |
| 语言与包管理 | Python 3.12 以上，uv | 单仓库，一个核心包加两个入口 |
| IBKR 接口 | [ib\_async](https://ib-api-reloaded.github.io/ib_async) | ib\_insync 的延续项目，要求 Python 3.10 以上 |
| 行情存储 | Parquet 文件，DuckDB 查询 | 按标的和日期分区，本地文件即可 |
| 数据处理 | Polars（核心），pandas（仅 notebook 接口输出） |  |
| 运行记录 | SQLite | 订单、成交、回测索引、事件日志索引 |
| 交易日历 | exchange\_calendars | 开收盘时间、半日市、节假日 |
| 配置与校验 | pydantic、pydantic-settings，YAML 配置文件 |  |
| 界面 | PySide6 桌面程序（决策 0005） | 可停靠、可拖出成独立窗口的面板；实盘版界面与引擎分属两个进程，本机通信只监听 127.0.0.1 |
| 图表 | TradingView Lightweight Charts 5（开源库），嵌在 QWebEngineView 中 | K 线、指标窗格、买卖点标记 |
| 研究 | JupyterLab | 只属于研究版依赖组 |
| 质量工具 | pytest、ruff、pyright、import-linter | import-linter 用于强制分层 |

### 3.2 目录结构

```text
trader/
  CLAUDE.md                 # 给 Claude Code 的规则摘要
  docs/DESIGN.md            # 本文档
  docs/decisions/           # 设计决策记录
  pyproject.toml            # 依赖分组：core / research / live / dev
  config/
    research.yaml  local_paper.yaml  broker_paper.yaml  live.yaml  universe.yaml
  src/trader/
    core/          # 数据模型、事件、时钟、交易日历、引擎
    data/          # 存储、导入、历史查询、回放行情源
    indicators/    # 指标基类、注册表、内置指标
    strategy/      # 策略基类、上下文、加载器
    oms/           # 订单归约、持仓、标的归属、风控
    brokers/
      sim/         # 模拟撮合
      ibkr/        # IBKR 行情源与执行器（仅实盘版可导入）
    backtest/      # 回测运行器、统计、参数扫描
    research/      # notebook 接口（仅研究版可导入）
    gui/           # PySide6 界面（研究版；实盘版界面在阶段 6 加入）
    apps/
      research_app.py   # 入口：trader-research
      live_app.py       # 入口：trader-live
  user/
    indicators/    # 用户自定义指标，自动发现
    strategies/    # 用户策略，自动发现
    notebooks/
  data/            # 行情数据（不进 git）
  runs/            # 回测与运行结果（不进 git）
  tests/
```

### 3.3 分层依赖规则

依赖方向只能自上而下，由 import-linter 在 CI 中检查：

- `core` 不依赖任何其他子包。
- `indicators`、`strategy`、`oms` 只依赖 `core`（`strategy` 可依赖 `indicators`）。
- `user/strategies` 只能导入 `trader.strategy`、`trader.indicators`、`trader.core`。
- `brokers.ibkr` 不被 `backtest`、`research`、`research_app` 导入。
- `research` 不被 `live_app` 导入。

## 4. 核心数据模型与事件

所有模块只通过本节定义的类型交互。类型放在 `trader.core`，使用不可变 dataclass（`frozen=True, slots=True`）。

### 4.1 约定

- **时间**：内部统一用 UTC 纳秒整数；显示和交易时段判断用 `America/New_York`。
- **三个时间**：每个行情事件区分市场发生时间 `event_time`、本机接收时间 `received_time`、策略最早可见时间 `available_time`。策略只能访问 `available_time` 不晚于当前引擎时间的数据。
- **Bar 时间戳**：`ts_start` 是区间起点，`ts_end = ts_start + 周期`。历史 bar 的 `available_time` 不早于 `ts_end`，可再加一个可配置的决策延迟。
- **价格**：行情用 float64；订单价格用 `Decimal`，提交前按该合约的实际报价规则校验和取整。不假定所有股票的最小报价单位都是 0.01 美元。
- **稀疏 bar**：某一秒没有合格成交就没有这一秒的 bar，两侧都不补空 bar。无成交造成的空档与下载缺失、断线、停牌造成的缺口分开记录。
- **标识**：`client_order_id` 由本系统生成，全局唯一，贯穿订单、成交、日志。

### 4.2 数据模型

```python
@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str              # 例如 "AAPL"
    exchange: str = "SMART"
    currency: str = "USD"
    min_tick: Decimal = Decimal("0.01")   # 默认值；实盘版从 IBKR 合约信息读取实际报价规则
    con_id: int | None = None   # IBKR 合约编号，实盘版解析后填入

@dataclass(frozen=True, slots=True)
class MarketMeta:            # 每个行情事件都携带
    source: str              # "massive" "ibkr" "synthetic"
    feed_kind: str           # "agg_1s" "trades" "tick_by_tick" "snapshot" "backfill"
    event_time: int
    received_time: int | None    # 历史数据为 None
    available_time: int
    sequence: int
    synthetic: bool = False
    quality_flags: frozenset[str] = frozenset()   # 例如 "degraded" "gap_before" "late_revision"

@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    timeframe: str           # "1s" "5s" "1m" "5m" "15m" "1h" "1d"
    ts_start: int
    open: float; high: float; low: float; close: float
    volume: float
    vwap: float | None = None
    trades: int | None = None
    session: str | None = None   # overnight / pre / regular / post；日线为空（决策 0003）
    closes_at: int | None = None # 不足一个周期的 bar 与日线的实际收盘时间；ts_end 优先取它

@dataclass(frozen=True, slots=True)
class Tick:                  # 逐笔成交
    symbol: str; ts: int; price: float; size: float
    conditions: tuple[str, ...] = ()   # 成交条件代码，用于聚合时过滤

@dataclass(frozen=True, slots=True)
class Quote:                 # 最优买卖价，可选
    symbol: str; ts: int
    bid: float; ask: float; bid_size: float; ask_size: float

@dataclass(frozen=True, slots=True)
class Reason:
    code: str                # 机器可统计，例如 "ema_cross_up" "stop_loss" "eod_flatten" "manual"
    text: str = ""           # 人可读说明
    context: dict[str, float] = field(default_factory=dict)  # 下单瞬间的指标快照

@dataclass(frozen=True, slots=True)
class OrderIntent:
    source: str              # 策略实例 id，或 "manual"
    symbol: str
    side: Literal["BUY", "SELL"]
    qty: int
    order_type: Literal["MKT", "LMT", "STP", "STP_LMT"]
    reason: Reason           # 必填
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    tif: Literal["DAY", "IOC"] = "DAY"
    outside_rth: bool = False
    take_profit: Decimal | None = None   # 两者任一非空即为括号单
    stop_loss: Decimal | None = None
    tags: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class Fill:                  # 成交事实，按 broker_exec_id 去重
    client_order_id: str
    ts: int; price: Decimal; qty: int
    broker_exec_id: str

@dataclass(frozen=True, slots=True)
class CommissionUpdate:      # 手续费可能晚于成交到达，单独成事件
    broker_exec_id: str
    commission: Decimal
```

`Order` 是可变的运行时对象，由事件归约得到（见第 8.1 节），包含 `OrderIntent`、`client_order_id`、券商侧标识（`account`、`client_id`、`broker_order_id`、`perm_id`）、`status`、`filled_qty`、`avg_fill_price`、`commission`（未到齐时标记为待定）和各状态时间戳。`Position` 记录 `qty`（负数为空头）、`avg_cost`、`realized_pnl`、`unrealized_pnl`，成本按均价法，与 IBKR 一致。

### 4.3 事件

| 事件 | 载荷 | 产生方 |
| --- | --- | --- |
| `BarEvent` | `Bar`、`MarketMeta`，以及 `closed: bool` | 行情源、聚合器 |
| `TickEvent` / `QuoteEvent` | `Tick` / `Quote`，以及 `MarketMeta` | 行情源 |
| `OrderEvent` | 券商原始状态与归约后的订单快照 | 订单管理、执行器 |
| `FillEvent` | `Fill` | 执行器 |
| `CommissionEvent` | `CommissionUpdate` | 执行器 |
| `TimerEvent` | 定时器名称与触发时间 | 时钟 |
| `SessionEvent` | 盘前开始、开盘、收盘、盘后结束 | 时钟与交易日历 |
| `SystemEvent` | 连接断开与恢复、行情中断或降级、风控触发、对账不一致 | 各模块 |

`closed=False` 的 `BarEvent` 是未收盘 bar 的中间更新，只发给界面，不发给策略。

### 4.4 时钟

```python
class Clock(Protocol):
    def now(self) -> int: ...                       # UTC 纳秒
    def set_timer(self, name: str, at: int) -> None: ...
```

`SimClock` 的时间由回放事件推进；`WallClock` 取系统时间。策略和核心模块只能通过 `Clock` 取时间，禁止调用 `datetime.now()` 或 `time.time()`，由测试扫描源码强制。

## 5. 模块一：数据层（`trader.data`、`trader.brokers.ibkr.feed`）

数据层向上只暴露两样东西：一个按时间推送事件的行情源 `DataFeed`，和一个按范围查询历史 bar 的 `HistoryService`。上层用同一套接口处理两种来源，来源和质量信息随事件一起传递。

### 5.1 本地存储

- 基础粒度是 1 秒 bar，路径 `data/bars/1s/symbol=AAPL/date=2026-01-23.parquet`，按 `ts_start` 升序。
- 另存 Massive 官方 1 分钟 bar，路径 `data/bars/1m/...`，只用作 1 分钟及以上周期的成交量、vwap、成交笔数来源（1 秒数据缺少只计成交量的成交，见决策 0003 第 5 条）。
- 可选 tick 数据，路径 `data/ticks/symbol=AAPL/date=....parquet`。
- 列：`ts_start`（UTC 纳秒）、`open`、`high`、`low`、`close`、`volume`、`vwap`、`trades`。
- 存原始未复权价格；另存 `data/corporate_actions.parquet`（拆股、分红），跨日指标按需复权。
- 更高周期不落盘，由聚合器现算，按 `(symbol, timeframe, date)` 做磁盘缓存。
- `data/catalog.sqlite` 记录每个分区的来源、时间戳单位与含义、复权方式、覆盖时段、成交条件过滤规则、行数、缺口清单、导入时间和内容指纹。
- 每次回测记录所用分区的指纹。分区被重新导入后，引用旧指纹的回测在界面上标记为“数据已更新，结果不可直接复现”。第一版不保留旧版本的数据文件。

### 5.2 数据下载与导入

（按决策记录 `docs/decisions/0001-massive-download.md` 修订。）

数据直接从 Massive REST 接口按天下载 1 秒聚合 bar，取**未复权**价格：

```text
trader data download --symbols SPY,QQQ --start 2024-01-01 [--end ...]   # 下载指定区间，已有日期跳过
trader data update [--symbols ...]                                       # 已有标的更新到最新交易日
trader data actions [--symbols ...]                                      # 刷新拆股与分红表
trader data report [--symbols ...]                                       # 覆盖范围与校验报告
```

- 增量：往前补一段、更新到今天、新增标的都是“对指定标的下载指定区间”，catalog 里已登记的日期跳过。没有任何 bar 的交易日（例如上市前）也登记，不重复请求。当天要等盘后结束 30 分钟后才下载。
- Massive 的时间戳 `t` 是 UTC 毫秒、bar 区间起点，存为 `ts_start`（UTC 纳秒）。成交量可能带小数（碎股）。
- 下载时逐日校验并写入报告：重复时间戳、时间倒序、非正价格、负成交量为**错误**，该日不写入、下次重试；`high`/`low` 与开收盘不自洽、vwap 落在当秒最高最低价之外（Massive 的 vwap 计入了不更新 OHLC 的成交类型，属正常现象）、时段外的 bar、常规时段内超过阈值（默认 60 秒）的空档为**提示**，照常写入并记入 catalog。
- 每天先写临时文件，校验通过后原子替换正式分区并更新 catalog；中途失败不留下半成品。重新下载同一天会替换该分区并产生新指纹。
- 研究版的“数据管理”界面（阶段 1 为 `download_gui.py`，阶段 4 并入网页）调用同一套下载代码。
- 逐笔成交文件的导入（由导入器聚合成 1 秒 bar，按成交条件过滤，与 Massive 官方聚合对齐）推迟到确实需要 tick 数据时再做；阶段 2 的聚合一致性测试只需一个样例日。

### 5.3 行情源接口

```python
class DataFeed(Protocol):
    def subscribe(self, symbols: list[str], kinds: set[Literal["bar_1s", "tick", "quote"]]) -> None: ...
    def unsubscribe(self, symbols: list[str]) -> None: ...

class BacktestFeed(DataFeed, Protocol):
    def events(self, start: int, end: int) -> Iterator[MarketEvent]: ...    # 同步，按 ts 归并多标的

class RealtimeFeed(DataFeed, Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def stream(self) -> AsyncIterator[MarketEvent]: ...
```

两类行情源输出的事件类型完全相同。策略订阅的任何周期都由引擎内的聚合器从 1 秒 bar 合成，行情源只负责 1 秒 bar 和可选的 tick。

### 5.4 历史回放行情源

- **全速模式**（backtest）：按天惰性加载 Parquet，多标的用堆归并，按可用时间输出。
- **定速模式**（replay）：实现 `RealtimeFeed`，支持 1 倍到 N 倍速、暂停、单步（下一根 bar）、跳转到指定时间。
- **秒内动画**：为了让回放时最后一根 K 线有形成过程，可以把一根 1 秒 bar 展开成几个合成价格点。合成点带 `synthetic=True`，只发给界面，不进入策略、指标、撮合和成交量统计。从 OHLC 无法得知秒内价格的先后顺序，任何合成路径都只是展示效果。
- 有真实 tick 数据的日期，界面动画和撮合都可以使用真实 tick。真实 tick 能确定成交的先后，但仍不代表买卖报价、排队位置和实际可成交数量。

回放跳转时交易状态的处理（第一版规则）：

- **向前跳转**：引擎全速处理中间的全部事件直到目标时间，挂单、策略和持仓按正常规则演变，仍属于同一次练习。
- **向后跳转**：开启一个新的练习分支。引擎在目标时间点重新构建状态：空仓、没有挂单、策略实例重新启动且 `ctx.state` 为空、指标按预热需求重新计算。原来那次练习的订单、成交和统计保留在原分支里，可以查看，但不带入新分支。
- 因此不会出现图表回到买入之前、账户却仍持有后来买入的股票的情况。界面上显示当前所在的分支，并在向后跳转前提示这会开始新的练习。

### 5.5 IBKR 实时行情源

用 ib\_async 连接本机的 IB Gateway 或 TWS。IBKR 的逐笔成交订阅和普通流式行情是两种不同的服务，后者是定时快照，不是完整的成交流，两者生成的 bar 在最高最低价、成交量和成交次数上可能不同。因此行情源必须声明自己的能力，不做静默降级。

```python
@dataclass(frozen=True)
class FeedCapabilities:
    trades_complete: bool          # 是否完整逐笔成交
    quotes_available: bool
    volume_semantics: Literal["per_trade", "cumulative_snapshot", "aggregated"]
    timestamp_precision_ns: int
    min_bar_interval: str          # 能可靠生成的最小 bar 周期
```

- 每个标的的订阅方式在配置中显式指定（逐笔或快照），启动时核对逐笔订阅数量是否超出账户限制，超出则报错，不自动改用快照。
- 策略声明所需的行情能力（见第 7.1 节）。标的的实际能力不满足时，策略拒绝启动；运行中发生降级则暂停该策略。策略显式声明允许降级的，降级时段写入运行报告。
- 快照行情不能把每次最新价更新当作一笔独立成交来累加成交量；成交量取累计量的差值。由快照生成的 bar 带 `degraded` 标记。
- 所有 tick 进入与回放相同的 `BarBuilder` 生成 1 秒 bar，封口规则见第 6.1 节。没有成交的秒不出 bar。
- **启动与重连的固定顺序**：订阅实时数据并缓冲 → 请求历史数据补缺口 → 确定历史与实时的切换边界 → 去重合并 → 聚合并预热指标 → 对账 → 才允许策略启动。IBKR 对小周期历史数据有时长和频率限制，请求要分段并限速。
- 补不上的时间段记为缺口，发 `SystemEvent`，受影响的策略保持暂停。
- **行情中断看门狗**：交易时段内某标的超过 N 秒没有任何更新，发 `SystemEvent` 报警。
- **实测规则（2026-10-06 录制对比，见 `docs/reports/2026-10-06_ibkr_vs_massive.txt`）**：IBKR 逐笔成交中标记为 `unreported` 的成交（主要是 FINRA 场外碎股）**不更新开高低收，但计入成交量**（与 SIP 官方的碎股规则一致；完整规则表见 `config/trade_conditions.json`，盘前盘后的 Form T 成交按 Massive 的做法更新价格，见决策 0003），这样生成的 bar 与 Massive 最一致（1 分钟收盘价平均偏差 0.1～0.2 美分，最高最低价约八成完全相同）；若全部成交都更新价格，偏差增大到 1～2 美分。快照行情的 1 分钟收盘价偏差为 0.2～16 美分（价格越高、波动越大越差），最高最低价只有约三到六成相同，常常低估极值；累计成交量差值与 Massive 基本一致。录制延迟约 0.3～1 秒。
- 实时收到的 tick 和生成的 1 秒 bar 落盘到 `data/live/`，用于事后与 Massive 数据对比和一致性测试。

### 5.6 历史查询服务

```python
class HistoryService:
    def bars(self, symbol: str, timeframe: str, start: int, end: int,
             session: Literal["rth", "extended"] = "rth", adjusted: bool = False) -> pl.DataFrame: ...
    def coverage(self, symbol: str) -> list[date]: ...
```

图表、notebook 和指标预热都通过这个接口取历史数据。它可以查询任意日期范围，因此不直接暴露给策略：策略上下文的 `history` 另行实现，只返回当前引擎时间之前已可用的数据。回放模式下，界面的查询同样要按回放时钟截断（见第 11.4 节）。

### 5.7 两个数据源不一致的处理

Massive 是全市场合并数据，IBKR 的数据取决于订阅的行情包和订阅方式，同一秒的 bar 可能有出入。系统不试图抹平这个差异，而是提供对比工具：`trader data compare --date ... --symbol ...` 输出两边 1 秒和 1 分钟 bar 的 OHLCV 偏差统计。

这项对比要尽早做：阶段 1 完成后，用一个独立的只读录制脚本录一个交易日的 IBKR 行情（清单内的标的），与同一天的 Massive 数据对比。如果偏差大到影响策略，就要在接入实盘之前调整策略使用的 bar 周期或行情订阅方式。

## 6. 模块二：Bar 聚合与指标（`trader.core.aggregation`、`trader.indicators`）

聚合器和指标都以增量方式计算，同一个实现同时服务回测、实时和图表，保证三处数值一致。

### 6.1 Bar 聚合器

- 输入 1 秒 bar（或 tick），输出任意订阅周期的 bar。支持的周期：1s、5s、10s、30s、1m、2m、5m、15m、30m、1h、1d。
- 对齐方式：日内周期从每个时段的开始时刻起按周期切分，bar 不跨时段，时段末尾不足一个周期的 bar 在时段结束时收盘（30 分钟及以下等同自然时间对齐；1 小时 bar 在常规时段从 09:30 起算）；日线每个交易日一根。可配置只统计常规时段（rth）或全部时段（extended）。见决策 0003。
- **封口由时钟触发**，而不是由下一笔行情触发，否则成交稀疏的股票会延迟收盘。
- **实时封口留宽限**：周期结束后再等一个可配置的迟到容忍窗口（默认 300 毫秒）才封口并发给策略，因为那一秒的行情可能还在路上。回测中 bar 的可用时间加上同样的延迟，两边规则一致。
- **交付时间决定订单生效时间**：策略在 bar 交付时才能作决策，它发出的订单生效时间不早于交付时间。这时下一个市场区间已经开始，所以该订单不能用下一个区间的开盘价成交，只能参与生效之后才开始的完整区间（见第 9.2 节）。
- **迟到数据**：封口之后才到达的 tick 不修改已发给策略的 bar，也不改变已经作出的决策；它被记为修订，带 `late_revision` 标记。策略当时看到的 bar 和事后修订的展示用 bar 分开保存，都可以查询。
- 周期内没有任何成交则不输出该 bar。
- 周期内每来一根 1 秒 bar，输出一次 `closed=False` 的中间状态，仅供界面显示。

### 6.2 指标接口

```python
class Indicator(ABC):
    name: ClassVar[str]                      # 注册名，例如 "ema"
    Params: ClassVar[type[BaseModel]]        # 参数模型，前端据此生成参数表单
    outputs: ClassVar[list[OutputSpec]]      # 每条输出序列的名称与绘图方式
    scope: ClassVar[Literal["continuous", "session"]] = "continuous"

    def __init__(self, params: BaseModel): ...
    def warmup(self) -> WarmupSpec: ...      # 输出有效值前需要哪些历史数据，可依赖参数
    @abstractmethod
    def update(self, bar: Bar) -> dict[str, float | None]: ...   # 每根收盘 bar 调用一次
    def on_session_open(self, day: TradingDay) -> None: ...   # 仅 session 指标：每个交易日开始时调用，day 提供各时段起止

@dataclass(frozen=True)
class WarmupSpec:
    bars: int = 0                 # 固定根数，例如 EMA(21) 需要若干倍周期的 bar
    from_session_start: bool = False   # 需要当前交易日从开盘起的全部 bar，例如日内 VWAP、开盘区间
    prev_sessions: int = 0        # 需要之前 N 个完整交易日，例如前一日高低收

@dataclass(frozen=True)
class OutputSpec:
    key: str                                 # 例如 "value" "upper" "signal"
    plot: Literal["line", "histogram", "band", "marker", "hline"]
    pane: Literal["price", "separate"]      # 叠加在主图，或单独窗格
    color: str | None = None
```

- `update` 是唯一的计算入口。批量计算就是对历史 bar 逐根调用 `update`，不另写一套向量化实现。
- 指标只能依赖传入的 bar 和自身状态，不能访问未来数据、时钟或外部资源。
- **连续指标**（`continuous`，如 EMA、RSI、ATR）跨交易日延续，换日时不清空状态；预热数据来自之前的交易日。
- **会话指标**（`session`，如按日锚定的 VWAP、开盘区间高低点）在每个交易日开盘时由框架调用 `on_session_open` 重新开始。
- 指标实例没有通用的 `reset`。需要从头计算时（回放跳转、重启），框架丢弃旧实例，新建实例并重新预热。
- 指标可以依赖其他指标（如 MACD 依赖 EMA），在构造函数中组合。

### 6.3 注册与自定义指标

- 用 `@register_indicator` 装饰器注册。启动时自动扫描 `trader/indicators/builtin/` 和 `user/indicators/`。
- 用户新增指标的全部工作：在 `user/indicators/` 下放一个文件，实现上面的类。保存后研究版自动热加载；实盘版不热加载。
- 前端通过 `/api/indicators` 取得指标列表、参数模型和 `OutputSpec`，据此生成参数表单并绘图。只要指标使用 `OutputSpec` 已有的五种绘图方式，就不需要改前端。
- `marker` 类输出用于在图上标注信号点（箭头、文字）。
- 五种方式之外的特殊图形、标注和交互，需要在前端新增绘图类型，属于单独的开发任务。

### 6.4 内置指标（第一版）

SMA、EMA、VWAP（按交易日锚定）、布林带、RSI、MACD、ATR、成交量均线、开盘区间高低点、前一日高低收。

### 6.5 指标计算服务

指标实例分两类，互不共用：

- **活动实例**：引擎为每个 `(symbol, timeframe, name, params)` 组合维护一个，只有引擎在处理收盘 bar 时更新它。策略读取它的值；图表订阅它的输出流，只读。
- **历史计算**：`IndicatorService.compute(symbol, timeframe, name, params, start, end)` 每次调用都新建实例，返回对齐到 bar 时间戳的 DataFrame，供图表加载历史和 notebook 使用。它不读也不写活动实例。
- **预热按指标声明的需求取数**：两类实例都按 `WarmupSpec` 决定从哪里开始喂数据，取三项要求中最早的起点，即固定根数、当前交易日开盘、之前 N 个完整交易日。盘中启动策略或从盘中某一时刻开始看图时，日内 VWAP 仍从当天开盘算起，前一日高低收仍取自上一个完整交易日。这样活动实例和历史计算在同一时刻给出相同的数值。

因此在图表上拖动历史、修改指标参数或添加指标，都不会影响正在运行的策略。

## 7. 模块三：策略框架（`trader.strategy`）

策略是放在 `user/strategies/` 下的 Python 类，只通过上下文对象 `ctx` 与系统交互，因此同一份代码可以不加修改地运行在所有运行组合下。

### 7.1 策略接口

```python
class Strategy(ABC):
    name: ClassVar[str]
    Params: ClassVar[type[BaseModel]]          # 参数模型，带默认值和取值范围
    requires: ClassVar[FeedRequirements] = FeedRequirements()   # 所需行情能力，见第 5.5 节

    def __init__(self, params: BaseModel, ctx: StrategyContext): ...

    def on_start(self) -> None: ...            # 订阅行情、声明指标、读取 ctx.state
    def on_session_open(self) -> None: ...
    @abstractmethod
    def on_bar(self, bar: Bar) -> None: ...    # 订阅周期的 bar 收盘时调用
    def on_tick(self, tick: Tick) -> None: ... # 可选，订阅 tick 时才调用
    def on_order(self, order: Order) -> None: ...
    def on_fill(self, fill: Fill) -> None: ...
    def on_timer(self, name: str) -> None: ...
    def on_session_close(self) -> None: ...
    def on_stop(self) -> None: ...

@dataclass(frozen=True)
class FeedRequirements:
    trades_complete: bool = False      # 是否必须完整逐笔成交
    quotes: bool = False
    min_bar_interval: str = "1s"       # 策略用到的最小 bar 周期
    allow_degraded: bool = False       # 是否允许在降级行情下继续运行
```

### 7.2 上下文接口

```python
class StrategyContext(Protocol):
    # 行情与指标
    def subscribe(self, symbol: str, timeframe: str) -> None: ...
    def indicator(self, name: str, symbol: str, timeframe: str, **params) -> IndicatorHandle: ...
    def history(self, symbol: str, timeframe: str, n: int) -> list[Bar]: ...   # 只含当前时刻已可用的收盘 bar

    # 下单（reason 必填）
    def buy(self, symbol: str, qty: int, *, reason: Reason, order_type="MKT",
            limit_price=None, stop_price=None, take_profit=None, stop_loss=None) -> str: ...
    def sell(self, symbol: str, qty: int, *, reason: Reason, **kw) -> str: ...
    def modify(self, client_order_id: str, *, reason: Reason,
               qty=None, limit_price=None, stop_price=None) -> None: ...      # 例如移动止损
    def cancel(self, client_order_id: str, *, reason: Reason) -> None: ...
    def close_position(self, symbol: str, *, reason: Reason) -> str | None: ...

    # 状态
    def position(self, symbol: str) -> Position: ...
    def open_orders(self, symbol: str | None = None) -> list[Order]: ...
    def now(self) -> int: ...
    def set_timer(self, name: str, at: int) -> None: ...
    state: StrategyState     # 由框架持久化的键值存储，见第 7.3 节

    # 记录
    def log(self, msg: str, **fields) -> None: ...
    def mark(self, symbol: str, text: str, kind: str = "note") -> None: ...    # 在图上打标记，不下单
```

`IndicatorHandle.value` 返回最新值，`IndicatorHandle[-1]` 返回上一根 bar 的值；预热未完成时为 `None`。

### 7.3 策略必须遵守的约束

- 不直接取系统时间，不做文件和网络读写，不导入 `trader.brokers`。
- 不判断当前运行模式；`ctx` 不提供查询运行模式的方法。
- 每次下单、改单和撤单都要给出 `Reason`：`code` 用稳定的短标识，`context` 放下单瞬间的关键指标值。
- **需要跨重启保留的状态必须放在 `ctx.state`。** 例如今天是否已做过一次突破交易、亏损后的冷却截止时间、跟踪止损记录的最高价、某个信号是否已提交过订单。这些无法从指标和持仓恢复，放在普通成员变量里会在重启后丢失并导致重复交易。
- `ctx.state` 只接受可序列化为 JSON 的值。框架在每次回调结束后把变更与该回调产生的订单请求放在同一个事务里落盘，然后才发送订单（见第 8.2 节）；重启时先恢复 `ctx.state`，再调用 `on_start`。回测中它只在内存里，按交易日是否清空由策略在 `on_session_open` 中决定。
- 指标值、持仓、挂单不要存进 `ctx.state`，它们由框架在预热和对账后提供。
- `ctx.position` 和 `ctx.open_orders` 返回本策略实例名下的持仓和订单（归属规则见第 7.4 节）。

这些约束由一个静态检查测试强制：扫描 `user/strategies/`，发现禁用的导入或调用即失败。

### 7.4 加载与实例

- 启动时自动发现 `user/strategies/` 下的策略类；研究版支持保存后热加载，实盘版不支持。
- 一个策略类可以用不同参数和标的启动多个实例，每个实例有唯一的 `strategy_id`。
- **标的归属（第一版规则）**：同一账户内，一个标的在同一时间只能有一个所有者，所有者是某个策略实例或“手动”。该标的的全部持仓和挂单都记在所有者名下，因此策略看到的持仓等于账户在该标的上的净持仓，对账时不存在归属歧义。
- **占用**：策略启动时声明并占用它要交易的标的；标的已有所有者时启动失败。
- **人工接管**：随时可以进行，不要求先清仓。在界面上确认后，该策略立即暂停，标的的所有者改为“手动”；现有持仓、挂单和保护单原样转到“手动”名下。保护单保持在券商端有效，不会因为接管而被撤销；之后由人决定改单、撤单或平仓。直接对策略名下的标的手动下单，等同于先发起接管，界面要求确认。
- **释放**：只有当该标的持仓为零且没有任何未终结订单时，才能释放归属。释放后标的没有所有者，可以被策略占用。
- 被接管的策略不能原地恢复。要让策略重新交易这个标的，必须先释放，再重新启动策略实例；它从空仓开始，`ctx.state` 中与该标的相关的状态由策略在 `on_start` 中自行处理。
- 标的不能在两个策略之间直接转移，只能经过释放。
- 实盘版只加载配置文件白名单中的策略，并记录其源码哈希。
- **半自动与全自动**：实盘版中每个策略实例有执行方式的设置，默认半自动（见第 8.6 节）；回测、回放和 local\_paper 中策略订单直接执行。这个区别由订单管理模块处理，策略代码感知不到。

### 7.5 示例策略

随框架交付两个示例，同时作为测试用例和写法模板：

1. **均线交叉**：1 分钟 EMA 快慢线交叉入场，反向交叉或收盘前平仓。
2. **开盘区间突破**：开盘后前 N 分钟的高低点，突破入场，括号单带止盈止损，收盘前平仓。

```python
class EmaCross(Strategy):
    name = "ema_cross"

    class Params(BaseModel):
        symbol: str = "AAPL"
        fast: int = 9
        slow: int = 21
        qty: int = 100

    def on_start(self):
        p = self.params
        self.ctx.subscribe(p.symbol, "1m")
        self.fast = self.ctx.indicator("ema", p.symbol, "1m", period=p.fast)
        self.slow = self.ctx.indicator("ema", p.symbol, "1m", period=p.slow)

    def on_bar(self, bar):
        if self.slow.value is None or self.slow[-1] is None:
            return
        p, pos = self.params, self.ctx.position(self.params.symbol)
        crossed_up = self.fast[-1] <= self.slow[-1] and self.fast.value > self.slow.value
        crossed_down = self.fast[-1] >= self.slow[-1] and self.fast.value < self.slow.value
        snapshot = {"fast": self.fast.value, "slow": self.slow.value, "close": bar.close}
        if crossed_up and pos.qty == 0:
            self.ctx.buy(p.symbol, p.qty, reason=Reason("ema_cross_up", context=snapshot))
        elif crossed_down and pos.qty > 0:
            self.ctx.close_position(p.symbol, reason=Reason("ema_cross_down", context=snapshot))
```

收盘前强制平仓由风控模块统一处理（见第 8 节），策略不需要各自实现。

## 8. 模块四：订单管理与风控（`trader.oms`）

所有订单，无论来自策略还是手动，都经过同一个入口 `OrderManager.submit(intent)`，先过风控，再交给执行器。

### 8.1 订单状态与事件归约

订单状态不是由一条线性状态机驱动，而是由一个可重复执行的归约函数从事件流算出：`reduce(order, event) -> order`。执行器适配层保留券商的原始状态，归约函数负责把它们变成下表的状态。

| 状态 | 含义 |
| --- | --- |
| `NEW` | 已创建，尚未过风控 |
| `AWAITING_CONFIRM` | 实盘半自动方式下等待人工确认（见第 8.6 节） |
| `SUBMITTED` | 请求已记录并发给执行器，未确认 |
| `UNKNOWN` | 发送结果不明（超时或发送时断线），等待查询确认 |
| `ACCEPTED` | 执行器确认挂单 |
| `PARTIALLY_FILLED` | 部分成交 |
| `PENDING_CANCEL` / `PENDING_REPLACE` | 撤单或改单请求已发出，尚未确认；期间仍可能成交 |
| `FILLED` / `CANCELLED` / `REJECTED` / `EXPIRED` | 终态 |

归约规则：

- **幂等**：同一事件重复到达，结果不变。券商的状态通知可能重复，成交按 `broker_exec_id` 去重。
- **成交以成交回报为准**，不依赖状态通知。可以没经过 `ACCEPTED` 就收到成交；可以跳过中间状态。
- **撤单途中成交**：`PENDING_CANCEL` 期间到达的成交照常入账。已进入 `CANCELLED` 之后才送达、但发生在撤单之前的成交，仍然更新成交账本和持仓，并记一条 `SystemEvent`。
- **手续费晚到**：`CommissionUpdate` 单独处理。手续费未到齐时订单和交易的净盈亏标记为未结算。
- **发送结果不明**：发送前先按第 8.2 节的顺序把订单请求落盘。请求超时进入 `UNKNOWN`，随后向券商查询挂单和成交；确认券商没有收到才允许重发，禁止直接再次下单。
- **成交更正或撤销**：保留原记录和更正记录，按版本留痕，不直接覆盖。

无法识别或不合逻辑的事件不能让引擎抛错停摆：记录原文、发 `SystemEvent`、把该订单所属的标的置为待人工处理，其余标的继续运行。上述券商边界情况的完整实现属于 IBKR 接入阶段；模拟撮合只会产生其中的常规路径，但两者使用同一个归约函数。

### 8.2 订单管理职责

- 生成 `client_order_id`，维护订单表，记录券商侧标识的映射。
- 括号单是统一的订单意图，由各执行器按自己的机制实现（见第 9 节）。约束对两者相同：保护单只覆盖入场单已成交的数量；任何退出单都不得因为另一张退出单或手动平仓已经成交而超量卖出、形成反向持仓；入场单撤销时未生效的子单一并撤销。
- 根据成交更新持仓和盈亏（`Portfolio`），按标的所有者和按账户各记一份。
- 维护标的归属表（见第 7.4 节）。
- **落盘顺序（实盘版）**：券商回报等输入事件到达时先落盘，并带一个“策略尚未处理”的标记，然后才回调策略。回调产生的订单请求、订单状态变更、标的归属变更、`ctx.state` 变更，连同把该输入事件标记为“已处理”，在同一个 SQLite 事务里提交（WAL 模式，提交时同步刷盘）。提交成功后才把订单发给执行器。
- **恢复时补处理**：重启后，已落盘但未标记为已处理的输入事件按原顺序重新交给策略，每个事件只处理一次。回调产生的订单，其 `client_order_id` 由输入事件标识和序号确定性地生成，因此重复处理不会产生第二张订单。已提交但尚未发送的订单请求进入 `UNKNOWN` 并走查询流程。
- **恢复依据**：SQLite 是崩溃恢复的唯一依据。JSONL 事件日志在事务提交之后追加，用于审计和录制重放；它可以滞后，缺失的尾部由 SQLite 补写，恢复时不依赖它。
- 研究版的回测和回放不做上述持久化，结果在运行结束时一次写出。以上三条属于阶段 6 的实现，之前的阶段不需要为它搭建框架。
- 第一版支持的订单类型：市价、限价、止损、止损限价、括号单；有效期 DAY 和 IOC；支持改单。

### 8.3 交易前风控

风控规则是纯函数 `check(intent, state, limits) -> Accept | Reject(rule, detail)`，所有运行组合共用同一份实现，只是阈值来自各自的配置。

| 规则 | 说明 |
| --- | --- |
| 标的白名单 | 只允许配置中列出的标的（第一版为固定清单） |
| 标的归属 | 订单来源必须是该标的当前的所有者 |
| 单笔上限 | 最大股数、最大金额 |
| 单标的持仓上限 | 成交后持仓股数与金额不超限 |
| 总敞口上限 | 全部持仓市值绝对值之和 |
| 日内亏损上限 | 当日已实现加浮动亏损触及阈值后，只允许减仓单 |
| 下单频率 | 每分钟订单数上限，防止策略失控循环下单 |
| 价格保护 | 限价偏离最新价超过 x% 则拒绝 |
| 做空开关 | 配置不允许做空时，拒绝会导致净空头的卖单 |
| 交易时间窗 | 开盘后 N 分钟内、收盘前 N 分钟内禁止新开仓；开收盘时间取自交易日历 |
| 重复订单保护 | 同一来源短时间内完全相同的订单视为重复 |
| 行情新鲜度与质量 | 该标的行情超过 N 秒未更新，或处于缺口、未授权的降级状态时，拒绝新开仓 |
| 连接状态 | 与券商断线或对账未通过时，拒绝新开仓 |

两条适用于全部规则的约定：

- **在途订单计入敞口**：数量和敞口类规则（单标的持仓上限、总敞口上限、做空开关）按最坏情况计算，即当前持仓加上所有未终结订单和等待确认订单中会增加敞口的未成交数量。状态为 `UNKNOWN` 的订单按全部会成交计算。
- **发送前重新检查**：风控在订单创建时执行一次，在实际发送给执行器之前再完整执行一次。第二次检查覆盖半自动确认之后、以及 `UNKNOWN` 订单查询后获准重发之前的情形，用的是发送时刻的持仓、行情和连接状态。
- **额度按订单标识管理，不重复累计**：订单通过第一次检查时，以 `client_order_id` 为键预留它占用的额度。复检同一张订单时，先替换它原有的预留再计算，不把它自己再加一遍。订单终结时释放未用的预留。
- **计数类规则每张订单只计一次**：下单频率和重复订单保护在订单创建时计数。同一张订单后续的确认和发送前复检不再计数，也不会被判为自己的重复。
- **退出不受开仓限制阻断**：减仓单、撤单、止损、收盘前平仓和紧急平仓，不受单笔上限、持仓上限、总敞口、日内亏损上限、下单频率、交易时间窗和行情新鲜度的限制。它们只检查标的归属、数量不超过可平持仓，以及连接状态。

被拒绝的订单进入 `REJECTED`，拒绝规则写入订单记录，并回调策略的 `on_order`。

### 8.4 盘中风控动作

停止策略、停止新开仓、撤销全部挂单、平仓是四个不同的动作，界面和接口上分开提供，不合并成一个含义模糊的按钮。

- **收盘前强制平仓**：在当日收盘前 N 分钟（默认 5 分钟）触发，收盘时间取自交易日历，半日市自动提前。步骤固定：停止新开仓 → 撤销全部挂单 → 等待撤单确认或成交结果 → 按确认后的实际持仓发市价平仓单，原因代码 `eod_flatten`。不等撤单确认就平仓，括号单的子单可能同时成交，把持仓打成反向。回测中同样生效。
- **紧急停止**：一键停止全部策略并停止新开仓；是否同时撤单、是否平仓由操作者在确认框中选择。平仓同样遵循“先撤单并确认，再按实际持仓平仓”。界面上常驻，命令行也可触发。
- **亏损熔断**：触及日内亏损上限时自动停止策略、停止新开仓并报警。
- **断线时**：停止新开仓，保留已经在券商端生效的保护单，不尝试撤销它们。撤单或平仓结果无法确认时，界面显示“状态未确认”，不显示“已安全”。连接和状态恢复明确之后，才执行有控制的减仓。

### 8.5 对账（broker\_paper 与 live）

- 连接建立时和之后每隔固定时间，把本地持仓和挂单与 IBKR 返回的结果比对。
- 发现不一致：发 `SystemEvent`、停止相关策略、在界面上显示差异，等待人工确认。系统不自动修正本地状态，也不自动下单对冲。
- 在 TWS 或手机上的人工操作会造成不一致，这是预期行为；界面提供“以券商为准重新同步”的按钮，需要人工点击。

### 8.6 实盘半自动确认

实盘版中策略默认以半自动方式执行：策略照常发出订单意图，订单通过风控后进入 `AWAITING_CONFIRM`，由人在界面上确认后才发给券商。

- 界面弹出信号卡片：标的、方向、数量、订单类型与价格、止盈止损、预估金额、原因代码与指标快照、信号产生时间。操作只有确认和拒绝。
- **超时**：超过配置的时限（默认 15 秒）未确认，订单以 `EXPIRED` 结束，原因记为 `confirm_timeout`，并回调策略的 `on_order`。
- **确认后重新过全部风控**：确认时按第 8.3 节重新执行全部规则，不只是价格。任何一条不通过，订单以 `REJECTED` 结束，不发出。
- **价格偏离保护**：作为其中一条规则，确认时最新价相对信号产生时的价格偏离超过阈值，订单作废。
- **确认请求幂等**：每个待确认信号有唯一标识，状态只能从等待转到已确认、已拒绝或已超时之一，转移在后端的一个事务内完成。重复点击、网络重试、多个浏览器窗口同时确认，最多只发出一张订单；对已经结束的信号再次确认，返回它当前的状态，不发单。
- **只有开仓需要确认**。括号单的止盈止损随入场单一起确认。减仓和平仓类订单（策略的出场信号、止损、收盘前平仓）默认直接执行，避免因为等待确认而扩大亏损；这一项可配置。
- 确认、拒绝、超时都写入事件日志，并在运行报告中统计：信号数、确认数、拒绝数、超时数，以及从信号到确认的延迟分布。这些数据用于判断策略是否可以放开全自动。
- 放开全自动按策略实例逐个进行：在 `live` 配置中显式把该策略的执行方式改为 `auto`。界面上不提供切换开关。

## 9. 模块五：执行层（`trader.brokers`）

执行器只有两个实现：模拟撮合 `SimBroker` 和 `IBKRBroker`。两者实现同一个接口，返回同样的订单事件和成交事件。

### 9.1 执行器接口

```python
class Broker(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    def submit(self, order: Order) -> None: ...          # 结果通过事件返回
    def cancel(self, client_order_id: str) -> None: ...
    def modify(self, client_order_id: str, *, qty=None, limit_price=None, stop_price=None) -> None: ...
    def positions(self) -> dict[str, Position]: ...
    def open_orders(self) -> list[Order]: ...
    def account(self) -> AccountSnapshot: ...
    def on_market_event(self, event: MarketEvent) -> None: ...   # 仅 SimBroker 使用
```

执行器产生的 `OrderEvent` 和 `FillEvent` 进入引擎的同一个事件队列。回测中 `SimBroker` 的方法是同步执行的，接口上的 `async` 只用于连接管理。

### 9.2 模拟撮合规则

规则必须确定、保守、可配置。每条规则都写明订单何时生效、成交价依据什么、成交数量依据什么。默认值如下，全部写入回测配置并随结果保存。只有秒级 OHLCV 时，这些规则是近似，报告中要注明。

| 项目 | 默认规则 |
| --- | --- |
| 生效时间 | 订单生效时间等于策略收到触发事件的时间（对 bar 是区间结束加封口宽限）再加可配置的决策延迟。订单只参与起点不早于生效时间的完整市场区间。秒级数据下，策略在 \[10:00:00, 10:00:01) 这根 bar 上发出的订单约在 10:00:01.3 生效，最早在 \[10:00:02, 10:00:03) 这个区间成交 |
| 市价单 | 在生效后第一个有成交的完整区间上一次性全部成交，成交价为该区间开盘价加上不利方向的成本（半个价差加冲击项）。这是第一版的简化模型 |
| 市价单模型的适用范围 | 订单数量不超过生效前 60 秒成交量的 `market_max_participation`（默认 2%）。超出的订单仍按上面的规则成交，但标记为超出适用范围，计入报告的可靠性提示 |
| 半个价差 | 有报价数据时取当时买卖价差的一半；没有时用清单配置里每个标的的半价差基点数 |
| 冲击项 | `冲击基点 = k × 订单数量 ÷ 生效前 60 秒成交量`，`k` 可配置。成交量只用订单生效时刻之前已经可用的数据。该成交量为零或低于配置的下限时，按下限计算，并把订单标记为超出适用范围 |
| 限价买单 | 生效后的区间开盘价严格低于限价：按开盘价成交。否则最低价严格低于限价：按限价成交。最低价只是触及限价不算成交。成交价不会差于限价 |
| 限价卖单 | 与限价买单对称 |
| 限价单成交数量 | 每个区间的可成交量为该区间成交量乘以参与率（默认 10%）。这份预算由同一标的在该区间内的全部模拟订单共享，按订单生效先后分配；余量留到后续区间，形成部分成交 |
| 止损单 | 生效后的区间触及止损价即触发，按止损价与开盘价中更差者成交，再加市价单同样的成本。跳空时按不利价格成交 |
| 止损限价单 | 分两个阶段：先按止损单规则触发；触发后成为限价单，从下一个市场区间起按限价单规则撮合。触发不等于成交 |
| IOC | 在生效后第一个有成交的完整区间上撮合一次，未成交部分立即撤销。没有行情的时段不算一次执行机会 |
| 同一区间内止盈止损都可触及 | 按不利结果处理（先止损），并把这笔交易标记为路径歧义 |
| 括号单数量 | 保护单数量跟随入场单的累计成交量。一张退出单成交后，另一张按剩余持仓缩减或撤销，不会超量卖出 |
| 手续费 | 按股费用在每笔成交上累加；每单最低收费在订单结束时结算一次，不在每次部分成交时重复收取。默认每股 0.005 美元、每单最低 1 美元，以 IBKR 官网现行费率为准 |
| 交易时段 | 常规时段外默认不撮合。标记 `outside_rth` 的订单在盘前盘后只撮合限价单，其他类型一律拒绝；实盘中各类型是否可用以券商实际能力为准 |
| 做空 | 假定可借到券，不计借券费用；可配置为禁止 |

- 上表中“生效后的下一个完整区间”是 bar 撮合的保守近似，只适用于用 bar 撮合的情形。选用 tick 撮合时，订单从生效时间之后的第一笔合格成交起参与撮合，按真实成交的先后判断触发顺序；回测结果中注明所用的撮合数据。
- 需要人工确认的订单，生效时间从确认通过并实际发送的时刻起算，不早于这个时刻。
- 合成的秒内路径不参与撮合。
- **路径歧义统计**：每次回测报告路径歧义交易的笔数，以及把它们全部改按有利结果计算时的盈亏差额。差额相对总盈亏很大，说明策略依赖秒内顺序，需要取得成交和报价数据后再下结论。
- **成本敏感性**：每次回测默认输出多档成本下的结果（见第 10.4 节），不只显示默认假设下的一个数字。
- 撮合规则每一条都要有独立的单元测试，用手工构造的 bar 序列验证成交时间、价格和数量。

### 9.3 IBKR 执行器

- 通过 ib\_async 连接本机 IB Gateway（推荐）或 TWS。常用默认端口：TWS 实盘 7496、模拟 7497；Gateway 实盘 4001、模拟 4002。
- **订单映射**：记录 `account`、`client_id`、`broker_order_id`、`perm_id` 和 `orderRef`。`client_order_id` 写入 `orderRef` 用于重连和重启后的关联；它不是券商保证幂等的凭据，不能据此认为重复发送是安全的。
- **括号单**：用券商的父子订单机制一次提交，让保护单在券商端随入场单建立。不采用“本机收到父单成交回报后再创建止损单”的做法，否则断线或崩溃时持仓没有保护。父单部分成交时保护单如何覆盖，以模拟账户实测为准。
- **回报处理**：成交以执行回报为准，手续费以佣金回报为准，状态通知只作辅助；全部经过第 8.1 节的归约函数。无法识别的状态记录原文并报警。
- **发送超时**：订单进入 `UNKNOWN`，向券商查询挂单和当日成交后再决定是否重发。
- **重连**：断线后自动重连，重连成功后重新请求挂单、当日成交和持仓，交给对账逻辑。Gateway 每日自动重启期间属于预期断线。
- **限速**：遵守 IBKR 的消息频率限制，下单和撤单经过一个限速队列。
- **账户校验**：连接后读取账户号。broker\_paper 配置要求账户号以模拟账户前缀开头（通常为 `DU`）；live 配置要求与配置文件中的账户号完全一致。不符合则立即断开并退出。
- **系统之外的第二道防线**：本系统的风控和策略在同一个进程里，不算独立防线。实盘前在 TWS 或 Gateway 的 API 预防设置中配置单笔数量和金额上限，并尽量使用资金有限的独立账户或子账户。这两项由用户手工设置，列入上线清单。

### 9.4 两种模拟盘

`broker_paper` 用 IBKR 模拟账户执行，目的是验证接口、订单流、恢复和对账；它的成交模型比较理想化，不代表真实成交质量。`local_paper` 用 IBKR 实时行情加本地 `SimBroker`，目的是在实时行情下检验聚合结果和回测的撮合假设，不向券商发送任何订单。两者验证的东西不同，上线前都要跑。

## 10. 模块六：回测引擎与报告（`trader.backtest`、`trader.research`）

回测就是用全速回放行情源、模拟撮合和模拟时钟驱动同一个引擎，然后把过程中记录的订单和成交整理成报告。

### 10.1 回测配置

```yaml
name: SPY 均线交叉示例        # 实验名称
strategy: ema_cross
params: {symbol: SPY, timeframe: 1m, fast: 9, slow: 21, notional: 10000}
start: 2026-09-01
end: 2026-09-30
session: rth                 # rth 或 extended（行情与策略周期覆盖的时段）
trade_extended: false        # 是否允许盘前盘后开仓（只能用 outside_rth 限价单）
hold_post: false             # false：常规收盘前平仓；true：盘后 20:00 前平仓
initial_cash: 100000
sim:
  decision_delay_ms: 0       # 决策到订单生效的额外延迟
  bar_grace_ms: 300          # bar 封口宽限，与实时一致
  half_spread_bps: {default: 2, SPY: 0.5}   # 没有报价数据时使用
  impact_k: 10               # 冲击项系数
  impact_min_volume: 1000    # 冲击项使用的 60 秒成交量下限（股）
  market_max_participation: 0.02   # 市价单简化模型的适用上限
  limit_participation: 0.10  # 限价单参与率
  commission_per_share: 0.005
  commission_min: 1.0
  cost_scenarios: [1, 2, 3]  # 成本敏感性的倍数
risk: {max_order_usd: 20000, max_position_usd: 20000, max_daily_loss_usd: 2000, flatten_before_close_min: 5}
```

指标预热不需要配置：每个指标按自己声明的预热需求自动往前取数（决策 0004 第 6 条）。

### 10.2 运行器

- 入口有三个，最终调用同一个函数：界面上的回测表单、命令行 `trader backtest run <config.yaml>`、Python 接口 `run_backtest(config) -> BacktestResult`。
- 回测在独立的 worker 进程中运行，界面显示进度并可以取消。
- 按交易日逐日加载数据，内存占用与回测区间长度无关。
- 每个交易日收盘前按风控规则强制平仓，日内策略不留隔夜仓。
- **可复现**：每次运行记录配置、策略源码哈希、git 提交号和所用数据分区的指纹。相同输入的业务结果必须完全相同；比较时排除运行编号、生成时间等字段。
- **性能目标**：单标的单日 1 秒 bar 的回测在 2 秒内完成（普通笔记本）。

### 10.3 输出

每次回测写入 `runs/<run_id>/`，并在 `runs/index.sqlite` 登记：

| 文件 | 内容 |
| --- | --- |
| `config.json` | 完整配置与可复现信息 |
| `orders.parquet` | 全部订单，含被风控拒绝的，带原因代码、原因说明和指标快照 |
| `fills.parquet` | 全部成交，含价格、数量、手续费、滑点 |
| `trades.parquet` | 配对后的完整交易（一次开仓到平仓） |
| `equity.parquet` | 每分钟的权益、持仓市值、现金 |
| `marks.parquet` | 策略用 `ctx.mark` 打的图表标记 |
| `log.jsonl` | 策略日志与系统事件 |
| `summary.json` | 汇总统计 |

`trades.parquet` 每行一笔完整交易，字段：标的、方向、数量、入场时间与价格、入场原因、出场时间与价格、出场原因、毛盈亏、手续费、净盈亏、持仓时长、持仓期间最大浮盈（MFE）和最大浮亏（MAE）。

### 10.4 汇总统计

- 交易笔数、多头与空头笔数、订单数、被拒订单数、部分成交数。
- 净盈亏、毛盈亏，以及成本分项：手续费、价差成本、冲击成本。
- 胜率、平均盈利、平均亏损、盈亏比、期望值、利润因子。
- 最大回撤（金额与比例）、最长回撤持续时间、按日收益计算的夏普比率。
- 平均持仓时长、持仓时间占比。
- 分组统计：按入场原因、按出场原因、按交易日、按一天内的时间段（每 30 分钟）、按多空方向。
- **成本敏感性**：在同一组成交上，把价差和冲击成本按 `cost_scenarios` 的倍数重新计价，输出每档成本下的净盈亏、胜率和利润因子，并给出盈亏平衡成本（每股多少成本时净盈亏归零）。这是事后重算，假设策略决策不随成本变化。
- **路径歧义**：歧义交易笔数及其对盈亏的影响（见第 9.2 节）。
- **数据质量**：回测区间内的缺口、降级时段，以及落在这些时段内的交易笔数。
- **流动性提示**：超出市价单简化模型适用范围的订单笔数和占比。占比超过配置的阈值时，结果页顶部显示可靠性提示，说明该回测的成交假设对这些标的或订单规模不可靠。

### 10.5 参数扫描

- `trader backtest sweep <config.yaml> --grid fast=5,9,13 slow=21,34,55`，多进程并行，每组参数一个独立的回测结果。
- 输出一张参数对比表（每组参数的主要统计），可在界面和 notebook 中查看。
- 支持把日期区间切成样本内和样本外两段，扫描只在样本内进行，报告同时给出两段的结果，用于识别过拟合。

### 10.6 研究接口（notebook）

```python
from trader.research import load_bars, compute_indicator, run_backtest, load_run, sweep

bars = load_bars("AAPL", "1m", "2026-01-05", "2026-01-09")        # pandas DataFrame
res = run_backtest(strategy="ema_cross", params={...}, start=..., end=...)
res.summary; res.trades; res.orders; res.equity                      # 均为 DataFrame
res.open_in_ui()                                                      # 在界面中打开该次回测
```

Notebook 接口是补充手段，用于界面没有覆盖的自由分析；日常的回测、对比和复盘都在界面完成。接口要稳定，返回值一律是 DataFrame 或简单对象。

### 10.7 研究工作流界面

这是第一批交付的核心：不写脚本、不敲命令，就能完成一次完整的研究循环。

1. **发起回测**：在表单中选择策略、填写参数（表单由参数模型自动生成）、选择标的和日期区间、调整撮合与成本假设，点击运行。
2. **看结果**：汇总统计、成本分项与成本敏感性表、权益曲线、每日盈亏柱状图、分组统计表、路径歧义和数据质量提示。
3. **看交易**：交易明细表可排序和筛选（按原因、方向、盈亏、时间段）。
4. **复盘**：点击任一笔交易，图表跳到对应时间，显示入场和出场标记、原因、当时的指标值，以及该笔交易是否有路径歧义。
5. **保存和比较实验**：每次回测可以命名、加备注和标签。实验列表按策略、参数、日期筛选；选中两次或多次回测并排比较统计和权益曲线，并标出它们在参数、数据和撮合假设上的差别。
6. **参数扫描**：在表单中给参数填多个取值即可发起扫描，结果以参数对比表呈现，每一行可以点进去看单次回测。

## 11. 模块七：界面（`trader.gui`）

（按决策记录 `docs/decisions/0005-desktop-ui.md` 修订：界面是 PySide6 桌面程序，面板可停靠、可拖出成独立窗口，图表可以开任意多个。研究版界面直接调用数据、指标和回测模块；实盘版界面与交易引擎分属两个进程，下表的接口清单作为两者通信协议的功能清单，在阶段 6 定具体实现。）

### 11.1 后端接口

| 类别 | REST 接口 | 说明 |
| --- | --- | --- |
| 元信息 | `GET /api/meta` | 运行组合、账户号（脱敏）、能力开关、版本 |
| 行情 | `GET /api/bars`，`GET /api/symbols`，`GET /api/coverage` | 历史 bar 查询、标的清单、数据覆盖与质量 |
| 指标 | `GET /api/indicators`，`POST /api/indicators/compute` | 指标目录与参数模型；计算历史值 |
| 订单 | `POST /api/orders`，`PATCH /api/orders/{id}`，`DELETE /api/orders/{id}`，`GET /api/orders`，`GET /api/positions`，`GET /api/account` | 手动下单、改单、撤单、查询 |
| 信号确认 | `GET /api/signals/pending`，`POST /api/signals/{id}/confirm`，`POST /api/signals/{id}/reject` | 仅实盘版，见第 8.6 节 |
| 策略 | `GET /api/strategies`，`POST /api/strategies/{name}/start`，`POST /api/strategies/{id}/pause`，`POST /api/strategies/{id}/stop` | 策略目录、启动实例、暂停、停止 |
| 风控动作 | `POST /api/risk/halt-entries`，`/cancel-all`，`/flatten`，`/stop-all`；`GET /api/risk`；`POST /api/reconcile` | 四个动作分开；风控状态；重新同步 |
| 回放 | `POST /api/replay/load`，`/play`，`/pause`，`/step`，`/seek`，`/speed` | 仅研究版 |
| 回测与实验 | `POST /api/backtests`，`GET /api/backtests`，`GET /api/backtests/{id}/...`，`PATCH /api/backtests/{id}`，`POST /api/backtests/compare` | 仅研究版；发起、查询、命名与备注、对比 |

WebSocket `/ws` 采用订阅频道的方式推送：`bars:{symbol}:{timeframe}`、`indicator:{实例 id}`、`orders`、`fills`、`positions`、`account`、`strategy_log:{id}`、`marks:{symbol}`、`system`。

### 11.2 页面

**交易工作台（两个应用共用）**

- 主图：K 线、成交量、指标叠加与独立窗格、周期切换、标的切换。时间轴显示美东时间。
- 图上标记：订单与成交（买卖箭头，悬停显示数量、价格、原因）、当前持仓均价线、挂单价格线、策略标记。
- 行情质量提示：当前标的的数据来源、订阅方式，以及缺口和降级时段在图上的底色标示。
- 指标面板：从目录添加指标、填写参数、调整颜色、移除。布局可保存为模板。
- 下单面板：标的、方向、数量、订单类型、价格、止盈止损；显示预估金额、风控预检结果和该标的当前的所有者。
- 底部面板：持仓、挂单、当日成交、当日盈亏（手续费未到齐时标注未结算）、策略日志、系统消息。
- 策略面板：策略目录、参数表单（由参数模型自动生成）、启动、暂停与停止、各实例占用的标的、持仓和盈亏。
- 信号确认卡片（仅实盘版）：见第 8.6 节，带倒计时。
- 常驻的风控动作按钮和连接状态指示；状态无法确认时明确显示“状态未确认”。

**回放控制条（仅研究版）**

- 选择日期和起始时间，播放、暂停、单步、倍速、跳转。
- 回放过程中可以手动下单，也可以挂载策略，成交由模拟撮合给出。回放结束后可查看当次练习的交易统计。

**回测页（仅研究版）**：见第 10.7 节。

**数据页（仅研究版）**：各标的的数据覆盖日历、导入校验报告、Massive 与 IBKR 数据对比结果。

### 11.3 环境区分

| 运行组合 | 顶部横幅 | 手动下单确认 |
| --- | --- | --- |
| backtest / replay | 灰蓝色，“研究（模拟）” | 不需要 |
| local\_paper | 绿色，“实时行情，本地模拟成交” | 不需要 |
| broker\_paper | 黄色，“IBKR 模拟账户”，显示账户号 | 不需要 |
| live | 红色，“实盘”，显示账户号 | 每笔弹出确认框，显示标的、方向、数量、预估金额 |

浏览器标签页标题同样带模式前缀。实盘版与研究版默认使用不同端口，避免同时打开时混淆。

### 11.4 图表实现要点

- 使用 Lightweight Charts。指标绘制完全由后端返回的 `OutputSpec` 驱动，前端只实现 `line`、`histogram`、`band`、`marker`、`hline` 五种绘图方式；超出这五种的需求要扩展前端。
- 历史数据按可视范围分段懒加载；向左拖动时再请求更早的数据。
- 实时更新一律按 bar 的 `ts_start` 定位：图上已有这个时间戳的 K 线就更新它，没有才新增。`closed=False` 更新未收盘的那一根；`closed=True` 把同一根更新为最终值并封口，不另外追加，避免同一根 K 线重复显示。
- **回放时不泄露未来数据**：回放模式下，bar 查询、指标计算、成交标记等所有接口都由后端按回放时钟截断，前端拿不到当前回放时间之后的任何数据；跳转到更早的时间时前端清空缓存重新加载。这条规则要有接口层的测试。
- 第一版不做画线工具；需要画线时可以并排使用 TradingView。

## 12. 模块八：配置、运行模式与实盘安全

运行模式由启动入口和配置文件共同决定，实盘版在启动时做一系列强制检查，任何一项不通过就拒绝启动。

### 12.1 启动方式

```text
trader-research                               # 研究版：回测、回放、实验对比、notebook 接口
trader-live --profile local_paper             # 实盘版：IBKR 实时行情，本地模拟成交
trader-live --profile broker_paper            # 实盘版：IBKR 模拟账户
trader-live --profile live --confirm-live     # 实盘版：真实账户
```

### 12.2 配置文件

- `config/research.yaml`、`config/local_paper.yaml`、`config/broker_paper.yaml`、`config/live.yaml`，用 pydantic 模型校验，未知字段报错。
- `config/universe.yaml`：固定的标的清单，每个标的带半价差默认值、行情订阅方式（逐笔或快照）和是否允许做空。研究版和实盘版共用这份清单。
- 其他内容：数据路径、交易时段、风控阈值、模拟撮合参数、IBKR 连接参数与账户号（实盘版）、策略白名单及每个策略的执行方式 `confirm` 或 `auto`（实盘版）。
- 研究版和实盘版的运行数据库、日志目录和端口分开配置，互不共用。
- 密钥类信息（如 Massive 的下载密钥）只放在 `.env`，不进 git。IBKR 的 API 连接走本机 Gateway，不需要在本系统中保存账户密码。

### 12.3 实盘版启动检查

1. 配置中的风控阈值全部显式填写，没有缺省项。
2. `live` 配置必须带 `--confirm-live` 参数。
3. 连接端口与配置的运行组合相符；连接后账户号校验通过（见第 9.3 节）。
4. 白名单中的每个策略，其源码哈希与配置中登记的一致；运行目录有未提交的改动、或当前提交没有版本标签时，拒绝以 `live` 配置启动。
5. 清单中每个标的的行情订阅方式可以满足，并且满足白名单策略声明的行情能力要求。
6. 系统时钟与 IBKR 服务器时间的偏差在阈值内。
7. 按第 5.5 节的固定顺序完成补数、预热和对账；有缺口或不一致时策略保持暂停，等待人工确认。
8. `live` 配置中的上线清单项已由用户标记完成：TWS 或 Gateway 的 API 预防设置、账户资金上限。

### 12.4 运行期保护

- 策略启动后默认处于暂停状态，由人工在界面上逐个开启；默认执行方式是半自动确认。
- 持久化：订单请求、订单回报、成交、手续费、标的归属、人工确认结果和 `ctx.state` 按第 8.2 节的顺序先写入 SQLite，再追加到 `runs/live/<日期>/events.jsonl`；定时器、交易时段、风控和系统事件也记入事件日志。行情另存。
- 崩溃恢复：重启后从 SQLite 恢复订单表、标的归属和策略状态。已落盘但没有收到任何券商回报的订单置为 `UNKNOWN`，向券商查询后归约；等待确认中的信号一律作废。然后与 IBKR 对账，策略保持暂停。
- 界面不是运行的必要条件：浏览器关闭后引擎继续运行，持仓的保护依靠已在券商端生效的保护单。
- 心跳：引擎主循环超过 N 秒无响应，或行情看门狗触发，界面显示报警并可配置桌面通知。
- 后端只监听 `127.0.0.1`。配置成其他地址时启动时给出明确警告。

### 12.5 发布流程

- 研究和开发在主干进行；实盘只运行打了版本标签的提交。
- 实盘版从单独的目录运行：用 git worktree 或单独克隆检出版本标签，有自己的虚拟环境、配置和运行数据库。这样在研究目录里改代码、切分支、装依赖都不会碰到正在运行的交易进程。
- 一个策略从研究到实盘的固定顺序：回测（含成本敏感性和路径歧义检查）→ 样本外回测 → 回放检查 → local\_paper → broker\_paper 运行若干个交易日 → 小仓位实盘半自动 → 按策略放开全自动。每一步的结果记录在策略自己的说明文件里。

## 13. 测试与一致性验证

测试的重点不是覆盖率，而是三件事：撮合规则正确、没有未来数据、研究和实盘行为一致。

| 测试 | 验证内容 | 做法 |
| --- | --- | --- |
| 撮合规则 | 第 9.2 节每条规则 | 手工构造 bar 序列，断言成交时间、价格、数量；包括有利跳空、触及不成交、共享参与率预算、止损限价两阶段、最低手续费只收一次 |
| 事件阶段顺序 | 第 2.3 节 | 策略在某根 bar 上发出的订单不能用这根 bar 的价格成交，也不能用交付时已经开始的下一个区间的开盘价成交；行情稀疏时定时器照常触发 |
| 指标正确性 | 内置指标数值 | 与成熟的指标库在样例数据上对比，误差在容差内（对比库只作开发依赖）；盘中启动的活动实例与历史计算在同一时刻数值相同，用日内 VWAP、开盘区间、前一日高低收各验证一次 |
| 聚合一致性 | 1 秒 bar 合成的 1 分钟 bar | 与直接由 tick 合成的结果一致；跨时段、半日市、稀疏成交的边界用例；由 tick 聚合的结果与 Massive 官方聚合在样例日上对得上 |
| 无未来数据 | 策略在时刻 t 的决策不依赖 t 之后的数据 | 把 t 之后的数据替换成随机值重跑，t 之前的订单必须完全相同 |
| 回放界面无未来数据 | 第 11.4 节 | 回放时钟停在 t，调用所有查询接口，返回结果中不得有 t 之后的数据 |
| 确定性 | 同样输入同样输出 | 同一配置连续跑两次，比较规范化后的业务结果（去掉运行编号、生成时间等字段） |
| 回放与回测一致 | 同一天、同一策略 | replay 全速跑完的订单序列与 backtest 完全相同；向后跳转后新分支从空仓开始，原分支的交易记录保留且不带入 |
| 录制重放一致 | 引擎在两种驱动方式下，对同样的输入作出同样的决策 | 把一次 local\_paper 或 broker\_paper 运行录下的全部输入事件（行情、订单回报、成交、手续费、定时器、交易时段事件、人工确认与拒绝、策略启动暂停与停止、手动订单）按原顺序重放给引擎。重放时执行器换成只记录不执行的桩：不连接 IBKR，也不运行模拟撮合，成交只来自录制的事件，策略发出的订单意图必须与当时完全相同 |
| 订单归约 | 第 8.1 节 | 事件序列用例：重复通知、成交先于确认、撤单途中成交、撤单后补到成交、手续费晚到、发送超时后查询、成交更正；断言最终订单、持仓和成交账本 |
| 括号单场景 | 第 8.2、9.3 节 | 父单部分成交、子单被拒、父单取消、两张子单竞争成交、改子单；断言不出现超量卖出和无保护持仓 |
| 行情衔接 | 第 5.5、6.1 节 | 历史与实时的交界、重叠数据去重、迟到 tick、封口宽限 |
| 策略状态恢复 | 第 7.3 节 | 运行到一半停止，恢复 `ctx.state` 后继续，结果与不中断运行相同；不出现重复下单。在落盘之后、发送之前中断的订单，恢复后进入 UNKNOWN 并走查询流程，不直接重发。成交已落盘但 on\_fill 尚未执行时中断，恢复后补执行一次且只执行一次 |
| 标的归属 | 第 7.4 节 | 重复占用被拒；人工接管时策略暂停，持仓、挂单和保护单原样转到“手动”名下且保护单不被撤销；持仓或挂单未清空时不能释放，也不能交给另一个策略 |
| 风控 | 第 8.3 节每条规则 | 每条规则各一个通过用例和一个拒绝用例；在途订单和待确认订单计入敞口；发送前的第二次检查能拦下创建时通过、发送时已不满足的订单；重复确认只发出一张订单；同一张订单经过创建、确认、发送三次检查，敞口只占用一次，频率和重复保护只计一次；持仓已到上限或已触发亏损熔断时，减仓单和止损单仍能通过；半日市的收盘前平仓时间正确 |
| IBKR 适配 | 状态映射、重连、对账 | 用假的 IB 客户端回放录制的消息序列；不连接真实服务 |
| 分层规则与策略约束 | 第 3.3、7.3 节 | import-linter；静态扫描 `user/strategies/` |

- 自动化测试永远不连接 IBKR，也不发出任何订单。
- 需要真实连接的检查写成一份手工清单 `docs/paper_checklist.md`，只在 broker\_paper 配置下由人执行：下单、部分成交、改单、撤单、括号单（含父单部分成交时保护单的实际行为）、发送过程中断线、断线重连、Gateway 重启、本地进程崩溃后恢复、对账不一致。
- CI（本地 pre-commit 即可）：ruff、pyright、pytest、import-linter 全部通过才算完成。

## 14. 开发阶段与验收标准

第一批交付是阶段 0 到阶段 4，目标是一个可用的研究工具：导入数据、回测、看成本和交易原因、点击交易复盘、保存和比较实验。阶段 0 到 5 不接触券商下单；券商相关的边界情况全部集中在阶段 6 以后。

| 阶段 | 内容 | 验收标准 |
| --- | --- | --- |
| 0 骨架 | 仓库结构、依赖分组、核心数据模型、事件、调度器与时钟、交易日历、质量工具、`CLAUDE.md` | 质量工具全部通过；分层规则生效 |
| 1 数据 | Parquet 存储、Massive 下载与校验、catalog 与指纹、历史查询、全速回放行情源；一个独立的 IBKR 只读行情录制脚本 | 下载清单内全部标的并输出校验报告；能查询任意标的任意一天的 1 秒 bar（其他周期在阶段 2 提供）；用户录一个交易日的 IBKR 行情并得到与 Massive 的对比报告 |
| 2 聚合与指标 | Bar 聚合器、指标框架、内置指标、自定义指标自动发现 | 聚合一致性和指标正确性测试通过；在 `user/indicators/` 放入新文件后无需改其他代码即可计算 |
| 3 策略与回测 | 策略框架（含 `ctx.state` 和标的归属）、订单归约的常规路径、风控、模拟撮合、回测运行器、输出与统计（含成本敏感性和路径歧义）、两个示例策略、命令行 | 示例策略在一个月数据上回测完成并输出全部文件；撮合、事件阶段顺序、无未来数据、确定性、策略状态恢复测试通过；达到性能目标 |
| 4 研究闭环界面 | API 服务、图表与指标面板、回测表单、结果页、交易明细与点击复盘、实验命名与对比、表单发起参数扫描 | 不使用命令行和 notebook 完成第 10.7 节的六个步骤；自定义指标自动出现在指标目录并正确绘制 |
| 5 回放与 notebook | 定速回放、回放控制条、回放中手动下单与挂载策略、notebook 接口 | 可回放任意一天，暂停、单步、倍速正常；回放与回测一致、回放界面无未来数据的测试通过 |
| 6 IBKR 接入 | 实时行情源（能力声明、补数衔接、封口宽限）、local\_paper；IBKR 执行器、订单归约的全部券商边界情况、括号单、对账、事件日志与崩溃恢复、broker\_paper | 订单归约、括号单场景、行情衔接、录制重放一致的测试通过；手工清单在模拟账户上全部通过 |
| 7 实盘半自动 | 信号确认流程、启动检查、环境区分、四个风控动作、看门狗与报警、独立运行目录与发布流程 | 第 12.3 节每项检查都有对应的失败用例测试；broker\_paper 连续运行 5 个交易日无未处理异常；之后以最小仓位开始半自动实盘 |
| 8 放开全自动 | 不新增功能；按策略实例逐个把执行方式改为 `auto` | 该策略半自动实盘运行满约定的交易日数，确认统计、对账记录和实际成交成本与回测假设的对比经用户审阅 |

### 14.1 给 Claude Code 的工作约定

1. 把本文档保存为 `docs/DESIGN.md`。在阶段 0 创建 `CLAUDE.md`，内容是第 1.3、3.3、7.3 节的规则摘要和常用命令。
2. 一次只做一个阶段。每个阶段开始前先给出该阶段的任务清单，结束时给出验收标准的逐项结果和一条可运行的演示命令，等用户确认后再进入下一阶段。
3. 接口以本文档为准。需要偏离时，先在 `docs/decisions/` 写一条决策记录（背景、选项、结论），并同步更新本文档。
4. 新增第三方依赖前先说明理由；实盘依赖组尽量不增加。
5. 每个模块先写接口和测试，再写实现。第 13 节列出的测试属于对应阶段的交付内容。
6. 任何测试和脚本都不得连接 IBKR 实盘端口，不得在自动化流程中下单。阶段 1 的行情录制脚本只订阅行情，不包含任何下单代码。
7. 遇到第 15 节的待确认事项，停下来询问用户，不要自行假设。
8. 不实现第 1.2 节列出的非目标，也不为它们预留抽象。
9. 券商边界情况（第 8.1 节的异常路径、第 9.3 节）只在阶段 6 实现。阶段 0 到 5 只需要保证数据模型和归约函数的接口能容纳它们，不要提前实现。
10. 用户不审阅代码。每个阶段的交付说明要用使用者能验证的方式写：做了什么、怎么在界面或命令行里看到、哪些假设会影响回测结论。

## 15. 已确定的决策与待确认事项

已确定的决策：

- 标的先用固定清单，不做每日扫描选股和全市场回测。清单是人工挑选的，回测结果带有选择偏差，解读时要记住这一点。
- 实盘先做半自动确认，稳定后按策略逐个放开全自动。
- 第一批交付压缩为研究闭环（阶段 0 到 4），日常操作在界面完成。
- 第一版中一个标的同一时间只归一个所有者。
- 数据：Massive 1 秒聚合 bar，原始价格，从 2024-01 起；固定清单 22 个标的，见 `config/universe.yaml`（2026-10-07 确认）。暂无 tick 数据。
- IBKR：使用 IB Gateway；模拟账户已开通；行情订阅为美股 Network A、B、C 一级（非专业）。逐笔成交的并发订阅额度实测为 5 个（第 6 个起报错 10190），清单内其余标的只能用快照行情（2026-10-07 实测）。
- 时段：研究和交易覆盖全部时段（盘前、常规、盘后），并为 2026-12-06 起的 23/5 交易（夜盘 21:00–04:00，交易日从前一天 21:00 开始）预留，见 `docs/decisions/0002-all-sessions-and-23x5.md`（2026-10-07 确认）。
- 交易方式（2026-10-07 确认）：允许做空（回测假定可借到券、不计借券费，可按标的关闭）；保证金账户，实盘账户净值约 19.5 万美元，高于 2.5 万美元，不受 PDT 日内交易次数限制；模拟账户额度 105 万美元，但按保守规模使用。
- 规模（2026-10-07 确认）：回测默认初始资金 10 万美元；单笔金额 5,000～20,000 美元，风控默认单笔和单标的持仓上限 20,000 美元。
- 持仓时长（2026-10-07 确认）：每天 0～10 笔，持仓几分钟到几小时，1 秒 bar 撮合足够，暂不需要 tick 和报价数据（Massive 的逐笔成交也不在当前订阅内）；价差用清单里每个标的的半价差配置值。
- 收盘前平仓（2026-10-07 确认）：默认在常规时段收盘前平仓，盘后不持仓；少数策略可以设置为持仓到盘后、20:00 前平仓（盘前盘后只用限价单）。23/5 交易开始后再评估。
- 运行环境：Windows 11；研究版和实盘版在同一台机器上运行（2026-10-06 确认）。因此两者的端口、运行数据库和日志目录必须在配置中分开，实盘版按第 12.5 节从单独的目录运行。

以下问题会影响具体实现，需要用户在对应阶段开始前回答。

| 事项 | 影响的模块 | 需要在哪个阶段前确认 |
| --- | --- | --- |
| 实盘能否使用资金有限的独立账户或子账户 | 第二道防线 | 阶段 7 |
| 报警方式：只在界面上提示，还是需要桌面通知或手机消息 | 实盘运行期保护 | 阶段 7 |

## 资料来源

- [ib\_async 文档](https://ib-api-reloaded.github.io/ib_async)：项目说明与 Python 版本要求。
- [Massive 文档目录](https://massive.com/docs/llms.txt) 与 [Flat Files 快速入门](https://massive.com/docs/flat-files/quickstart)：批量文件的数据类型与格式。
- IBKR 的端口、行情推送频率、手续费等数值来自通用经验，开发时以 IBKR 官方文档现行内容为准。
