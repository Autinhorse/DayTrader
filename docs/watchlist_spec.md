# 功能一：定期更新的观察股票池（Watchlist）

## 0. 目标

- 生成一个 200–500 只的观察股票池，作为日内交易的长期观察范围
- 每 20 或 30 个交易日更新一次（参数 `refresh_every_n_days`，默认 20），两次更新之间名单固定
- 名单由两部分组成：
  1. **自动筛选部分**：按证券类别分别过滤和排名
  2. **手工名单**：用户指定、一定要追踪的股票和 ETF，不受过滤规则影响
- 功能二（每日选股）只从这个股票池中挑选
- 所有数值阈值都是默认值，必须放在配置文件中
- 实现前请查阅 Massive 文档索引 https://massive.com/docs/llms.txt ，确认接口、字段名和证券类型代码，以文档为准

## 1. 数据来源

本功能**只用于研究和回测**，数据全部来自 Massive，在本地缓存（Parquet），增量更新。

| 用途 | 建议来源 |
|---|---|
| 全市场每日 OHLCV | 按日获取全市场日 K 线（grouped daily 接口，或 day aggregates Flat Files） |
| 全市场分钟 K 线 | minute aggregates Flat Files（用于计算盘前和开盘成交量均值） |
| 股票列表、证券类型、交易所 | tickers 参考数据接口，**需要包含已退市的股票** |
| 总股本、市值、公司名称、行业 | ticker details 接口（支持按日期查询） |
| 拆股、合股 | splits 参考数据接口 |

- 使用复权后的价格和成交量，或用 splits 数据自行调整
- 实盘和模拟交易使用 IBKR 数据，由功能二处理，本功能不涉及。实盘时直接读取最近一次生成的名单即可

## 2. 时点规则（防止前视偏差和幸存者偏差）

- 第 T 日更新的名单，**只使用 T-1 日收盘及之前的数据**计算
- 名单有效期为 T 日到下一次更新日的前一天
- 每次更新时使用**当时存在**的股票列表，包括之后退市的股票
- 回测时按历史上每个更新日依次生成名单，形成"名单版本表"，记录每个版本的生效区间

## 3. 证券分类

每只证券归入以下类别之一，每个类别有独立的阈值（第 4 节）和独立的数量配额（第 5 节）。

| 类别代码 | 类别 | 示例 | 默认 |
|---|---|---|---|
| `stock` | 普通股 | AAPL、AMD | 启用 |
| `adr` | ADR | TSM、BABA | 启用 |
| `etf_broad` | 宽基指数 ETF | SPY、QQQ、IWM、DIA | 启用 |
| `etf_sector` | 行业 / 主题 ETF | XLK、SMH、XLE、KRE、ARKK | 启用 |
| `etf_macro` | 商品、债券、国际 ETF | GLD、SLV、USO、TLT、FXI、EWZ | 启用 |
| `etf_leveraged` | 杠杆 / 反向 ETF | TQQQ、SQQQ、SOXL、SPXU | 启用 |
| `etp_volatility` | 波动率产品 | UVXY、VXX、SVIX | 不启用 |
| `etn` | 其他 ETN | | 不启用 |
| `etf_other` | 无法自动分类的 ETF | | 启用，使用 `etf_sector` 的阈值 |

始终剔除：权证、权利、单位（units）、优先股、OTC 股票、封闭式基金。主上市交易所限于 NYSE、Nasdaq、NYSE American、NYSE Arca、Cboe BZX。

### 3.1 分类方法
- `stock`、`adr`、`etn`：直接用 Massive 的证券类型代码（如 `CS`、`ADRC`、`ETN`）
- ETF 的细分类别 Massive 基础数据中没有，按以下顺序判断：
  1. **ETF 分类映射表** `etf_categories.csv`（字段：`ticker`、`category`、`underlying`、`leverage_factor`、`note`）：表中有的，以表为准。实现时先预置一批常见 ETF（主要宽基、SPDR 行业系列、主要商品和债券 ETF、主要杠杆 ETF），用户之后可以手工维护
  2. **名称关键词规则**：证券名称包含 2X、3X、-1X、-2X、-3X、Ultra、UltraPro、Bull、Bear、Inverse、Short、Leveraged 等关键词的，归入 `etf_leveraged`；包含 VIX、Volatility 的，归入 `etp_volatility`
  3. 以上都不满足的，归入 `etf_other`
- 每次更新输出"`etf_other` 中成交额排名靠前的 ETF"清单（默认前 50），方便用户补充到映射表

## 4. 各类别过滤阈值

统计窗口默认为过去 **60 个交易日**（参数 `lookback_days`），ATR 使用 **21 日**（参数 `atr_days`）。

通用规则（所有类别）：
- 60 个交易日中有成交的天数 ≥ 57 天
- 已上市至少 60 个交易日
- 成交额 = VWAP × 成交量；无 VWAP 时用收盘价

| 类别 | 最低价格 | 60 日成交额中位数 | 60 日成交量中位数 | ATR% 中位数范围 | 最小 ATR 绝对值 |
|---|---|---|---|---|---|
| `stock` | $10 | ≥ $20M | ≥ 500K 股 | 1.5% – 8% | $0.50 |
| `adr` | $10 | ≥ $20M | ≥ 500K 股 | 1.5% – 8% | $0.50 |
| `etf_broad` | $10 | ≥ $100M | ≥ 1M 股 | ≥ 0.6%，不设上限 | 不设 |
| `etf_sector` | $10 | ≥ $20M | ≥ 300K 股 | ≥ 0.8%，不设上限 | 不设 |
| `etf_macro` | $10 | ≥ $20M | ≥ 300K 股 | ≥ 0.6%，不设上限 | 不设 |
| `etf_leveraged` | $5 | ≥ $50M | ≥ 1M 股 | ≥ 1.5%，不设上限 | 不设 |
| `etp_volatility` | $5 | ≥ $50M | ≥ 1M 股 | 不设 | 不设 |
| `etn` | $10 | ≥ $20M | ≥ 300K 股 | ≥ 0.8% | 不设 |
| `etf_other` | 同 `etf_sector` | | | | |

- 价格使用 60 日收盘价中位数；ATR% = 21 日 ATR / 当日收盘价，取 60 日内的中位数
- 以上每个数值都是配置文件中按类别设置的参数

### 4.1 买卖价差（可选）
- Massive 数据中只有 Stocks Advanced 套餐包含报价数据。有报价数据时启用：常规时段 NBBO 价差中位数 ≤ max($0.02, 价格 × 0.05%)，阈值按类别可配置
- 没有报价数据时跳过，标记 `spread_check = skipped`

### 4.2 同一公司多类股份去重
- 同一公司有多个股份类别时（如 GOOG / GOOGL），只保留 60 日成交额中位数较高的一个
- 判断依据：ticker details 中的公司层级标识字段，以 Massive 文档为准
- 手工名单中的股票不参与去重

## 5. 数量控制

### 5.1 按类别配额
通过过滤的证券在各自类别内按 60 日成交额中位数排名，每个类别最多取：

| 类别 | 默认上限 |
|---|---|
| `stock` | 380 |
| `adr` | 30 |
| `etf_broad` | 10 |
| `etf_sector` | 40 |
| `etf_macro` | 20 |
| `etf_leveraged` | 20 |
| `etp_volatility` | 5 |
| `etn` | 5 |
| `etf_other` | 10 |

- 自动筛选部分的总量上限 `max_size` 默认 500。各类别取完后如果超过 500，按全局成交额排名截断
- 总量少于 `min_size`（默认 200）时**不自动放宽规则**，在输出和日志中给出警告
- 手工名单不计入配额和总量

### 5.2 名单稳定性（参数 `use_hysteresis`，默认开启）
- 上一版名单中的证券，只要仍通过过滤规则，且在本类别内的排名不超过配额的 1.2 倍（`hysteresis_factor`），就保留
- 新证券必须排在本类别配额以内才能进入
- 输出本次新增和移除的列表

## 6. 手工名单（必追踪）

### 6.1 配置文件
单独的配置文件 `manual_watchlist.yaml`，每个条目包含：

| 字段 | 说明 |
|---|---|
| `ticker` | 代码 |
| `category` | 类别（可选，留空则自动分类） |
| `pin_daily` | 是否在功能二中每天固定输出（true / false） |
| `added_on` | 加入日期 |
| `note` | 备注，比如加入原因 |

默认预置：`SPY`、`QQQ`、`IWM`、`DIA`，`pin_daily` 为 true，作为大盘参照。

### 6.2 处理规则
- 手工名单中的证券**无条件进入股票池**，不受第 4 节过滤规则和第 5 节配额限制
- 仍然计算所有指标字段，并额外输出：
  - `would_pass`：如果按正常规则，是否会通过过滤
  - `failed_rules`：未通过的规则列表
  - 方便用户发现手工名单中已经不再活跃的证券
- 手工名单中的证券如果已退市、停牌或缺少数据，给出警告，不中断运行
- **回测中的处理**：手工名单是用户现在的选择，用在历史回测中会带来幸存者偏差（比如事后知道某只股票后来很热门）。提供参数 `manual_in_backtest`：
  - `from_added_on`（默认）：只从 `added_on` 日期之后的名单版本开始包含
  - `always`：所有历史版本都包含（会有偏差，用于对比）
  - `never`：回测中不包含

## 7. 标签字段（不用于过滤，供功能二和后续分析使用）

- `category`：第 3 节的类别
- `is_manual`、`pin_daily`
- `sector`：行业分类（个股和 ADR）
- `underlying`、`leverage_factor`：杠杆 ETF 的标的和倍数（来自映射表）
- `market_cap`、`shares_outstanding`
- `beta_60d`：对 SPY 的 60 日 beta
- `corr_spy_60d`：与 SPY 日收益率的 60 日相关系数
- `next_earnings_date`：有效期内是否有财报（如能获得，否则留空）

## 8. 输出

每次更新输出一个名单版本（Parquet）：

| 字段 | 说明 |
|---|---|
| `version_id` | 版本号或更新日期 |
| `effective_from`、`effective_to` | 生效区间 |
| `ticker` | |
| `category` | |
| `rank_in_category` | 类别内排名（手工名单为空） |
| `median_price_60d` | |
| `median_dollar_vol_60d` | |
| `median_share_vol_60d` | |
| `atr21`、`median_atr21_pct_60d` | |
| `avg_premarket_vol_20d` | 过去 20 日 4:00–9:30 盘前平均成交量（供功能二使用） |
| `avg_open5_vol_20d` | 过去 20 日 9:30–9:35 平均成交量（供功能二使用） |
| `spread_check` | passed / failed / skipped |
| `is_manual`、`would_pass`、`failed_rules` | |
| 第 7 节的标签字段 | |

另外输出：
- 剔除记录：`ticker`、`category`、`failed_rules`
- 变动记录：本次新增、移除的证券
- 汇总：各类别数量、行业分布、手工名单状态
- `etf_other` 中成交额靠前的 ETF 清单（供补充映射表）

另外导出一份简单的 CSV（代码、类别、是否手工），供功能二实盘时读取和导入 IBKR 自选列表。

## 9. 测试

- **前视偏差测试**：删除 T 日及之后的数据后重新生成 T 日版本，结果必须完全一致
- **幸存者偏差检查**：统计每个历史版本中后来退市的证券数量，应大于 0
- **复权检查**：挑选发生过拆股的证券（如 NVDA 2024 年 6 月 10 拆 1），检查指标是否连续
- **分类检查**：抽查各类别的前 20 名，确认分类正确；确认 GOOG / GOOGL 不同时出现
- **手工名单检查**：手工名单中的证券在每个版本都出现（受 `manual_in_backtest` 控制）

## 10. 配置文件结构

- `config.yaml`：通用参数（`refresh_every_n_days`、`lookback_days`、`atr_days`、`min_active_days`、`min_listed_days`、`max_size`、`min_size`、`use_hysteresis`、`hysteresis_factor`、`manual_in_backtest`）和按类别的参数（是否启用、各项阈值、配额、价差阈值）
- `etf_categories.csv`：ETF 分类映射表
- `manual_watchlist.yaml`：手工名单
