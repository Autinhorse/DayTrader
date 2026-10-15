# 实盘上线清单（阶段 7 验收之后使用）

第一次用真实资金之前，按顺序做完下面每一项。程序会核对其中能核对的部分（见 DESIGN.md 12.3），
核对不了的（Gateway 设置、资金规模）要你亲自确认后在 `config/live.yaml` 的 `go_live` 里改成 `true`。
任何测试和脚本都不会连接实盘端口；实盘只能由你自己双击 `run_live.bat` 启动。

## 1. 前提

- [ ] 阶段 6 的模拟账户清单（`docs/paper_checklist.md`）全部通过
- [ ] broker_paper 连续运行 5 个交易日没有未处理的异常 → `go_live.broker_paper_5_days: true`
- [ ] 要上线的策略按 DESIGN.md 12.5 走完：回测（含成本敏感性）→ 样本外 → 回放 → local_paper → broker_paper，
      结果记在策略说明里 → `go_live.strategy_reviewed: true`

## 2. IBKR 端设置

- [ ] IB Gateway 用**实盘账户**登录，API 端口 4001，取消勾选 Read-Only API，只允许本机（127.0.0.1）连接
- [ ] Gateway → Configure → Settings → API → Precautions：设置单笔数量上限和金额上限（比 `risk.max_order_qty`、
      `risk.max_order_usd` 略大），这是程序风控之外的第二道防线 → `go_live.gateway_precautions: true`
- [ ] 确认实盘账户里可用于这套程序的资金上限（以后改为资金有限的子账户）→ `go_live.account_limit: true`
- [ ] 同一时间不要再运行 broker_paper（逐笔成交额度 5 个是两个账户共用的）

## 3. 发布

```bat
:: 在研究目录里：先提交并打版本标签（例如 V0.0.20），然后
uv run python tools/release_live.py
```

- [ ] 生成了 `..\DayTrader-live\<标签>\`，里面有 `run_live.bat` 和 `config\live.yaml`
- [ ] 打开 `config\live.yaml`：填写 `ibkr.account`（U 开头）；确认风控阈值（模板是最小仓位：单笔 5,000 美元、
      当日亏损 300 美元、不做空）；`strategy` 是要上线的策略，不写 `execution`（= 人工确认）

## 4. 第一次启动

- [ ] 双击 `run_live.bat`：引擎窗口是红色，界面横幅红色“实盘（真实资金）”，窗口标题以“【实盘 真实资金】”开头
- [ ] 第一次会因为“策略源码哈希”拒绝启动，窗口里给出当前的哈希值。确认策略代码就是审阅过的版本后，
      把它填进 `strategy.source_hash`，再双击启动
- [ ] 启动检查全部通过，界面显示“引擎正常 已连接 IB 账户 U…”，策略处于未开启状态
- [ ] 点“开启”。之后每个开仓信号都会弹出来，**15 秒内**点确认才会发单

## 5. 回退

每次发布都是新目录，旧目录保留。出问题时关掉当前的引擎窗口，双击上一个版本目录里的 `run_live.bat`。

## 记录

| 日期 | 版本 | 项目 | 结果 | 备注 |
| --- | --- | --- | --- | --- |
| | | | | |
