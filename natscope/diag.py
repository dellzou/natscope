# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""诊断日志与诊断包。

为什么需要：用户报"检测失败"时，如果没有任何现场材料，唯一能做的猜谜是
"换个网络再试试"。这里把每次运行的关键过程落到滚动日志里，并在需要时把
版本 / 系统 / DPI / 网关 / 最近日志打成一段纯文本，用户可直接复制或另存。

约束：
  * 只用标准库，**不导入 tkinter**（CLI 与无 GUI 环境也要能用）；
  * 任何一步失败都不抛异常——诊断模块自己崩掉比没有诊断更糟；
  * 单文件上限 1 MiB，保留 1 个备份（滚动由 RotatingFileHandler 完成）。
"""

from __future__ import annotations

import logging
import os
import platform
import sys
from logging.handlers import RotatingFileHandler

from . import __license__, __version__

#: 单个日志文件上限
MAX_BYTES = 1024 * 1024
#: 保留几个历史文件
BACKUP_COUNT = 1
#: 日志格式：时间 + 级别 + 模块 + 内容（不含毫秒，省地方）
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)-18s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
#: 诊断包里附带的日志行数
TAIL_LINES = 100

_LOGGER_NAME = "natscope"
_configured = False


def log_dir() -> str:
    """日志目录：``%APPDATA%\\NatScope\\logs``（与配置文件同级）。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "NatScope", "logs")


def log_path() -> str:
    return os.path.join(log_dir(), "natscope.log")


def setup(level: str = "INFO", path: str | None = None) -> logging.Logger:
    """初始化滚动日志并返回包级 logger（幂等，可重复调用）。"""
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    if _configured:
        return logger
    target = path or log_path()
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        handler = RotatingFileHandler(
            target, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
        logger.addHandler(handler)
        _configured = True
    except OSError:
        # 只读用户目录 / 磁盘满：静默降级为"没有日志"，不影响主流程
        logger.addHandler(logging.NullHandler())
        _configured = True
    return logger


def get_logger(name: str = "") -> logging.Logger:
    """取一个挂在 natscope 名下的 logger（会自动继承文件 handler）。"""
    setup()
    return logging.getLogger(f"{_LOGGER_NAME}.{name}" if name else _LOGGER_NAME)


def tail(lines: int = TAIL_LINES, path: str | None = None) -> str:
    """读日志末尾若干行；文件不存在或不可读时返回空串。"""
    try:
        with open(path or log_path(), "r", encoding="utf-8", errors="replace") as f:
            data = f.readlines()
    except OSError:
        return ""
    return "".join(data[-max(1, lines):])


def _dpi() -> str:
    """当前系统 DPI 与缩放比（判断"界面糊"类问题时有用）。"""
    if sys.platform != "win32":
        return "n/a"
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32.dll")
        dpi = int(user32.GetDpiForSystem())
        return f"{dpi} ({dpi / 96.0:.0%})"
    except Exception:  # noqa: BLE001 - 诊断信息不值得抛异常
        return "unknown"


def _gateway_info() -> str:
    try:
        from .gateway import default_gateway

        gw = default_gateway()
        return gw or "未获取到"
    except Exception:  # noqa: BLE001
        return "读取失败"


def diagnostics() -> str:
    """生成诊断包文本：环境 + 网关 + 最近日志。

    纯文本，不含任何网络地址之外的隐私数据；用户可直接贴进 issue。
    """
    lines = [
        "NatScope 诊断信息",
        "=" * 48,
        f"版本     : {__version__}",
        f"许可     : {__license__}",
        f"Python   : {sys.version.split()[0]} ({platform.machine()})",
        f"操作系统 : {platform.platform()}",
        f"DPI      : {_dpi()}",
        f"默认网关 : {_gateway_info()}",
        f"日志文件 : {log_path()}",
        f"配置目录 : {os.path.dirname(log_dir())}",
        "-" * 48,
        f"最近 {TAIL_LINES} 行日志：",
    ]
    body = tail(TAIL_LINES)
    lines.append(body.rstrip() if body else "（无日志，或日志目录不可写）")
    return "\n".join(lines) + "\n"


def save_diagnostics(path: str) -> bool:
    """把诊断包写到指定路径；失败返回 False（不抛异常）。"""
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(diagnostics())
        return True
    except OSError:
        return False


__all__ = [
    "BACKUP_COUNT",
    "MAX_BYTES",
    "TAIL_LINES",
    "diagnostics",
    "get_logger",
    "log_dir",
    "log_path",
    "save_diagnostics",
    "setup",
    "tail",
]
