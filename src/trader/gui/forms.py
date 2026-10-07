"""由 pydantic 参数模型自动生成表单（DESIGN.md 6.3、10.7）。

策略参数、指标参数、撮合与风控假设都用它。

字段类型 → 控件：Literal → 下拉框；bool → 勾选框；int/float/str → 文本框（带说明和范围提示）；
dict/list → 文本框里写 JSON。sweep=True 时数字文本框可以填多个取值（逗号分隔，或 起点:终点:步长），
用于参数扫描。
"""

from __future__ import annotations

import json
import types
import typing
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo
from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QLineEdit, QWidget

from trader.backtest.sweep import parse_values


def _literal_choices(annotation: Any) -> list[Any] | None:
    if get_origin(annotation) is Literal:
        return list(get_args(annotation))
    if isinstance(annotation, typing.TypeAliasType):  # type: ignore[attr-defined]
        return _literal_choices(annotation.__value__)
    return None


def _base_type(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        return _base_type(args[0]) if args else str
    return annotation


def _range_hint(info: FieldInfo) -> str:
    parts = []
    for m in info.metadata:
        for attr, sym in (("ge", "≥"), ("gt", ">"), ("le", "≤"), ("lt", "<")):
            v = getattr(m, attr, None)
            if v is not None:
                parts.append(f"{sym}{v}")
    return " ".join(parts)


class ModelForm(QWidget):
    def __init__(
        self,
        model: type[BaseModel],
        values: dict[str, Any] | None = None,
        sweep: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.model = model
        self.sweep = sweep
        self.widgets: dict[str, QWidget] = {}
        self.kinds: dict[str, str] = {}
        values = values or {}
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        for name, info in model.model_fields.items():
            ann = info.annotation
            default = values.get(name, info.get_default(call_default_factory=True))
            tip = (info.description or "") + (
                f"（{_range_hint(info)}）" if _range_hint(info) else ""
            )
            choices = _literal_choices(ann)
            base = _base_type(ann)
            w: QWidget
            if choices is not None:
                cb = QComboBox()
                for c in choices:
                    cb.addItem(str(c), c)
                if default in choices:
                    cb.setCurrentIndex(choices.index(default))
                w, kind = cb, "choice"
            elif base is bool:
                ck = QCheckBox()
                ck.setChecked(bool(default))
                w, kind = ck, "bool"
            elif base in (int, float, str):
                le = QLineEdit("" if default is None else str(default))
                if sweep and base in (int, float):
                    le.setPlaceholderText("可填多个：5,9,13 或 5:20:5")
                w, kind = le, base.__name__
            else:
                le = QLineEdit(json.dumps(default, ensure_ascii=False))
                tip = (tip + " " if tip else "") + "JSON 格式"
                w, kind = le, "json"
            if tip:
                w.setToolTip(tip)
            self.widgets[name] = w
            self.kinds[name] = kind
            label = name + (
                f"  {info.description}" if info.description and len(info.description) <= 14 else ""
            )
            form.addRow(label, w)

    def _raw(self, name: str) -> Any:
        w, kind = self.widgets[name], self.kinds[name]
        if kind == "choice":
            return w.currentData()  # type: ignore[attr-defined]
        if kind == "bool":
            return w.isChecked()  # type: ignore[attr-defined]
        text = w.text().strip()  # type: ignore[attr-defined]
        if kind == "json":
            return json.loads(text) if text else None
        return text

    def sweep_grid(self) -> dict[str, list[str]]:
        """填了多个取值的数字字段 → {字段: [取值文字...]}。"""
        if not self.sweep:
            return {}
        out = {}
        for name, kind in self.kinds.items():
            if kind in ("int", "float"):
                text = self._raw(name)
                vals = parse_values(text) if text else []
                if len(vals) > 1:
                    out[name] = vals
        return out

    def values(self) -> dict[str, Any]:
        """单一取值（扫描字段取第一个），交给模型校验；校验失败抛 ValueError 并说明哪个字段。"""
        raw: dict[str, Any] = {}
        for name, kind in self.kinds.items():
            v = self._raw(name)
            if kind in ("int", "float", "str") and isinstance(v, str):
                if v == "":
                    continue  # 用默认值
                if kind != "str" and self.sweep:
                    v = parse_values(v)[0]
            raw[name] = v
        try:
            return self.model.model_validate(raw).model_dump()
        except Exception as e:
            raise ValueError(str(e)) from None
