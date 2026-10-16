# 0009 日内波动与震荡强度报表（研究工具）

日期：2026-10-16　状态：已采纳

## 背景

用户要一份 notebook 报表：输入一组证券和日期范围，计算日内波动和震荡特征并打分，回答"哪些证券适合做日内交易"。
规范见 `docs/intraday_volatility_report_spec.md`（v2，经过一轮审阅：v1 的分类阈值在实际数据上会把所有证券都判成震荡型，
1 分钟路程类指标受买卖价差反弹影响，v2 已改）。它不属于 DESIGN.md 第 14 节的开发阶段，是研究版的一个独立工具，
不碰实盘代码，所以和阶段 6 的验收并行进行（用户 2026-10-16 确认）。

## 选项

1. 计算写在 notebook 里：最快，但无法测试，以后观察股票池、每日选股也没法复用。
2. 放在 `trader.research` 里：research 是给 notebook 的稳定接口层，塞进大量计算会让它变杂。
3. **新子包 `trader.analytics`**：只依赖数据层，有测试；notebook 只设参数和展示。

## 结论

采用 3。

- **分层**：`trader.analytics` 只依赖 `trader.core` 和 `trader.data`（不依赖 `trader.research`，以后 research 可以反过来导出它）；
  不能导入 brokers、engine、live、oms、strategy、backtest、gui、apps。core、indicators、strategy、oms、engine、live、
  实盘版入口都不能导入 analytics。均由 import-linter 检查。
- **数据**：通过 `HistoryService` 读常规时段 1 分钟、5 分钟、日线和 1 秒 bar，汇总规则与图表、回测相同；无成交的时段本来就不生成 bar，
  正好满足"缺失不填充"。日内指标用当天原始价格；前收盘和 ATR 在拆股复权到区间末日的日线上计算，再乘以当天的复权系数换回原始价格口径。
  只分析本地已有数据的证券，不在 notebook 里自动下载。
- **摆动算法**：一根 bar 内最高价、最低价的先后两种顺序都试，结果不同才算歧义，再读这一分钟的 1 秒 bar 按顺序处理；
  1 秒 bar 仍分不出时取确认摆动较少的顺序，一样多时按开盘价离最高价还是最低价近来定。
- **时段指标**：收益率归入其结束 bar 所在的时间桶；摆动按确认时刻归桶；热力图的值是该桶方差占全天方差的比例（各桶之和为 1）。
- **单日详情图的"典型交易日"**：振幅和摆动次数都最接近该证券中位数的一天（按标准差归一后距离之和最小）。
- **导出**：`reports/intraday_vol_<开始>_<结束>/`（不进 git）：`ranking.csv`、`daily.csv`、`excluded_days.csv`（UTF-8 带 BOM，Excel 直接打开）、
  `swings.parquet`。HTML 报告用 jupyterlab 自带的 nbconvert（`--no-input`）。Excel 带颜色导出第一版不做，避免多加 openpyxl。
- **依赖**：研究依赖组加 matplotlib（用户同意）。实盘发布目录只装 core + live，不受影响。
  2026-10-16 清华镜像连不上，无法更新 `uv.lock`：matplotlib 先手工装进研究环境，等镜像恢复后再写入 `pyproject.toml` 并更新锁文件（待办）。

## 首批数据的观察（2026-07-01 ~ 2026-09-30，22 只证券）

- 5 分钟效率比中位数约 0.10，1 分钟约 0.045；`body_ratio` 中位数约 0.44，在证券之间差别不大，主要反映日子之间的差别。
- `swing_capturable_pct_k025` 对 SPY、QQQ 的中位数为 0（0.25 ATR 阈值下，单次摆动很少超过 2θ + 成本），所以它相对 SPY 的比值为空。
- 各证券开盘后第一个 30 分钟的波动占全天 11%–28%，个股集中度明显高于 SPY。
