# DayTrader

个人美股日内交易系统：一份核心代码（`src/trader`），两个应用（研究版 `trader-research`、实盘版 `trader-live`）。完整设计见 `docs/DESIGN.md`，以它为准；需要偏离时先在 `docs/decisions/` 写决策记录（背景、选项、结论），并同步更新 DESIGN.md。

运行环境：Windows 11，研究版和实盘版在同一台机器上。用户用中文交流，不审阅代码；交付说明要写成使用者能自己验证的方式。

## 常用命令

```bat
run_research.bat        :: 双击打开研究版桌面程序（= uv run trader-research）
run_jupyter.bat         :: 双击打开 JupyterLab（user/notebooks）
uv run trader-research  :: 研究版桌面程序（图表、回测、结果、实验对比、参数扫描、回放、数据管理）
uv sync                 :: 安装/同步依赖（新增依赖前先说明理由）
check.bat               :: 全部质量检查：ruff、ruff format、pyright、pytest、lint-imports
uv run pytest           :: 只跑测试
uv run ruff format .    :: 格式化
uv run lint-imports     :: 只查分层规则
run_downloader.bat      :: 数据管理 GUI（下载/更新 Massive 1 秒 bar），调用 trader.data.download
uv run trader data update            :: 清单内标的更新到最新交易日
uv run trader data report            :: 数据覆盖与校验报告
uv run trader data bars --symbol SPY --date 2026-10-01
uv run python tools/ib_record.py     :: IBKR 只读行情录制（只连模拟端口 4002）
uv run trader data compare --date <录制日期>
uv run trader data bars --symbol SPY --date 2026-10-05 --timeframe 5m --session extended
uv run trader data check-agg --symbols SPY --date 2026-10-05   :: 1 分钟 bar 与 Massive 官方对比
uv run trader indicators list                                  :: 内置 + user/indicators/ 的指标
uv run trader indicators compute --symbol SPY --timeframe 5m --name ema --params period=20 --date 2026-10-05
uv run trader strategies list                                  :: user/strategies/ 的策略
uv run trader backtest run config/backtests/orb_nvda.yaml      :: 结果写入 runs/<run_id>/
uv run trader backtest list
```

聚合：`trader.core.aggregation.BarAggregator`（增量，回测/实时用）与 `trader.data.aggregate`（向量化，历史/图表用）
规则必须一致，`tests/test_aggregation.py` 保证。bar 不跨时段，见 docs/decisions/0003。
指标：继承 `trader.indicators.base.Indicator` 并 `@register_indicator`；内置在 `src/trader/indicators/builtin/`，
自定义放 `user/indicators/`（自动发现）。
策略：继承 `trader.strategy.base.Strategy` 并 `@register_strategy`，放 `user/strategies/`；示例 ema_cross、orb_breakout。
回测：`trader.backtest.runner.run_backtest(config)`；引擎 `trader.engine.engine.BacktestEngine`；
撮合 `trader.brokers.sim.matcher`；订单与风控 `trader.oms`。实现约定见 docs/decisions/0004。
界面：PySide6 桌面程序（决策 0005），`trader.gui`；图表是 QWebEngineView 里的 Lightweight Charts
（`src/trader/gui/assets/`）。回测与参数扫描在子进程 `python -m trader.backtest.worker` 里运行。
界面改动后用 QWidget.grab() 截图检查外观（测试只覆盖逻辑）。
回放：`trader.backtest.replay.ReplaySession` 驱动同一个引擎（start / advance / finish），界面在“回放”面板；
查询一律截止到回放时钟。notebook：`from trader.research import load_bars, compute_indicator, run_backtest,
load_run, sweep`，示例 `user/notebooks/`。见 docs/decisions/0006。
给用户的扩展说明（新指标、新策略、新图表）：docs/扩展指南.md。

数据：`data/bars/1s/symbol=X/date=Y.parquet`（原始价格）、`data/catalog.sqlite`、`data/corporate_actions.parquet`、
`data/live/<日期>/`（IBKR 录制）。Massive key 在 `.env` 的 `MASSIVE_API_KEY`（旧的 `config.json` 也认）。

## 设计原则（DESIGN.md 1.3）

1. 策略只有一份，代码中不允许按运行模式分支；差异只由注入的行情源、执行器、时钟承担。
2. 共享的是策略决策和订单语义，不是成交结果；成交、延迟、数据、成本的差异要在报告中可见。
3. 共享代码、隔离运行：两个应用是独立进程，配置、运行数据库、端口、依赖环境各自独立。
4. 事件驱动、确定性优先：同样的配置、数据、代码，回测业务结果必须完全相同。
5. 禁止未来数据：策略只能看到已收盘且已到 `available_time` 的数据。
6. 数据质量差异不隐藏：合成、降级、缺口都带标记；不满足策略数据要求就暂停，不悄悄替换。
7. 实盘保守：状态对不上就停止策略并报警，不自动修正；实盘依赖尽量少。
8. 每笔订单必须带 `Reason`，贯穿订单、成交和报告。

## 分层依赖（DESIGN.md 3.3，由 import-linter 检查）

- `core` 不依赖任何其他子包。
- `indicators`、`oms` 只依赖 `core`；`strategy` 只依赖 `core` 和 `indicators`。
- `user/strategies` 只能导入 `trader.strategy`、`trader.indicators`、`trader.core`（`tests/test_architecture.py` 静态扫描）。
- `brokers.ibkr` 不被 `backtest`、`research`、`apps.research_app` 导入。
- `research` 不被 `apps.live_app` 导入。

## 策略约束（DESIGN.md 7.3）

- 不直接取系统时间，不做文件和网络读写，不导入 `trader.brokers`。
- 不判断当前运行模式；`ctx` 不提供查询运行模式的方法。
- 下单、改单、撤单都要给 `Reason`：`code` 用稳定短标识，`context` 放下单瞬间的关键指标值。
- 跨重启要保留的状态必须放在 `ctx.state`（只接受可 JSON 序列化的值）；指标值、持仓、挂单不放进去。
- `ctx.position`、`ctx.open_orders` 只返回本策略实例名下的持仓和订单。

## 代码约定

- 时间：内部一律 UTC 纳秒整数；显示和交易时段判断用 `America/New_York`（`trader.core.timeutil`）。
- 只有 `src/trader/core/clock.py` 可以读系统时间，其他地方一律通过 `Clock`（测试强制）。
- 行情价格 float64，订单价格 `Decimal`；不假定最小报价单位都是 0.01。
- 核心类型是 `frozen=True, slots=True` 的 dataclass，放在 `trader.core`。
- 调度顺序 `(available_time, phase, 序号)`；同一时刻：撮合 → 交付成交 → 发布收盘 bar → 交易时段 → 定时器（`trader.core.events.Phase`）。
- 每个模块先写接口和测试，再写实现。

## 工作约定（DESIGN.md 14.1）

- 一次只做一个阶段：开始前给出任务清单，结束时逐项给出验收结果和一条可运行的演示命令，等用户确认再进入下一阶段。
- 任何测试和脚本都不得连接 IBKR 实盘端口，不得在自动化流程中下单。
- 遇到 DESIGN.md 第 15 节的待确认事项，停下来问用户。
- 不实现第 1.2 节的非目标，也不为它们预留抽象。
- 券商边界情况只在阶段 6 实现；之前只保证数据模型和归约接口能容纳它们。
