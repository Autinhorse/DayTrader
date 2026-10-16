"""日内波动与震荡强度报表（docs/intraday_volatility_report_spec.md，决策 0009）。只属于研究版。

    from trader.analytics import ReportParams, build_report

    rep = build_report(["NVDA", "AMD"], "2026-07-01", "2026-09-30")
    rep.ranking; rep.daily; rep.swings      # pandas DataFrame

计算逻辑在本包，notebook（user/notebooks/日内波动报表.ipynb）只设置参数和展示结果。
"""

from trader.analytics.params import ReportParams, ksuffix
from trader.analytics.pipeline import VolReport, build_report

__all__ = ["ReportParams", "VolReport", "build_report", "ksuffix"]
