# 0001 数据来源改为直接从 Massive REST 下载原始价格，下载器并入 trader.data

日期：2026-10-07　状态：已采纳

## 背景

DESIGN.md 5.2 设想的是"导入用户提供的文件"（`trader data import --path ...`），秒级文件格式待定。
实际情况：

- 用户已有一个下载工具（`download_gui.py` + `market_data/massive.py`），通过 Massive REST
  接口 `/v2/aggs/ticker/{T}/range/1/second/{日}/{日}` 按天下载 1 秒聚合 bar；
- 已下载的 22 个标的都是**拆股复权**价格、按月分文件、带 `session` 列，时段判断不认半日市；
- 用户希望数据下载成为系统里的一个功能：给已有标的往前补或更新到今天、新增标的。

## 选项

1. 保留旧下载器，另写导入器把旧文件转换成新格式（复权价按拆股表换算回原始价）。
2. 下载器并入 `trader.data`，直接下载**原始价格**写入新格式，旧数据全部重新下载（约 5GB）。

## 结论

选 2（用户决定：一次重下，以后统一处理，不再有复权问题）。

- `trader.data.massive`：REST 客户端（1 秒聚合，`adjusted=false`；拆股；分红）。只属于研究版，
  实盘版入口不得导入（import-linter 规则）。
- `trader.data.download`：增量下载。"往前补"、"更新到今天"、"新增标的"都是"对指定标的下载指定区间"，
  已登记在 catalog 的日期跳过；当天要等盘后结束 30 分钟后才下载；每天先校验再原子写入。
- 命令 `trader data import` 改为 `trader data download` / `update` / `actions`；
  逐笔成交文件的导入适配器推迟到确实需要 tick 数据时再做。
- 界面：阶段 1 先把 `download_gui.py`（PySide6）改为调用 `trader.data.download`；阶段 4 的网页增加
  "数据管理"页面，调用同一套代码，届时旧 GUI 可以删除。
- 时段不再存进数据文件，查询时由交易日历判断（半日市正确）。
- 旧数据目录 `data/<代码>/` 和 `market_data/` 在新数据下载并核对完成后删除。
