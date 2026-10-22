"""图表的指标对话框：从目录添加指标（参数表单由参数模型自动生成）、调整颜色、移除、
设为只显示最后一个交易日（今日）。

目录包含内置指标和 user/indicators/ 下的自定义指标；“重新加载自定义指标”会重新读取文件（热加载）。
"""

from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from trader.gui.forms import ModelForm
from trader.gui.services import IndicatorSpec, Services

# 同一个指标加多次时依次使用的颜色
SPARE_COLORS = ("#e91e63", "#9c27b0", "#00bcd4", "#ffeb3b", "#8bc34a", "#ff9800", "#795548")


class IndicatorDialog(QDialog):
    def __init__(self, sv: Services, current: list[IndicatorSpec], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("指标")
        self.resize(720, 460)
        self.sv = sv
        self.specs = list(current)

        self.catalog = QListWidget()
        self.desc = QLabel("")
        self.desc.setWordWrap(True)
        self.form_holder = QVBoxLayout()
        self.form: ModelForm | None = None
        add = QPushButton("添加 →")
        self.last_day = QCheckBox("只显示最后一个交易日（今日）")
        self.last_day.setToolTip("例如前一日、5 日、15 日的价位只画在今天，便于对比")
        reload_btn = QPushButton("重新加载自定义指标")
        left = QVBoxLayout()
        left.addWidget(QLabel("指标目录（内置 + user/indicators/）"))
        left.addWidget(self.catalog, 1)
        left.addWidget(reload_btn)
        mid = QVBoxLayout()
        mid.addWidget(self.desc)
        mid.addLayout(self.form_holder)
        mid.addStretch()
        mid.addWidget(self.last_day)
        mid.addWidget(add)

        self.current = QListWidget()
        color = QPushButton("颜色…")
        today_btn = QPushButton("今日 开/关")
        remove = QPushButton("移除")
        right = QVBoxLayout()
        right.addWidget(QLabel("图上的指标"))
        right.addWidget(self.current, 1)
        row = QHBoxLayout()
        row.addWidget(color)
        row.addWidget(today_btn)
        row.addWidget(remove)
        right.addLayout(row)

        body = QHBoxLayout()
        body.addLayout(left, 2)
        body.addLayout(mid, 3)
        body.addLayout(right, 2)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(body, 1)
        lay.addWidget(buttons)

        self.catalog.currentRowChanged.connect(self._select)
        add.clicked.connect(self._add)
        remove.clicked.connect(self._remove)
        color.clicked.connect(self._color)
        today_btn.clicked.connect(self._toggle_last_day)
        reload_btn.clicked.connect(self._reload)
        self._fill_catalog()
        self._fill_current()

    def _fill_catalog(self) -> None:
        self.catalog.clear()
        for cls in self.sv.indicator_classes():
            origin = "自定义" if cls.__module__.startswith("user_indicators") else "内置"
            item = QListWidgetItem(f"{cls.name}  [{origin}]")
            item.setData(256, cls.name)
            self.catalog.addItem(item)
        if self.catalog.count():
            self.catalog.setCurrentRow(0)

    def _fill_current(self) -> None:
        self.current.clear()
        for s in self.specs:
            item = QListWidgetItem(s.label)
            if s.color:
                item.setForeground(QColor(s.color))
            self.current.addItem(item)

    def _select(self, row: int) -> None:
        item = self.catalog.item(row)
        if item is None:
            return
        from trader.indicators.base import get_indicator

        cls = get_indicator(item.data(256))
        outs = "、".join(
            f"{o.key}（{o.plot}，{'主图' if o.pane == 'price' else '副图'}）" for o in cls.outputs
        )
        self.desc.setText(f"<b>{cls.name}</b>：{cls.description}<br>输出：{outs}")
        if self.form is not None:
            self.form.setParent(None)  # type: ignore[call-overload]
        self.form = ModelForm(cls.Params)
        self.form_holder.addWidget(self.form)

    def _add(self) -> None:
        item = self.catalog.currentItem()
        if item is None or self.form is None:
            return
        try:
            params = self.form.values()
        except ValueError as e:
            QMessageBox.warning(self, "参数有误", str(e))
            return
        name = item.data(256)
        color = None
        if any(s.name == name for s in self.specs):  # 同一指标加第二个：自动换一种颜色以便区分
            used = {s.color for s in self.specs}
            color = next((c for c in SPARE_COLORS if c not in used), None)
        self.specs.append(
            IndicatorSpec(name, params, color=color, last_day=self.last_day.isChecked())
        )
        self._fill_current()

    def _remove(self) -> None:
        row = self.current.currentRow()
        if 0 <= row < len(self.specs):
            del self.specs[row]
            self._fill_current()

    def _color(self) -> None:
        row = self.current.currentRow()
        if not (0 <= row < len(self.specs)):
            return
        c = QColorDialog.getColor(parent=self)
        if c.isValid():
            self.specs[row].color = c.name()
            self._fill_current()

    def _toggle_last_day(self) -> None:
        row = self.current.currentRow()
        if 0 <= row < len(self.specs):
            self.specs[row].last_day = not self.specs[row].last_day
            self._fill_current()
            self.current.setCurrentRow(row)

    def _reload(self) -> None:
        errors = self.sv.reload_user_code()
        self._fill_catalog()
        bad = {k: v for k, v in errors.items() if not k.startswith("strategies/")}
        if bad:
            QMessageBox.warning(self, "加载失败", "\n".join(f"{k}：{v}" for k, v in bad.items()))

    def result_specs(self) -> list[IndicatorSpec]:
        return self.specs
