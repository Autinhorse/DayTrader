# 0014 排名稳定性：长区间按季度切段打分

日期：2026-10-17　状态：已采纳　依据：用户选择"长历史稳定性"作为下一步历史数据分析

## 背景

v3.3 的单季排名噪音大（2026 Q2 对 Q3 的可交易性评分名次相关只有 0.33），而本地 1 秒数据从 2024-01-02 开始，
可以看 11 个季度。股票池规范（docs/watchlist_spec.md）计划每 20–30 个交易日刷新一次，需要知道"最近的排名对下一段有多大参考价值"。

## 选项

- A. 每个季度各跑一次 `build_report`：每段开头都要重新预热 ADR（少 21 天），而且重复读数据。
- B. 跑一次长区间报表，再按段切 `daily` 表重新汇总打分。

## 结论

采用 B，新模块 `trader.analytics.stability`（只依赖 analytics 内部，不改报表本身的计算）：

1. `period_rankings(rep, freq="Q"|"M"|"Y", min_days=30)`：每段只用该段参与汇总的日子，用与报表完全相同的
   `scoring.score` 打分、段内排名（含参照证券）。每日指标（ADR、摆动）在全区间连续计算，不受分段影响。
   某只证券在某段有效天数 < min_days 就不参加该段（上市不久、区间开头 ADR 不足、末尾不满一段）。
   测试保证"切段打分 = 只用该段日子单独打分"。
2. `stability_summary`：每只证券的评分中位数/均值/最低、名次中位数/最好/最差、进前 N 的段数占比、落后一半的占比、
   可获取波动和转化效率（原始值）的中位数。
3. `adjacent_rank_corr`、`lagged_rank_corr`：相邻段、相隔 n 段的名次 Spearman 相关。
4. `selection_check(lookback, top_n)`：按过去 lookback 段的平均评分选前 top_n（不含参照证券），看下一段的评分、名次、
   可获取波动和命中数，检验"按最近排名定股票池"是否有效。
5. 图 `report.fig_period_ranks`；notebook `user/notebooks/排名稳定性.ipynb`，长区间结果缓存在
   `reports/stability_cache/`（pickle，不进 git），参数变化时自动重算。

分数是段内相对分数，各段证券数量可能不同（METU 2024-06 上市，SNDK 2025-02 上市），跨段比较用名次比例或原始值。
