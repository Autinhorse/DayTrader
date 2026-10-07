"""表格工具：polars DataFrame → 可排序、可筛选的 Qt 表格（数字按数值排序）。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import polars as pl
from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableView, QWidget

SORT_ROLE = Qt.ItemDataRole.UserRole + 1
ROW_ROLE = Qt.ItemDataRole.UserRole + 2


def fmt_value(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "是" if v else ""
    if isinstance(v, float):
        return f"{v:,.2f}" if abs(v) >= 0.01 or v == 0 else f"{v:.4f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


class FilterProxy(QSortFilterProxyModel):
    def __init__(self) -> None:
        super().__init__()
        self.setSortRole(SORT_ROLE)
        self.predicate: Callable[[dict[str, Any]], bool] | None = None
        self.rows: list[dict[str, Any]] = []

    def filterAcceptsRow(  # noqa: N802
        self, source_row: int, source_parent: QModelIndex | QPersistentModelIndex
    ) -> bool:
        if self.predicate is None or source_row >= len(self.rows):
            return True
        return self.predicate(self.rows[source_row])


class FrameTable(QTableView):
    """显示一个 DataFrame；columns 为 [(列名, 表头)]，formatters 可按列自定义显示文字。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model_ = QStandardItemModel()
        self.proxy = FilterProxy()
        self.proxy.setSourceModel(self.model_)
        self.setModel(self.proxy)
        self.setSortingEnabled(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.setAlternatingRowColors(True)

    def set_frame(
        self,
        df: pl.DataFrame,
        columns: list[tuple[str, str]] | None = None,
        formatters: dict[str, Callable[[Any], str]] | None = None,
        color_col: str | None = None,
    ) -> None:
        columns = columns or [(c, c) for c in df.columns]
        formatters = formatters or {}
        self.model_.clear()
        self.model_.setHorizontalHeaderLabels([h for _, h in columns])
        rows = df.to_dicts()
        self.proxy.rows = rows
        for i, r in enumerate(rows):
            items = []
            for col, _ in columns:
                v = r.get(col)
                it = QStandardItem(formatters[col](v) if col in formatters else fmt_value(v))
                it.setData(
                    v if isinstance(v, (int, float)) else ("" if v is None else str(v)), SORT_ROLE
                )
                it.setData(i, ROW_ROLE)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if color_col and r.get(color_col) is not None and col == color_col:
                    it.setForeground(QColor("#26a69a" if r[color_col] > 0 else "#ef5350"))
                items.append(it)
            self.model_.appendRow(items)
        self.resizeColumnsToContents()

    def set_filter(self, predicate: Callable[[dict[str, Any]], bool] | None) -> None:
        if hasattr(self.proxy, "beginFilterChange"):  # Qt 6.10 起的写法
            self.proxy.beginFilterChange()
            self.proxy.predicate = predicate
            self.proxy.endFilterChange()
        else:
            self.proxy.predicate = predicate
            self.proxy.invalidateFilter()

    def row_at(self, proxy_index: QModelIndex) -> dict[str, Any] | None:
        if not proxy_index.isValid():
            return None
        i = self.proxy.mapToSource(proxy_index).siblingAtColumn(0).data(ROW_ROLE)
        return self.proxy.rows[i] if i is not None else None

    def visible_count(self) -> int:
        return self.proxy.rowCount()
