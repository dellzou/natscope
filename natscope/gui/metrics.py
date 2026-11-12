# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""窗口布局度量：**纯计算**，不 import tkinter，可被 unittest 直接驱动。

为什么单独一层
--------------
v1.0 的窗口尺寸是两处**互相打架**的猜测：

* ``App.__init__`` 里 ``self.minsize(860 * scale, 660 * scale)``；
* ``App._center`` 里 ``h = min(760 * scale, screenheight - 90)``。

两者都不知道任务栏占了多少。1366×768 @125% 的真实算术：

    工作区高度 = 768 - 任务栏(约 35~50px) ≈ 733px
    minsize 高度 = 660 × 1.25 = 825px      → 超出工作区 92px
    ⇒ 窗口底栏（「开始检测」按钮、状态行）被任务栏永久吞掉，
      用户无法点击，且**任何**拖拽都无法露出。

本模块把「屏幕有多大」「窗口该多大」「最小能多小」三件事收进一处，
用 ``work_area()`` 拿真实工作区（排除任务栏），再用 :func:`resolve` 做
三级降级，硬约束是 ``min_h <= work_h - MARGIN``——宁可让内容在
:class:`~natscope.gui.widgets.ScrollFrame` 里滚动，也不让底栏出界。

三级降级
--------
=============  =========================================  ===========================
mode           触发条件                                    表现
=============  =========================================  ===========================
comfortable    设计尺寸（按缩放放大）完整放得下            980×760 布局、pad 22
compact        放不下，但可用区 >= 紧凑下限（820×540）      窗口压到可用区、pad 14、日志默认折叠
scroll         连紧凑下限都放不下                          整页可滚动，pad 12
=============  =========================================  ===========================

所有尺寸单位都是**物理像素**：进程已声明 DPI 感知（``SetProcessDpiAwareness``），
``geometry()`` 与 ``SystemParametersInfoW`` 在同一坐标系里，不换算。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass

# ---- 模式 -----------------------------------------------------------------

MODE_COMFORTABLE = "comfortable"
MODE_COMPACT = "compact"
MODE_SCROLL = "scroll"

MODES = (MODE_COMFORTABLE, MODE_COMPACT, MODE_SCROLL)

# ---- 尺寸阶梯（100% 缩放下的物理像素） ------------------------------------

#: 舒适档设计尺寸：与 v1.0 的 980×760 一致，避免"顺手把窗口改小"
DESIGN = (980, 760)
#: 舒适档最小尺寸（用户可拖到的最小值，100% 缩放）
FLOOR = (860, 660)
#: 紧凑档下限：低于这个可用区就整页滚动，不再硬挤
COMPACT_FLOOR = (820, 540)

#: 工作区四周留白：保证 min ≤ work - 32（每边 16px，抓边框够用了）
MARGIN = 32
#: 紧凑档的缩放上限：1280 宽的屏幕上把窗口放到 2 倍没有意义
COMPACT_SCALE_CAP = 1.25

#: 卡片内边距（100% 缩放点值）
PAD_COMFORTABLE = 22
PAD_COMPACT = 14
PAD_SCROLL = 12
MIN_PAD = 6

#: 窗口尺寸的合法区间（用于 geometry 串校验）
MIN_WINDOW_W = 480
MIN_WINDOW_H = 360
MAX_WINDOW_W = 12000
MAX_WINDOW_H = 12000

#: 非 Windows 且拿不到 tk 根窗口时的保守回退屏幕
DEFAULT_SCREEN = (1280, 800)
#: 回退时假定的任务栏高度（逻辑像素，最后再减）
TASKBAR_FALLBACK = 40

#: ``SystemParametersInfoW(SPI_GETWORKAREA)``
SPI_GETWORKAREA = 0x0030

#: ``[宽]x[高][+x+y]``：位置可省略，符号必须是 ``+`` / ``-``（Tk 的格式）
_GEOMETRY_RE = re.compile(r"^\s*(\d{2,5})x(\d{2,5})(?:([+-]\d{1,5})([+-]\d{1,5}))?\s*$")


@dataclass(frozen=True)
class Layout:
    """一次布局决策的结果。

    ``mode`` 决定内容要不要滚动、日志是否默认折叠；``width`` / ``height`` 是
    启动时的初始窗口尺寸；``min_w`` / ``min_h`` 是 ``minsize()``。后两个字段
    **保证**不超过工作区减 :data:`MARGIN`。
    """

    mode: str
    width: int
    height: int
    min_w: int
    min_h: int
    scale: float
    pad: int

    @property
    def compact(self) -> bool:
        """日志是否默认折叠（紧凑与滚动档都折叠）。"""
        return self.mode != MODE_COMFORTABLE

    @property
    def scrolls(self) -> bool:
        """整页是否需要滚动。"""
        return self.mode == MODE_SCROLL


# --------------------------------------------------------------------------
# 工作区
# --------------------------------------------------------------------------


def work_area() -> tuple[int, int, int, int]:
    """返回主显示器**可用工作区** ``(x, y, w, h)``（已排除任务栏）。

    Windows 走 ``SystemParametersInfoW(SPI_GETWORKAREA)``——这是唯一能正确
    反映"任务栏停靠在哪、多高"的接口（``GetSystemMetrics`` 只给整屏）。
    进程已声明 DPI 感知，因此返回的就是 ``geometry()`` 所用的物理像素。

    非 Windows（或调用失败）时回退成整屏尺寸，并且**不在本模块 import
    tkinter**：如果调用方（GUI）已经在跑，就借它已有的根窗口量屏幕，
    否则用 :data:`DEFAULT_SCREEN`。
    """
    if sys.platform == "win32":
        area = _work_area_win32()
        if area is not None:
            return area
    return _fallback_area()


def _work_area_win32() -> tuple[int, int, int, int] | None:
    """Windows 工作区；任何一步失败都返回 ``None`` 交给调用方回退。"""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # pragma: no cover - 非 CPython 或裁剪运行时的兜底
        return None
    try:
        rect = wintypes.RECT()
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETWORKAREA, 0, ctypes.byref(rect), 0
        )
        if not ok:
            return None
        left = int(rect.left)
        top = int(rect.top)
        width = int(rect.right) - left
        height = int(rect.bottom) - top
        if width <= 0 or height <= 0:
            return None
        return (left, top, width, height)
    except Exception:  # pragma: no cover - 受保护的 API 调用兜底
        return None


def _fallback_area() -> tuple[int, int, int, int]:
    """整屏尺寸回退；顺手减掉一个假定的任务栏高度，避免底栏压到任务栏下。"""
    tk = sys.modules.get("tkinter")
    if tk is not None:  # GUI 已在跑：白借它的根窗口，不额外导入 tkinter
        root = getattr(tk, "_default_root", None)
        if root is not None:
            try:
                width = int(root.winfo_screenwidth())
                height = int(root.winfo_screenheight())
                if width > 0 and height > 0:
                    return (0, 0, width, max(1, height - TASKBAR_FALLBACK))
            except Exception:  # pragma: no cover - 窗口销毁中的竞态
                pass
    width, height = DEFAULT_SCREEN
    return (0, 0, width, max(1, height - TASKBAR_FALLBACK))


# --------------------------------------------------------------------------
# 布局决策
# --------------------------------------------------------------------------


def _clean_scale(scale: float) -> float:
    """缩放夹到 ``[1.0, 3.0]``；NaN/非法值按 1.0 处理。

    低于 1.0 的 scale 只会把窗口缩小到看不全内容，没有收益（Windows 也
    没有 96dpi 以下的比例）；高于 3.0 的取值基本来自度量异常。
    """
    try:
        value = float(scale)
    except (TypeError, ValueError):
        return 1.0
    if value != value or value <= 0.0:  # NaN / 0 / 负数
        return 1.0
    return max(1.0, min(3.0, value))


def _pad(base: int, scale: float) -> int:
    return max(MIN_PAD, int(round(base * scale)))


def resolve(
    work: tuple[int, int, int, int],
    scale: float,
    design: tuple[int, int] = DESIGN,
    floor: tuple[int, int] = FLOOR,
    compact_floor: tuple[int, int] = COMPACT_FLOOR,
) -> Layout:
    """按工作区与缩放算出窗口尺寸、最小尺寸与降级档位。

    纯函数：不读屏幕上任何东西，也不建窗口，因此可以直接用 20 组
    「分辨率 × 缩放」矩阵驱动（见 ``tests/test_layout.py``）。

    算法：
      1. 可用区 = 工作区各减 :data:`MARGIN`（32px），这是硬边界；
      2. 舒适档 = 设计尺寸 × 缩放；两个方向都放得下就是 ``comfortable``；
      3. 否则看可用区是否达到紧凑下限（820×540，缩放最多算 1.25 倍）；
         达到就是 ``compact``——窗口压到可用区大小，内容自己能滚；
      4. 连紧凑下限都放不下就是 ``scroll``：整页滚动，不硬挤；
      5. ``minsize`` 同样夹在可用区内，**从构造上**保证底栏不会被任务栏吞掉。
    """
    work_w = max(1, int(work[2]))
    work_h = max(1, int(work[3]))
    ratio = _clean_scale(scale)

    avail_w = max(1, work_w - MARGIN)
    avail_h = max(1, work_h - MARGIN)

    want_w = int(round(design[0] * ratio))
    want_h = int(round(design[1] * ratio))

    if want_w <= avail_w and want_h <= avail_h:
        mode = MODE_COMFORTABLE
        pad = _pad(PAD_COMFORTABLE, ratio)
    else:
        need_scale = min(ratio, COMPACT_SCALE_CAP)
        need_w = int(round(compact_floor[0] * need_scale))
        need_h = int(round(compact_floor[1] * need_scale))
        if avail_w >= need_w and avail_h >= need_h:
            mode = MODE_COMPACT
            pad = _pad(PAD_COMPACT, ratio)
        else:
            mode = MODE_SCROLL
            pad = _pad(PAD_SCROLL, ratio)

    # 初始尺寸：目标与可用区取小；minsize 同样夹在可用区内
    width = max(1, min(want_w, avail_w))
    height = max(1, min(want_h, avail_h))
    min_w = max(1, min(int(round(floor[0] * ratio)), avail_w))
    min_h = max(1, min(int(round(floor[1] * ratio)), avail_h))

    # want >= floor 恒成立，这里再显式夹一次，杜绝 future 改动破坏不变式
    width = max(min_w, width)
    height = max(min_h, height)

    return Layout(
        mode=mode,
        width=width,
        height=height,
        min_w=min_w,
        min_h=min_h,
        scale=ratio,
        pad=pad,
    )


def centered(layout: Layout, work: tuple[int, int, int, int]) -> str:
    """把窗口居中到工作区，返回可直接交给 ``geometry()`` 的串。"""
    x, y, w, h = (int(v) for v in work[:4])
    px = x + max(0, (w - layout.width) // 2)
    py = y + max(0, (h - layout.height) // 2)
    return f"{layout.width}x{layout.height}{px:+d}{py:+d}"


# --------------------------------------------------------------------------
# 几何串校验
# --------------------------------------------------------------------------


def _work_rects(work) -> list[tuple[int, int, int, int]]:
    """把 ``work`` 归一成矩形列表（支持多显示器：传矩形列表）。"""
    rects: list[tuple[int, int, int, int]] = []
    if work is None:
        return rects
    if isinstance(work, (tuple, list)) and len(work) == 4 and all(
        isinstance(v, (int, float)) for v in work
    ):
        x, y, w, h = (int(v) for v in work)
        if w > 0 and h > 0:
            rects.append((x, y, w, h))
        return rects
    try:
        for item in work:
            x, y, w, h = (int(v) for v in item[:4])
            if w > 0 and h > 0:
                rects.append((x, y, w, h))
    except (TypeError, ValueError):
        return []
    return rects


def clamp_geometry(geo: str, work) -> str | None:
    """校验并修复几何串；不合法返回 ``None``。

    "980x720+120+80" 只有在与某个工作区**相交 ≥40%** 时才算合法——否则
    用户在上次会话拔掉副屏后，窗口会"启动在看不见的地方"，看起来像没启动。

    合法时会顺手修复：

      * 尺寸夹到工作区内（避免比屏幕还大）；
      * 位置平移到完全可见（避免标题栏在屏幕外拖不动）。

    高度因此永远 <= 工作区高度，和 :func:`resolve` 的不变式一致；
    几何串里没有位置信息（``"980x720"``）时只校验尺寸，原样返回。
    """
    if not isinstance(geo, str):
        return None
    match = _GEOMETRY_RE.match(geo)
    if match is None:
        return None

    width = int(match.group(1))
    height = int(match.group(2))
    if not (MIN_WINDOW_W <= width <= MAX_WINDOW_W):
        return None
    if not (MIN_WINDOW_H <= height <= MAX_WINDOW_H):
        return None

    rects = _work_rects(work)
    if not rects:
        return None

    if match.group(3) is None or match.group(4) is None:
        return f"{width}x{height}"

    x = int(match.group(3))
    y = int(match.group(4))

    # 与哪个工作区交得最多，就认定窗口属于哪块屏。
    #
    # 判定口径：**相交面积 ÷ 窗口面积 >= 0.4**。"窗口面积"取"先按工作区尺寸
    # 收缩后的窗口"——比屏幕还大的窗口本来就要缩到工作区（换小屏时窗口必须
    # 能用），不能因为 4000x3000 只有左上角在屏内就判成不可用。
    # 阈值 40% 的含义是"至少能看到小一半"，不是"必须大部分在屏内"：用户把
    # 窗口拖到跨屏位置（一半在这屏一半在那屏）是完全正常的使用方式，
    # 直接丢弃会让用户每次启动都要重新调窗口位置。
    best_rect = None
    best_area = 0
    best_reference = 0
    total_area = 0  # 跨多显示器时按"各屏交集之和"（并集面积，正常屏幕互不重叠）
    for rx, ry, rw, rh in rects:
        probe_w = min(width, rw)
        probe_h = min(height, rh)
        iw = min(x + probe_w, rx + rw) - max(x, rx)
        ih = min(y + probe_h, ry + rh) - max(y, ry)
        if iw <= 0 or ih <= 0:
            continue
        area = iw * ih
        total_area += area
        if area > best_area:
            best_area = area
            best_reference = probe_w * probe_h
            best_rect = (rx, ry, rw, rh)
    if best_rect is None or best_reference <= 0:
        return None
    # 跨屏窗口（一半在这块屏、一半在另一块）按并集面积算，照样能用
    if total_area < 0.4 * best_reference:
        return None

    rx, ry, rw, rh = best_rect
    fixed_w = min(width, rw)
    fixed_h = min(height, rh)
    fixed_x = min(max(x, rx), rx + rw - fixed_w)
    fixed_y = min(max(y, ry), ry + rh - fixed_h)
    return f"{fixed_w}x{fixed_h}{fixed_x:+d}{fixed_y:+d}"


__all__ = [
    "COMPACT_FLOOR",
    "COMPACT_SCALE_CAP",
    "DESIGN",
    "FLOOR",
    "Layout",
    "MARGIN",
    "MODE_COMPACT",
    "MODE_COMFORTABLE",
    "MODE_SCROLL",
    "MODES",
    "clamp_geometry",
    "centered",
    "resolve",
    "work_area",
]
