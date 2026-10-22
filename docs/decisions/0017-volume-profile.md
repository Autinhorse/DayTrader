# 0017 Volume Profile 模块

日期：2026-10-22　状态：已采纳　依据：用户要求按 `docs/volume_profile_spec.md`（v1）实现，代码位置、数据来源和组织方式由我们决定；
数据精度优先用 1 秒 bar，并与 1 分钟、5 分钟 bar 对比差异

## 背景

规范由不了解本项目结构的人编写，建议把代码放在 `src/trader/profile/`。本项目还要求：

- 以后 K 线图上要显示 Volume Profile 的关键价位（第 2 步），策略回测也要用它们；指标和策略只能依赖 `core`（分层规则）。
- 研究用的数据清洗（坏数据时段、离群成交）在 `trader.analytics.cleaning`，ADR 的定义在日内波动报表里。

## 选项

- A. 按规范新建 `trader.profile`，只依赖数据层：清洗和 ADR 要么复制一份，要么反向依赖 analytics；以后的指标也无法复用计算逻辑。
- B. 纯计算放 `trader.core.volume_profile`（numpy，无 IO），读数据、清洗、缓存、各种时间窗口放 `trader.analytics.volume_profile`。

## 结论

采用 B。

1. `trader.core.volume_profile`：细网格（$0.01）累加（uniform / close / typical / vwap）、拆股换算、合并成粗格子、
   POC、价值区、HVN / LVN、形态指标。不读文件、不取时间，以后图表指标和策略直接用它。
2. `trader.analytics.volume_profile`：
   - 数据：默认 1 秒 bar；没有 1 秒数据的日子退回 Massive 官方 1 分钟 bar（`source_bar = "1m"`，此时无法单独剔除竞价）。
     对比用的 1 分钟、5 分钟版本由清洗后的 1 秒 bar 合成，只差在 bar 粒度，便于判断精度损失。
   - 清洗：沿用决策 0011（坏数据时段、离群成交）。离群阈值 = `print_filter_adr` × 前一日的 ADR，ADR 用已缓存的
     **清洗后**常规时段振幅计算（报表用原始振幅），跨拆股时先换算到同一口径。
   - 每日细网格缓存在 `data/profiles/<构建参数>/symbol=X/date=Y.<源数据指纹>.parquet`，按时段分段存：
     `pre`、`rth`、`open_cross`（9:30:00 那一秒）、`close_cross`（收盘前最后一秒和收盘那一秒）、`post`。
     竞价是否剔除在读取时决定，不需要重算；剔除的部分仍单独记账（守恒检查用）。当天的统计（常规时段开高低收、清洗计数）
     放在文件元数据里。源数据重新下载后指纹变化，缓存自动失效。
   - 复权系数不写进缓存（新的拆股会让它失效），读取时按 `corporate_actions` 现算。
3. 价格口径：交易日 T 的关键价位一律用 **T 当天的实际价格**（之前日子的 Profile 按拆股换算到 T 的口径），
   与当天屏幕上看到的价格一致；`get_profile(start, end)` 用 end 当天的口径。
4. 时点：前一日、复合、衰减、锚定、盘后类型只用 T−1 及之前完整交易日的数据；developing、premarket 只用 t 之前已收盘的 1 秒 bar，
   清洗也只用 t 之前的数据（离群判断用居中的滚动中位数，最后 30 根 bar 改用截断窗口），因此删掉 t 之后的数据重算，结果完全相同。
5. 格子大小：ADR × 0.02 向上取整到 $0.01；用 T 开盘时已知的 ADR（截至 T−1）。格子按价格绝对对齐（下沿是格子大小的整数倍）。
6. 对规范的补充：
   - HVN 之间没有满足条件的 LVN 时，只保留较高的那个峰，因此 `n_hvn ≥ 2` 即表示多峰分布。
   - HVN 的显著性 = 峰值 / 两侧谷值中较高者；LVN 的显著性 = 谷值 / 两侧峰值中较低者（越小越显著）。
   - `min_bar_volume` 分常规时段（默认 0）和扩展时段（`min_bar_volume_eth`，默认 100）两个参数。
   - 衰减 Profile 最多回看 10 个半衰期（更早的权重 < 0.1%）。
   - 自动锚点：常规时段开盘价相对前一日收盘价跳空 ≥ 1 ADR 的日子，从当天开盘开始；`anchored` 取 T 之前最近的锚点（默认回看 60 个交易日）。
   - naked POC 的触及用清洗后的常规时段 1 秒 bar 判断，从 POC 当天的下一个交易日开始。

notebook：`user/notebooks/Volume Profile.ipynb`；命令行 `uv run trader profile levels|developing|build`。

## 精度对比的结果（同日）

1 秒、1 分钟、5 分钟 bar 的对比见 `docs/研究记录.md` 2026-10-22：1 分钟 bar 与 1 秒 bar 几乎一样，
5 分钟 bar 的 POC 误差明显变大。因此第 2 步的图表指标用 1 秒（或 1 分钟）bar 计算，不直接用图表周期的 5 分钟 bar。
