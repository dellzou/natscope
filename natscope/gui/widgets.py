# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""公共控件：卡片、按钮、信息网格、滚动容器、品牌标记、像素级截断。

造型纪律（三条，改控件前先读）
------------------------------

1. **圆角必须是真几何**。Canvas 没有 ``rounded_rectangle``，常见做法是
   ``create_polygon(smooth=True)``——那是样条拟合，200% 缩放下拐角会鼓成
   毛刺。本模块用 :func:`round_rect_points` 显式计算每段 90° 圆弧上的点
   （默认 6 段/角，弦高误差 < 0.5px），任何缩放下拐角都是稳定圆弧。
2. **不写字面量像素**。所有长度从 ``scale``（当前 DPI / 96）推导，唯一例外是
   :data:`HAIRLINE`——1px 发丝边框本来就该在任何 DPI 下都是 1 物理像素。
3. **字体只传正数点值**。tk 里负数尺寸表示"像素"，会绕过 DPI 缩放，在
   200% 屏上字直接只有一半大。

配色一律由 :class:`natscope.theme.Palette` 传入，本模块不出现任何字面量 hex；
需要"浅色填充"时用 :func:`blend` 在调色板颜色之间插值（Canvas 不支持 alpha）。
"""

from __future__ import annotations

import math
import tkinter as tk
from tkinter import font as tkfont

from .. import brand
from ..theme import Palette, Typography

#: 界面主字体（微软雅黑 UI；"Microsoft YaHei" 在 Win7 上缺字形）
FONT_FAMILY = "Microsoft YaHei UI"
#: 等宽字体（IP:Port、日志、对齐用的列）
MONO_FAMILY = "Consolas"

#: 发丝边框：任何 DPI 下都是 1 物理像素
HAIRLINE = 1

_FONT_CACHE: dict[tuple[str, int, bool], tkfont.Font] = {}


def reset_font_cache() -> None:
    """丢弃字体缓存（Tk 根窗口重建后旧 ``Font`` 对象会失效）。"""
    _FONT_CACHE.clear()


def font(size_pt: int, bold: bool = False, family: str = FONT_FAMILY) -> tkfont.Font:
    """按 (字体族, 点值, 是否粗体) 取缓存的 :class:`tkinter.font.Font`。

    传**正数点值**；``Typography`` 里的阶梯就是点值。缓存是必要的——Canvas
    每帧重绘都会引用字体，反复 new 会持续堆积 Tcl 对象。
    """
    key = (family, int(size_pt), bool(bold))
    got = _FONT_CACHE.get(key)
    if got is None:
        got = tkfont.Font(family=family, size=int(size_pt), weight="bold" if bold else "normal")
        _FONT_CACHE[key] = got
    return got


def as_font(spec) -> tkfont.Font:
    """把 ``Font`` / ``(family, size[, weight])`` / ``int`` 归一成 ``Font``。"""
    if isinstance(spec, tkfont.Font):
        return spec
    if isinstance(spec, (tuple, list)):
        family = str(spec[0]) if len(spec) > 0 else FONT_FAMILY
        size = int(spec[1]) if len(spec) > 1 else Typography.body
        bold = len(spec) > 2 and "bold" in str(spec[2]).lower()
        return font(abs(size), bold=bold, family=family)
    return font(int(spec) if spec else Typography.body)


def px(scale: float, value: float) -> int:
    """把 100% 缩放下的长度换算成当前缩放下的像素（至少 1px）。"""
    return max(0, int(round(float(value) * float(scale))))


def _rgb(color: str) -> tuple[int, int, int]:
    text = color.lstrip("#")
    if len(text) != 6:
        return (0, 0, 0)
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return (0, 0, 0)


def blend(color_a: str, color_b: str, t: float) -> str:
    """两色线性插值：``t=0`` 取 ``color_a``，``t=1`` 取 ``color_b``。

    Canvas 没有 alpha，所有"半透明语义色底"都靠它预先算成实色。
    """
    if t <= 0.0:
        return color_a
    if t >= 1.0:
        return color_b
    ar, ag, ab = _rgb(color_a)
    br, bg, bb = _rgb(color_b)
    return "#{:02X}{:02X}{:02X}".format(
        int(round(ar + (br - ar) * t)),
        int(round(ag + (bg - ag) * t)),
        int(round(ab + (bb - ab) * t)),
    )


#: 语义色调 -> 调色板字段名。``viz`` 只给语义（ok/warn/bad/na），
#: 具体颜色在这里落地，换主题自动跟随。
_TONE_FIELD: dict[str, str] = {
    "ok": "ok",
    "warn": "warn",
    "bad": "err",
    "err": "err",
    "na": "text_dim",
    "dim": "text_faint",
    "accent": "accent",
}


def tone_color(palette: Palette, tone: str) -> str:
    """语义色调 -> 具体颜色（未知色调回落到 ``text_dim``）。"""
    return getattr(palette, _TONE_FIELD.get(tone, "text_dim"))


def tone_fill(palette: Palette, tone: str, strength: float = 0.14, base: str = "") -> str:
    """语义色调的浅色填充（用于格子/胶囊底色）。"""
    return blend(base or palette.surface, tone_color(palette, tone), strength)


def round_rect_points(
    x0: float, y0: float, x1: float, y1: float, radius: float, steps: int = 6
) -> list[float]:
    """圆角矩形的**显式**顶点序列，可直接喂给 ``create_polygon``。

    与 ``smooth=True`` 的区别：这里每个角都是真实的 90° 圆弧采样点，
    拐角曲率连续，缩放不会鼓包。``radius`` 会被夹到边长的一半以内。
    """
    radius = max(0.0, min(float(radius), (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    if radius < 0.5:  # 太小的圆角看起来就是直角，别浪费顶点
        return [x0, y0, x1, y0, x1, y1, x0, y1]
    steps = max(1, int(steps))
    points: list[float] = []
    # 顺时针：右上 -> 右下 -> 左下 -> 左上（起始角分别是 -90/0/90/180）
    corners = (
        (x1 - radius, y0 + radius, -90.0),
        (x1 - radius, y1 - radius, 0.0),
        (x0 + radius, y1 - radius, 90.0),
        (x0 + radius, y0 + radius, 180.0),
    )
    for cx, cy, start in corners:
        for i in range(steps + 1):
            angle = math.radians(start + 90.0 * i / steps)
            points.append(cx + radius * math.cos(angle))
            points.append(cy + radius * math.sin(angle))
    return points


def ellipsis(text: str, fnt, max_px: int) -> str:
    """像素级截断加省略号。

    ``report.py`` 里是按字符数截断的（Pillow 侧另有实现）——中英混排时中文
    是双宽，按字符数对齐/截断必然错位。这里的量尺是真字体度量。
    """
    if max_px <= 0:
        return ""
    measure = as_font(fnt)
    source = "" if text is None else str(text)
    if measure.measure(source) <= max_px:
        return source
    marker = "…"
    budget = max_px - measure.measure(marker)
    if budget <= 0:
        return marker
    out: list[str] = []
    used = 0
    for char in source:
        width = measure.measure(char)
        if used + width > budget:
            break
        out.append(char)
        used += width
    return "".join(out).rstrip() + marker


def pad_to_px(text: str, fnt, target_px: int, filler: str = " ") -> str:
    """用空格把 ``text`` 撑到至少 ``target_px`` 像素宽（清单列对齐用）。

    Listbox 只有一列，中文标签是双宽，``f"{key:<34}"`` 这类字符级对齐必然
    错位；按像素补空格则与字体、DPI 都无关。
    """
    measure = as_font(fnt)
    out = "" if text is None else str(text)
    space = max(1, measure.measure(filler))
    guard = 0
    while measure.measure(out) < target_px and guard < 4096:
        out += filler
        guard += 1
    return out


def hairline(parent: tk.Misc, palette: Palette, orient: str = "h") -> tk.Frame:
    """1px 发丝分隔线（``orient`` 为 ``h`` 横线、``v`` 竖线）。"""
    if orient == "v":
        return tk.Frame(parent, bg=palette.border, width=HAIRLINE)
    return tk.Frame(parent, bg=palette.border, height=HAIRLINE)


def elevation(
    parent: tk.Misc, palette: Palette, scale: float, rows: int = 0
) -> tk.Frame:
    """固定栏的"抬起"效果：几条 1px 横线，从上到下由 ``bg`` 渐变到 ``border``。

    用途是底栏：固定栏与滚动内容同为 ``surface`` 时，1px 描边在缩略图/低分屏
    上几乎看不见，滚动的内容（白色卡片）会和白色底栏糊成一片，读起来像"按钮
    盖住了内容"。渐变阴影把两者分开，且它是**实色叠加**——Canvas 没有 alpha，
    这里靠 :func:`blend` 预先算好每一行的颜色，任何 DPI 下都不会糊。
    """
    count = rows or max(2, px(scale, 3))
    box = tk.Frame(parent, bg=palette.bg, bd=0, highlightthickness=0)
    # 最上面那一行最浅、贴着底栏那一行最深：视线自然落到底栏边缘
    for index in range(count):
        ratio = 0.9 * (index + 1) / float(count)
        tk.Frame(
            box, bg=blend(palette.bg, palette.border, ratio), height=HAIRLINE
        ).pack(side="top", fill="x")
    return box


def _parent_bg(widget: tk.Misc, fallback: str) -> str:
    try:
        return str(widget.cget("bg"))
    except tk.TclError:
        return fallback


# --------------------------------------------------------------------------
# 卡片
# --------------------------------------------------------------------------


class Card(tk.Frame):
    """直角卡片：1px 边框 + 可选左侧 4px 语义色条。

    结构：``Card``（边框）→ 可选 ``accent`` 色条 → ``self.body``（内容容器）。
    所有子控件都应放进 ``self.body``，否则会被色条挤到左边。
    """

    def __init__(
        self,
        parent: tk.Misc,
        palette: Palette,
        scale: float,
        *,
        accent: str = "",
        accent_width: float = 4.0,
        surface: str = "",
        **kwargs,
    ):
        self.palette = palette
        self.scale = float(scale)
        self._surface = surface or palette.surface
        super().__init__(
            parent,
            bg=self._surface,
            bd=0,
            highlightthickness=HAIRLINE,
            highlightbackground=palette.border,
            highlightcolor=palette.border,
            **kwargs,
        )
        self._accent_bar: tk.Frame | None = None
        if accent:
            self._accent_bar = tk.Frame(
                self, bg=accent, width=px(self.scale, accent_width)
            )
            self._accent_bar.pack(side="left", fill="y")
            self._accent_bar.pack_propagate(False)
        self.body = tk.Frame(self, bg=self._surface)
        self.body.pack(side="left", fill="both", expand=True)

    def set_accent(self, color: str) -> None:
        """更新左侧色条颜色（结论色随检测结果变化）。"""
        if self._accent_bar is not None:
            self._accent_bar.configure(bg=color)


# --------------------------------------------------------------------------
# 按钮
# --------------------------------------------------------------------------


class PillButton(tk.Canvas):
    """Canvas 绘制的按钮：真圆弧 + hover/press/disabled 三态。

    为什么不用 ``tk.Button``：Windows 上 ``tk.Button`` 无法控制圆角，也无法
    保证 ``padx/pady`` 与字体度量一致，高 DPI 下内容会贴边。这里自己量字体
    算尺寸，圆角半径 = 高度的一半（真胶囊）。

    兼容旧调用：``.configure(text=..., state=...)`` 与 ``tk.Button`` 同名同义，
    因此 ``app.py`` 里"检测中… / 开始检测"的切换代码不用改写。
    """

    def __init__(
        self,
        parent: tk.Misc,
        palette: Palette,
        scale: float,
        text: str,
        command=None,
        *,
        variant: str = "primary",
        pad_x: float = 16.0,
        pad_y: float = 7.0,
        min_width: float = 0.0,
    ):
        self.palette = palette
        self.scale = float(scale)
        self._variant = variant
        self._text = str(text)
        self._command = command
        self._state = "normal"
        self._hover = False
        self._pressed = False
        self._pad_x = float(pad_x)
        self._pad_y = float(pad_y)
        self._min_width = float(min_width)
        self._label_font = font(Typography.body, bold=(variant == "primary"))
        super().__init__(
            parent,
            bg=_parent_bg(parent, palette.surface),
            highlightthickness=0,
            bd=0,
            takefocus=0,
            cursor="hand2",
        )
        self._width_px = 0
        self._height_px = 0
        self._measure()
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._redraw()

    # ---- 尺寸与绘制 ----

    def _measure(self) -> None:
        height = self._label_font.metrics("linespace") + 2 * px(self.scale, self._pad_y)
        width = self._label_font.measure(self._text) + 2 * px(self.scale, self._pad_x)
        width = max(width, px(self.scale, self._min_width))
        self._width_px = max(px(self.scale, 24), width)
        self._height_px = max(px(self.scale, 18), height)
        super().configure(width=self._width_px, height=self._height_px)

    def _colors(self) -> tuple[str, str, str]:
        """(填充, 文字, 描边)。"""
        palette = self.palette
        if self._state == "disabled":
            return palette.surface_alt, palette.text_faint, palette.border
        if self._variant == "primary":
            fill = palette.accent
            if self._pressed:
                fill = blend(palette.accent, palette.text, 0.30)
            elif self._hover:
                fill = blend(palette.accent, palette.text, 0.14)
            return fill, palette.accent_text, blend(palette.accent, palette.text, 0.35)
        fill = palette.surface_alt
        if self._pressed:
            fill = blend(palette.surface_alt, palette.text, 0.18)
        elif self._hover:
            fill = palette.border
        return fill, palette.text, palette.border

    def _redraw(self) -> None:
        self.delete("all")
        fill, ink, outline = self._colors()
        width, height = self._width_px, self._height_px
        if self._variant == "primary":
            radius = height / 2.0
        else:
            radius = min(px(self.scale, 6), height / 2.0)
        self.create_polygon(
            round_rect_points(0.5, 0.5, width - 0.5, height - 0.5, radius),
            fill=fill,
            outline=outline,
            width=HAIRLINE,
        )
        self.create_text(
            width / 2.0, height / 2.0, text=self._text, fill=ink,
            font=self._label_font, anchor="center",
        )

    # ---- 事件 ----

    def _on_enter(self, _event=None) -> None:
        if self._state == "disabled":
            return
        self._hover = True
        self._redraw()

    def _on_leave(self, _event=None) -> None:
        self._hover = False
        self._pressed = False
        self._redraw()

    def _on_press(self, _event=None) -> None:
        if self._state == "disabled":
            return
        self._pressed = True
        self._redraw()

    def _on_release(self, event) -> None:
        if self._state == "disabled":
            return
        was_pressed = self._pressed
        self._pressed = False
        self._redraw()
        inside = 0 <= int(event.x) < self._width_px and 0 <= int(event.y) < self._height_px
        if was_pressed and inside:
            self.invoke()

    # ---- 接口 ----

    def invoke(self) -> None:
        """触发命令（disabled 时什么也不做）。"""
        if self._state == "disabled":
            return
        if callable(self._command):
            self._command()

    def set_text(self, text: str) -> None:
        self.configure(text=text)

    def set_state(self, state: str) -> None:
        self.configure(state=state)

    def configure(self, cnf=None, **kwargs):
        """兼容 ``tk.Button`` 的 ``text`` / ``state`` / ``command``。"""
        dirty = False
        if "text" in kwargs:
            self._text = str(kwargs.pop("text"))
            dirty = True
        if "state" in kwargs:
            self._state = "disabled" if str(kwargs.pop("state")) == "disabled" else "normal"
            dirty = True
        if "command" in kwargs:
            self._command = kwargs.pop("command")
        if dirty:
            super().configure(cursor="arrow" if self._state == "disabled" else "hand2")
            self._measure()
            self._redraw()
        if kwargs or cnf:
            return super().configure(cnf, **kwargs)
        return None

    config = configure  # tk.Button 的别名，保持一致


class GhostButton(PillButton):
    """次级按钮：浅底 + 1px 边框 + hover 变深。"""

    def __init__(self, parent, palette, scale, text, command=None, **kwargs):
        kwargs.setdefault("pad_x", 12.0)
        kwargs.setdefault("pad_y", 5.5)
        super().__init__(parent, palette, scale, text, command, variant="ghost", **kwargs)


# --------------------------------------------------------------------------
# 信息网格
# --------------------------------------------------------------------------


class InfoGrid(tk.Frame):
    """键值信息网格：多列排布，值按**像素**截断（长 IP 不会顶开列宽）。"""

    def __init__(
        self,
        parent: tk.Misc,
        palette: Palette,
        scale: float,
        *,
        columns: int = 2,
        label_width: int = 11,
        surface: str = "",
    ):
        self.palette = palette
        self.scale = float(scale)
        self._surface = surface or palette.surface
        super().__init__(parent, bg=self._surface)
        self._columns = max(1, int(columns))
        self._label_font = font(Typography.label)
        self._value_font = font(Typography.body, family=MONO_FAMILY)
        self._label_width = max(2, int(label_width))
        self._values: dict[str, tk.Label] = {}
        self._full: dict[str, str] = {}
        self._slot = 0
        for col in range(self._columns):
            self.columnconfigure(col * 2, weight=0)
            self.columnconfigure(col * 2 + 1, weight=1, uniform="info")

    def add(self, key: str, label: str, value: str = "—") -> tk.Label:
        """追加一行（字段顺序即 ``_slot`` 递增的顺序，两列时先左后右）。"""
        col = self._slot % self._columns
        row = self._slot // self._columns
        self._slot += 1
        tk.Label(
            self,
            text=label,
            font=self._label_font,
            fg=self.palette.text_faint,
            bg=self._surface,
            anchor="w",
            width=self._label_width,
        ).grid(
            row=row, column=col * 2, sticky="w",
            padx=(0, px(self.scale, 6)), pady=px(self.scale, 2),
        )
        value_label = tk.Label(
            self,
            text=value,
            font=self._value_font,
            fg=self.palette.text,
            bg=self._surface,
            anchor="w",
        )
        value_label.grid(
            row=row, column=col * 2 + 1, sticky="we",
            padx=(0, px(self.scale, 14)), pady=px(self.scale, 2),
        )
        self._values[key] = value_label
        self._full[key] = str(value)
        value_label.bind("<Configure>", lambda _event, k=key: self._fit(k))
        return value_label

    def set(self, key: str, value) -> None:
        """更新某项的值（按可用宽度截断）。"""
        self._full[key] = "—" if value is None else str(value)
        if key in self._values:
            self._fit(key)

    def set_many(self, values: dict) -> None:
        for key, value in values.items():
            self.set(key, value)

    def values(self) -> dict[str, str]:
        """当前完整（未截断）值，便于测试与导出。"""
        return dict(self._full)

    def _fit(self, key: str) -> None:
        label = self._values[key]
        available = label.winfo_width()
        if available <= 1:
            return
        limit = max(0, available - px(self.scale, 2))
        text = ellipsis(self._full[key], self._value_font, limit)
        if str(label.cget("text")) != text:
            label.configure(text=text)


# --------------------------------------------------------------------------
# 滚动容器
# --------------------------------------------------------------------------


class ScrollFrame(tk.Frame):
    """Canvas + 内嵌 Frame：内容超出视口时出现滚动条。

    用 ``scroll`` 降级档的整页滚动，也用于结果区。鼠标滚轮只在指针进入本
    控件时绑定（``<Enter>``/``<Leave>``），不污染其它控件。
    """

    def __init__(
        self,
        parent: tk.Misc,
        palette: Palette,
        scale: float,
        *,
        background: str = "",
        height: float = 0.0,
    ):
        self.palette = palette
        self.scale = float(scale)
        self._bg = background or palette.bg
        super().__init__(parent, bg=self._bg, bd=0, highlightthickness=0)
        self.canvas = tk.Canvas(
            self,
            bg=self._bg,
            highlightthickness=0,
            bd=0,
            takefocus=0,
            height=max(1, px(self.scale, height)) if height else px(self.scale, 160),
        )
        self.canvas.pack(side="left", fill="both", expand=True)
        self.bar = tk.Scrollbar(
            self,
            orient="vertical",
            command=self.canvas.yview,
            width=px(self.scale, 10),
            bg=palette.surface_alt,
            troughcolor=self._bg,
            activebackground=palette.border,
            relief="flat",
            bd=0,
            highlightthickness=0,
        )
        self.canvas.configure(yscrollcommand=self._on_view_change)
        self.inner = tk.Frame(self.canvas, bg=self._bg)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self._bar_visible = False
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", self._grab_wheel)
        self.canvas.bind("<Leave>", self._release_wheel)

    # ---- 内部 ----

    def _on_inner_configure(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._sync_bar()

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=max(1, int(event.width)))
        self._sync_bar()

    def _sync_bar(self) -> None:
        need = self.inner.winfo_reqheight() > self.canvas.winfo_height() + 1
        if need == self._bar_visible:
            return
        self._bar_visible = need
        if need:
            self.bar.pack(side="right", fill="y")
        else:
            self.bar.pack_forget()
            self.canvas.yview_moveto(0.0)

    def _on_view_change(self, first, last) -> None:
        if self._bar_visible:
            self.bar.set(first, last)

    def _grab_wheel(self, _event=None) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _release_wheel(self, _event=None) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event):
        if not self._bar_visible:
            return None
        self.canvas.yview_scroll(-1 if int(getattr(event, "delta", 0)) > 0 else 1, "units")
        return "break"

    # ---- 接口 ----

    @property
    def scrolling(self) -> bool:
        """当前是否真的出现了滚动条（测试与自查用）。"""
        return self._bar_visible

    def scroll_to_top(self) -> None:
        self.canvas.yview_moveto(0.0)


# --------------------------------------------------------------------------
# 品牌标记
# --------------------------------------------------------------------------


class LogoMark(tk.Canvas):
    """用 :data:`natscope.brand.MARK_GEOM` 在 Canvas 上画品牌标记。

    几何与 ``assets/icon.ico`` 同源（``brand.layout()``），所以界面上的标记
    与任务栏图标不会"各画一套"。
    """

    def __init__(
        self,
        parent: tk.Misc,
        palette: Palette,
        scale: float,
        size: float = 28.0,
        *,
        background: str = "",
        tile: bool = True,
    ):
        self.palette = palette
        self.scale = float(scale)
        self._size = max(12, px(self.scale, size))
        super().__init__(
            parent,
            width=self._size,
            height=self._size,
            bg=background or _parent_bg(parent, palette.surface),
            highlightthickness=0,
            bd=0,
            takefocus=0,
        )
        self._tile = tile
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        size = float(self._size)
        variant = brand.pick_variant(int(size))
        geometry = brand.layout(variant)
        white = brand.hex_color(brand.BRAND_WHITE)
        accent = brand.hex_color(brand.BRAND_ACCENT)

        if self._tile:
            radius = float(geometry["corner_radius"]) * size
            self.create_polygon(
                round_rect_points(0.5, 0.5, size - 0.5, size - 0.5, radius, steps=4),
                fill=brand.hex_color(brand.BRAND_DARK),
                outline="",
            )

        ring_cx, ring_cy = geometry["ring_center"]
        ring_r = float(geometry["ring_outer_r"]) * size
        ring_w = max(1, int(round(float(geometry["ring_width"]) * size)))
        # Tk 的 create_arc 角度是"逆时针为正、0° 在 3 点钟"，与 brand 的
        # PIL 约定（y 轴向下、顺时针为正）符号相反，这里取负号转换。
        start = -float(geometry["arc_start_deg"])
        extent = -(float(geometry["arc_end_deg"]) - float(geometry["arc_start_deg"]))
        self.create_arc(
            ring_cx * size - ring_r,
            ring_cy * size - ring_r,
            ring_cx * size + ring_r,
            ring_cy * size + ring_r,
            start=start,
            extent=extent,
            style="arc",
            outline=white,
            width=ring_w,
        )

        rod = geometry["rod"]
        if rod:
            flat: list[float] = []
            for point in rod:
                flat.append(point[0] * size)
                flat.append(point[1] * size)
            self.create_polygon(flat, fill=white, outline="")

        ball_x, ball_y = geometry["ball_center"]
        ball_r = float(geometry["ball_r"]) * size
        self.create_oval(
            ball_x * size - ball_r,
            ball_y * size - ball_r,
            ball_x * size + ball_r,
            ball_y * size + ball_r,
            fill=accent,
            outline="",
        )


__all__ = [
    "Card",
    "FONT_FAMILY",
    "GhostButton",
    "HAIRLINE",
    "InfoGrid",
    "LogoMark",
    "MONO_FAMILY",
    "PillButton",
    "ScrollFrame",
    "as_font",
    "blend",
    "ellipsis",
    "font",
    "hairline",
    "pad_to_px",
    "px",
    "reset_font_cache",
    "round_rect_points",
    "tone_color",
    "tone_fill",
]
