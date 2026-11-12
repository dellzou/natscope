# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""GUI 子包。

``App`` / ``run`` 采用**惰性**导出：``import natscope.gui`` 不会加载 tkinter
（GUI 只在真正需要窗口时才进来）。好处是纯逻辑模块——:mod:`natscope.gui.metrics`
以及所有单元测试——可以在没有 Tk 的环境里导入；否则
``from natscope.gui import metrics`` 会连带把整个 tkinter 拉起来。
"""

from __future__ import annotations

__all__ = ["App", "run"]


def __getattr__(name: str):
    if name in __all__:
        from . import app as _app

        return getattr(_app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
