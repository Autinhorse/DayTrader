# 0005 界面改为 PySide6 桌面程序

日期：2026-10-07　状态：已采纳

## 背景

DESIGN.md 第 3.1、11 节原定界面为本地 Web 应用（FastAPI 后端 + React/TypeScript/Vite 前端 + Lightweight Charts）。
用户希望使用 Windows 窗口，尤其是**图表窗口可以拖出来单独放置**（例如放到第二块显示器），并按需要打开多个图表。

## 选项

1. 网页：页面内可以分格、拖动，也可以弹出新的浏览器窗口，但弹出的仍是浏览器窗口；需要另一套技术栈（Node、TypeScript）。
2. PySide6 桌面程序：主窗口 + 可停靠面板，面板可以拖出成独立窗口、放到其他显示器、再拖回停靠；布局由 Qt 保存恢复；
   全部是 Python，直接调用现有模块。图表在 QWebEngineView 里运行 Lightweight Charts（本地文件，不联网）。

## 结论

选 2。

- 研究版 `trader-research` 是一个 PySide6 桌面程序（`trader.gui`）。图表、回测表单、结果、交易明细、实验列表、
  数据管理都是可停靠面板；图表可以开任意多个，每个独立设置标的、周期、时段和指标。窗口布局和打开的图表在退出时保存、启动时恢复。
- 图表用 Lightweight Charts 5（Apache-2.0，`src/trader/gui/assets/`），保留 TradingView 署名标志。Python 与图表之间用
  QWebChannel 通信。指标绘制仍完全由 `OutputSpec` 驱动（line / histogram / band / marker / hline）。
- 研究版不再需要 REST/WebSocket 服务：界面直接调用 `trader.data`、`trader.indicators`、`trader.backtest`。
  回测和参数扫描在独立的子进程里运行（`python -m trader.backtest.worker`），不阻塞界面，可以取消。
- 实盘版（阶段 6 以后）同样用 PySide6 界面，但交易引擎是独立进程，界面通过本机进程间通信（只监听 127.0.0.1）显示和操作；
  界面关闭不影响引擎。具体协议在阶段 6 决定。原第 11.1 节的接口清单作为该协议的功能清单保留。
- 新增依赖：PySide6 从工具组移到研究组；开发组加 pytest-qt（界面测试）。不再需要 FastAPI、Node 和前端工程（`web/` 目录删除）。
