# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""结果可视化：把 :mod:`natscope.viz` 的数据模型画到 Canvas 上。

分工（v1.1 起强制）
------------------
* :mod:`natscope.viz` 决定**该画什么**（格子状态、语义色调、样本分组、结论文案）；
* 本模块只决定**放在哪、什么颜色**，不重新推导任何结论。

五个视图
--------
1. :class:`MatrixView` 行为矩阵 3×3：行=映射(EIM/ADM/APDM)、列=过滤(EIF/ADF/APDF)。
   结论格 2px 强调边框 + 语义色填充；不可细分的锥型整行 ``related``；
   ``open`` / ``blocked`` 全格 ``na`` 并浮层说明原因。
2. :class:`SpectrumView` 类型谱：从开放到阻断的横向色带，当前类型上方三角标记。
   ``cone_unknown`` 时三个锥型段一起变成 ``candidate``——把"不确定"画出来。
3. :class:`SamplesView` 映射样本：每条样本一个 chip，映射端口相同的同底色 +
   底部括线归组，让 EIM（端口不变）一眼可见。
4. :class:`ScenariosView` / :class:`GatewayStrip` 场景徽章 + 网关端口映射徽标。
5. :class:`StepsView` 步骤时间线：替代埋在日志里的 Test I/II/III。

渲染纪律
--------
* ``<Configure>`` 里用 ``after_idle`` 合并重绘，并比对 ``(w, h, palette.name)``
  三元组，三元组不变就不重画（拖拽改窗口大小时 Canvas 每秒能触发几十次）；
* 字体统一 ``tkfont.Font(family=FONT, size=pt)``，**只传正数点值**；
* 宽度不足 :data:`COLLAPSE_WIDTH` 时降级成单行文本，不硬挤；
* 步骤状态必须覆盖 ``ok / fail / skip / warn / pending`` 五种——``warn`` 是
  映射端口不一致时新增的状态，缺了会被渲染成中性色，误读成"没问题"；
* 颜色只从 :class:`~natscope.theme.Palette` 取，本文件不出现字面量 hex。
"""

from __future__ import annotations

import tkinter as tk

from .. import viz
from ..nat import NatResult, TestStep
from ..theme import Palette, Typography
from .widgets import (
    HAIRLINE,
    FONT_FAMILY,
    MONO_FAMILY,
    blend,
    ellipsis,
    font,
    px,
    round_rect_points,
    tone_color,
    tone_fill,
)

#: 结果区宽度低于该值（100% 缩放下）时降级为单行文本摘要
COLLAPSE_WIDTH = 720

#: 步骤状态 -> (图标, Palette 字段)。五种状态缺一不可。
STEP_STYLE: dict[str, tuple[str, str]] = {
    "ok": ("√", "ok"),
    "fail": ("×", "err"),
    "skip": ("-", "text_dim"),
    "warn": ("!", "warn"),
    "pending": ("·", "text_faint"),
}
#: 未知状态按 pending 渲染（宁可显示"未完成"，不能假装成功）
STEP_FALLBACK = STEP_STYLE["pending"]


def status_style(status: str) -> tuple[str, str]:
    """状态 -> (图标, Palette 字段)；未登记的状态回落到 pending。"""
    return STEP_STYLE.get(str(status or "").lower(), STEP_FALLBACK)


def _wrap(text: str, fnt, max_px: int, max_lines: int = 2) -> list[str]:
    """按像素宽度硬折行（只用于浮层里的短说明，最多 ``max_lines`` 行）。"""
    source = "" if text is None else str(text)
    if max_px <= 0 or not source:
        return []
    lines: list[str] = []
    current = ""
    consumed = 0
    for char in source:
        if current and fnt.measure(current + char) > max_px:
            lines.append(current)
            consumed += len(current)
            current = ""
            if len(lines) >= max_lines:
                return lines
        current += char
    if current:
        if len(lines) >= max_lines:
            return lines
        lines.append(current)
        consumed += len(current)
    if consumed < len(source) and lines:
        # 还有没画完的文字：最后一行补省略号，明确告诉用户"后面还有"
        remaining = source[consumed:]
        lines[-1] = ellipsis(lines[-1] + remaining, fnt, max_px)
    return lines


class CanvasView(tk.Canvas):
    """Canvas 子视图基类：防抖重绘 + 尺寸/主题缓存。

    子类只需实现 :meth:`_draw`；数据变化时调 :meth:`invalidate`。
    """

    #: 100% 缩放下的默认高度，子类覆盖
    DEFAULT_HEIGHT = 60.0

    def __init__(
        self,
        parent: tk.Misc,
        palette: Palette,
        scale: float,
        *,
        height: float = 0.0,
        surface: str = "",
        thumb: bool = False,
    ):
        self.palette = palette
        self.scale = float(scale)
        self._surface = surface or palette.surface
        self._thumb = bool(thumb)
        self._signature: tuple[int, int, str] | None = None
        self._pending: str | None = None
        super().__init__(
            parent,
            bg=self._surface,
            highlightthickness=0,
            bd=0,
            takefocus=0,
            height=max(1, px(self.scale, height or self.DEFAULT_HEIGHT)),
        )
        self.bind("<Configure>", self._on_configure)
        self.bind("<Destroy>", self._on_destroy)

    # ---- 刷新管线 ----

    def invalidate(self) -> None:
        """数据变了：丢掉签名并请求一次重绘。"""
        self._signature = None
        self._request()

    def set_palette(self, palette: Palette) -> None:
        self.palette = palette
        self.invalidate()

    def _on_configure(self, _event=None) -> None:
        self._request()

    def _on_destroy(self, _event=None) -> None:
        if self._pending is not None:
            try:
                self.after_cancel(self._pending)
            except (tk.TclError, ValueError):
                pass
            self._pending = None

    def _request(self) -> None:
        if self._pending is not None:
            return
        try:
            self._pending = self.after_idle(self._flush)
        except tk.TclError:  # 窗口已在销毁
            self._pending = None

    def _flush(self) -> None:
        self._pending = None
        try:
            width = int(self.winfo_width())
            height = int(self.winfo_height())
        except tk.TclError:
            return
        signature = (width, height, self.palette.name)
        if signature == self._signature:
            return
        self._signature = signature
        self.delete("all")
        if width <= 1 or height <= 1:
            return
        self._draw(width, height)

    # ---- 子类实现 ----

    def _draw(self, width: int, height: int) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError

    # ---- 绘制工具 ----

    def _collapsed(self, width: int) -> bool:
        return width < px(self.scale, COLLAPSE_WIDTH)

    def _ink(self, field: str) -> str:
        return str(getattr(self.palette, field, self.palette.text))

    def _label(
        self,
        x: float,
        y: float,
        text: str,
        *,
        size: int = Typography.body,
        field: str = "text",
        bold: bool = False,
        anchor: str = "nw",
        family: str = FONT_FAMILY,
        limit: int = 0,
    ):
        """画一段文字（超宽按像素截断）。"""
        fnt = font(size, bold=bold, family=family)
        shown = ellipsis(text, fnt, limit) if limit else str(text)
        return self.create_text(
            x, y, text=shown, fill=self._ink(field), font=fnt, anchor=anchor
        )

    def _empty(self, width: int, height: int, text: str = "尚未检测") -> None:
        self._label(width / 2.0, height / 2.0, text, field="text_faint", anchor="center")

    @staticmethod
    def _stack(top: float, fonts, gap: float = 0.0) -> tuple[float, list[float]]:
        """按**字体度量**把若干行文字纵向堆叠，返回 ``(总高, 各行中心 y)``。

        为什么不用 ``cy ± px(7)`` 这类固定偏移：文字行高来自字体度量
        （``linespace``），它随缩放非线性变化（tk 是 ``1.333 × 缩放`` 像素/点），
        而偏移是线性的。两者在 100% 与 125% 下差了 1~5px，正好让标题与副标题
        的字符撞在一起（125% 下实测："两行需要 43px，行距只给了 42px"）。
        用度量算，任何 DPI 都不会撞。
        """
        heights = [f.metrics("linespace") for f in fonts]
        total = sum(heights) + gap * max(0, len(fonts) - 1)
        centers: list[float] = []
        cursor = float(top)
        for height in heights:
            centers.append(cursor + height / 2.0)
            cursor += height + gap
        return total, centers

    def _heading(self, text: str, extra: str = "") -> None:
        """左上角小标题 + 右侧灰色补充说明。"""
        baseline = px(self.scale, 8)
        self._label(0, baseline, text, size=Typography.label, field="text_faint", bold=True)
        if extra:
            self._label(
                px(self.scale, 96), baseline, extra,
                size=Typography.label, field="text_faint", limit=px(self.scale, 520),
            )


# --------------------------------------------------------------------------
# 1. 行为矩阵
# --------------------------------------------------------------------------


class MatrixView(CanvasView):
    """3×3 行为矩阵（``thumb=True`` 时是结论卡右侧的缩略图）。"""

    DEFAULT_HEIGHT = 190.0

    def __init__(self, parent, palette, scale, *, thumb: bool = False, height: float = 0.0,
                 surface: str = ""):
        self._matrix: viz.Matrix | None = None
        super().__init__(parent, palette, scale, height=height, surface=surface, thumb=thumb)

    def set_matrix(self, matrix: viz.Matrix | None) -> None:
        self._matrix = matrix
        self.invalidate()

    # ---- 绘制 ----

    def _draw(self, width: int, height: int) -> None:
        if self._thumb:
            self._draw_thumb(width, height)
            return
        matrix = self._matrix
        if matrix is None:
            self._empty(width, height)
            return

        head_h = px(self.scale, 20)
        col_head_h = px(self.scale, 18)
        note_h = px(self.scale, 20)
        row_head_w = px(self.scale, 78)
        narrow = self._collapsed(width)
        if narrow:
            # 窄宽度下"不硬挤"：去掉英文副名与底部注释，只留中文标题，
            # 但**保留矩阵结构**——它是主视图，退化成一行文字反而更看不懂。
            note_h = 0
        grid_top = head_h + col_head_h
        grid_h = max(px(self.scale, 60), height - grid_top - note_h)
        grid_w = max(px(self.scale, 120), min(width - row_head_w, px(self.scale, 640)))
        cell_w = grid_w / 3.0
        cell_h = grid_h / 3.0
        gap = max(1, px(self.scale, 2))

        self._heading("行为矩阵", "" if narrow else "行＝映射行为 · 列＝过滤行为")

        # 列头
        for index, col in enumerate(matrix.cols):
            center = row_head_w + cell_w * (index + 0.5)
            self._label(
                center, head_h + col_head_h / 2.0,
                viz.COL_CODE.get(col, col.upper()),
                size=Typography.label, field="text_dim", bold=True, anchor="center",
            )
        # 行头：上行缩写、下行中文（挤在一行会被截成"EIM 端…"，读不出含义）
        for index, row in enumerate(matrix.rows):
            center = grid_top + cell_h * (index + 0.5)
            self._label(
                0, center - px(self.scale, 7),
                viz.ROW_CODE.get(row, row.upper()),
                size=Typography.label, field="text_dim", bold=True, anchor="w",
            )
            self._label(
                0, center + px(self.scale, 7),
                viz.ROW_SHORT.get(row, ""), size=Typography.label,
                field="text_faint", anchor="w", limit=row_head_w - px(self.scale, 6),
            )

        for row_index, line in enumerate(matrix.cells):
            for col_index, cell in enumerate(line):
                x0 = row_head_w + cell_w * col_index + gap / 2.0
                y0 = grid_top + cell_h * row_index + gap / 2.0
                self._draw_cell(cell, x0, y0, cell_w - gap, cell_h - gap, show_sub=not narrow)

        all_na = all(c.state == viz.STATE_NA for line in matrix.cells for c in line)
        if all_na and matrix.note:
            self._draw_overlay(matrix.note, row_head_w, grid_top, grid_w, grid_h)
        elif matrix.note and note_h > 0:
            self._label(
                0, height - note_h + px(self.scale, 4), "说明：" + matrix.note,
                size=Typography.label, field="text_dim", limit=width - px(self.scale, 4),
            )

    def _draw_cell(
        self, cell: viz.Cell, x0: float, y0: float, w: float, h: float, show_sub: bool = True
    ) -> None:
        palette = self.palette
        state = cell.state
        # 上色一律用 display_tone（候选态统一 CANDIDATE_TONE）。直接拿 cell.tone
        # 上色的话，cone_unknown 下「完全锥型」那格是绿的，读起来像「这个更可能」，
        # 而结论恰恰是三者分不出来。导出图（report._Planner.matrix）用同一套。
        tone = tone_color(palette, cell.display_tone)
        if state == viz.STATE_ACTIVE:
            fill = blend(palette.surface, tone, 0.18)
            outline = tone
            border = max(2, px(self.scale, 2))
            ink = ""  # 空串=用 tone 本色写标题（与导出图一致）
            bold = True
        elif state == viz.STATE_RELATED:
            fill = palette.surface  # 无填充：只有统一描边 + 右上色点
            outline = tone
            border = HAIRLINE
            ink = "text"
            bold = False
        elif state == viz.STATE_NA:
            fill = palette.overlay
            outline = palette.grid
            border = HAIRLINE
            ink = "text_faint"
            bold = False
        else:  # dim（已被实测排除：surface_alt + grid，强调度必须低于 related）
            fill = palette.surface_alt
            outline = palette.grid
            border = HAIRLINE
            ink = "text_faint"
            bold = False

        radius = min(px(self.scale, 4), w / 2.0, h / 2.0)
        self.create_polygon(
            round_rect_points(x0, y0, x0 + w, y0 + h, radius),
            fill=fill, outline=outline, width=border,
        )
        if state == viz.STATE_RELATED:
            # 右上角一个圆点：和描边一起表达"这一格还活着，但不是结论"
            dot = max(2, px(self.scale, 3))
            self.create_oval(
                x0 + w - px(self.scale, 8) - dot, y0 + px(self.scale, 8) - dot,
                x0 + w - px(self.scale, 8) + dot, y0 + px(self.scale, 8) + dot,
                fill=tone, outline=tone,
            )
        if state == viz.STATE_NA:
            return  # na 格只留底，文字由中央浮层承担，避免九格重复同一句话

        ink_color = tone if not ink else self._ink(ink)
        center_x = x0 + w / 2.0
        title_font = font(Typography.body if h >= px(self.scale, 44) else Typography.label,
                          bold=bold)
        sub_font = font(Typography.label)
        # 标题 + 英文副名：按字体度量堆叠并整体居中（固定 ±px(7)/px(9) 在
        # 100%/125% 下会让两行字挤在一起）
        block_h, centers = self._stack(0.0, (title_font, sub_font))
        if show_sub and block_h <= h - px(self.scale, 4):
            top = y0 + (h - block_h) / 2.0
            self.create_text(
                center_x, top + centers[0],
                text=ellipsis(cell.title, title_font, w - px(self.scale, 8)),
                fill=ink_color, font=title_font, anchor="center",
            )
            self.create_text(
                center_x, top + centers[1],
                text=ellipsis(cell.sub, sub_font, w - px(self.scale, 8)),
                fill=palette.text_faint, font=sub_font, anchor="center",
            )
        else:
            self.create_text(
                center_x, y0 + h / 2.0,
                text=ellipsis(cell.title, title_font, w - px(self.scale, 4)),
                fill=ink_color, font=title_font, anchor="center",
            )

    def _draw_overlay(self, note: str, x0: float, y0: float, w: float, h: float) -> None:
        """全 na 时在矩阵中央放一块浮层说明原因（不能只留九个空格子）。"""
        palette = self.palette
        fnt = font(Typography.label)
        lines = _wrap(note, fnt, px(self.scale, 300), max_lines=2)
        box_w = min(w - px(self.scale, 20), px(self.scale, 360))
        box_h = px(self.scale, 18) + len(lines) * fnt.metrics("linespace")
        cx = x0 + w / 2.0
        cy = y0 + h / 2.0
        self.create_polygon(
            round_rect_points(cx - box_w / 2.0, cy - box_h / 2.0,
                              cx + box_w / 2.0, cy + box_h / 2.0, px(self.scale, 6)),
            fill=palette.surface, outline=palette.border, width=HAIRLINE,
        )
        line_h = fnt.metrics("linespace")
        top = cy - box_h / 2.0 + px(self.scale, 9)
        for index, line in enumerate(lines):
            self._label(
                cx, top + line_h * (index + 0.5), line,
                size=Typography.label, field="text_dim", anchor="center",
            )

    def _draw_thumb(self, width: int, height: int) -> None:
        """缩略矩阵：只用颜色表达"哪一格亮着"，放进结论卡右侧做一眼定位。

        缩略图不放文字：62px 的容器里每格不到 20px，"APDF" 会溢出格子反而
        更乱；格子的语义由完整视图（下方）承担。
        """
        matrix = self._matrix
        if matrix is None:
            self._empty(width, height, "")
            return
        palette = self.palette
        gap = max(1, px(self.scale, 2))
        size = min(width, height)
        cell = (size - 2 * gap) / 3.0
        origin_x = (width - size) / 2.0
        origin_y = (height - size) / 2.0
        for row_index, line in enumerate(matrix.cells):
            for col_index, cell_data in enumerate(line):
                x0 = origin_x + col_index * cell
                y0 = origin_y + row_index * cell
                state = cell_data.state
                tone = tone_color(palette, cell_data.display_tone)
                if state == viz.STATE_ACTIVE:
                    fill = tone
                    outline = tone
                    border = max(2, px(self.scale, 2))
                elif state == viz.STATE_RELATED:
                    # 候选：无填充 + 候选色描边（缩略图也得读成"不确定"）
                    fill = palette.surface
                    outline = tone
                    border = HAIRLINE
                elif state == viz.STATE_NA:
                    fill = palette.overlay
                    outline = palette.grid
                    border = HAIRLINE
                else:
                    fill = palette.surface_alt
                    outline = palette.grid
                    border = HAIRLINE
                self.create_polygon(
                    round_rect_points(x0, y0, x0 + cell - gap, y0 + cell - gap,
                                      min(px(self.scale, 3), cell / 2.0)),
                    fill=fill, outline=outline, width=border,
                )


# --------------------------------------------------------------------------
# 2. 类型谱
# --------------------------------------------------------------------------


class SpectrumView(CanvasView):
    """类型谱：从"开放互联网"到"UDP 阻断"的横向色带 + 结论位置标记。

    标记的三种情况（与导出图 ``report._Planner.spectrum`` 逐条对齐）：

    * ``current`` —— 段本身用类型色填充，段中心上方**一个**三角；
    * ``candidate`` —— 连续候选段合成 ``viz.spectrum_span()`` 给出的**一个**
      区间：整段画一条略高的胶囊（内部用卡片底色细线分段）、区间中央一个
      三角、区间下方居中写 ``span.label``。三段各画一个三角会被读成"三个
      都命中/挑一个"，而正确含义是"落在这三段构成的区间内"；
    * 都没有 —— 不画标记。
    """

    DEFAULT_HEIGHT = 96.0

    def __init__(self, parent, palette, scale, *, height: float = 0.0, surface: str = ""):
        self._segments: tuple[viz.SpectrumSegment, ...] = ()
        super().__init__(parent, palette, scale, height=height, surface=surface)

    def set_spectrum(self, segments) -> None:
        self._segments = tuple(segments or ())
        self.invalidate()

    def _draw(self, width: int, height: int) -> None:
        segments = self._segments
        if not segments:
            self._empty(width, height)
            return
        if self._collapsed(width):
            self._draw_collapsed(width, height, segments)
            return

        palette = self.palette
        warn = palette.warn
        head_h = px(self.scale, 20)
        bar_h = max(px(self.scale, 10), px(self.scale, 14))
        bar_top = head_h + px(self.scale, 8)
        label_font = font(Typography.label)
        sub_font = font(Typography.label)
        label_block, label_centers = self._stack(0.0, (label_font, sub_font))
        label_top = bar_top + bar_h + px(self.scale, 2)
        count = len(segments)
        gap = max(1, px(self.scale, 3))
        seg_w = max(1.0, (width - gap * (count - 1)) / float(count))
        # score 只用于文案里的数值展示，几何一律按元组下标算
        span = viz.spectrum_span(segments)
        current = [s for s in segments if s.state == "current"]

        if current:
            caption, caption_field = f"当前 · {current[0].label}", "text_dim"
        elif span is not None:
            caption, caption_field = "可能 · 落在区间内", "warn"
        else:
            caption, caption_field = "结论未定", "text_faint"
        self._label(0, px(self.scale, 8), "类型谱",
                    size=Typography.label, field="text_faint", bold=True)
        self._label(width, px(self.scale, 8), caption,
                    size=Typography.label, field=caption_field, anchor="ne", limit=width)

        if span is not None:
            # 一条横跨候选区间的胶囊（上下各高 2px），内部用卡片底色分格
            grow = px(self.scale, 2)
            first_x = span.start * (seg_w + gap)
            last_x = span.end * (seg_w + gap) + seg_w
            self.create_polygon(
                round_rect_points(first_x, bar_top - grow, last_x, bar_top + bar_h + grow,
                                  bar_h / 2.0 + grow),
                fill=blend(palette.surface, warn, 0.28), outline=warn, width=HAIRLINE,
            )
            for index in range(span.start, span.end):
                divider = index * (seg_w + gap) + seg_w + gap / 2.0
                self.create_line(
                    divider, bar_top + px(self.scale, 1),
                    divider, bar_top + bar_h - px(self.scale, 1),
                    fill=palette.surface, width=HAIRLINE,
                )

        for index, segment in enumerate(segments):
            x0 = index * (seg_w + gap)
            x1 = x0 + seg_w
            in_span = span is not None and span.start <= index <= span.end
            if segment.state == "current":
                fill = tone_color(palette, segment.tone)
                outline = fill
                ink, bold = "text", True
            elif segment.state == "candidate":
                # 候选不跟各自语义色走（统一 warn）；区间条已画好时不再重复画
                fill = blend(palette.surface, warn, 0.28)
                outline = warn
                ink, bold = "text_dim", False
            else:
                fill = palette.overlay
                outline = palette.grid
                ink, bold = "text_faint", False
            if not in_span:
                self.create_polygon(
                    round_rect_points(x0, bar_top, x1, bar_top + bar_h,
                                      min(px(self.scale, 3), bar_h / 2)),
                    fill=fill, outline=outline, width=HAIRLINE,
                )
            font_label = font(Typography.label, bold=bold)
            self.create_text(
                x0 + seg_w / 2.0, label_top + label_centers[0],
                text=ellipsis(segment.label, font_label, seg_w - px(self.scale, 2)),
                fill=self._ink(ink), font=font_label, anchor="center",
            )
            self.create_text(
                x0 + seg_w / 2.0, label_top + label_centers[1],
                text=ellipsis(segment.sub, sub_font, seg_w - px(self.scale, 2)),
                fill=palette.text_faint, font=sub_font, anchor="center",
            )

        half = px(self.scale, 5)
        if span is not None:
            mid = (span.start * (seg_w + gap) + span.end * (seg_w + gap) + seg_w) / 2.0
            base = bar_top - px(self.scale, 2)
            self.create_polygon(
                mid, base, mid - half, base - half, mid + half, base - half,
                fill=warn, outline="",
            )
            self.create_text(
                mid, label_top + label_block + px(self.scale, 2),
                text=ellipsis(span.label, label_font,
                              int((span.end - span.start + 1) * (seg_w + gap))),
                fill=warn, font=label_font, anchor="n",
            )
        elif current:
            marker_x = segments.index(current[0]) * (seg_w + gap) + seg_w / 2.0
            base = bar_top - px(self.scale, 1)
            self.create_polygon(
                marker_x, base, marker_x - half, base - px(self.scale, 5),
                marker_x + half, base - px(self.scale, 5),
                fill=tone_color(palette, current[0].tone), outline="",
            )

    def _draw_collapsed(self, width: int, height: int, segments) -> None:
        """窄宽度：单行摘要。候选区间只说"落在区间内"，不并列三个段名。"""
        current = [s for s in segments if s.state == "current"]
        span = viz.spectrum_span(segments)
        if current:
            text = (f"类型谱：当前为 {current[0].label}（{current[0].sub}），"
                    f"P2P 友好度 {current[0].score}/100")
            field = "text_dim"
        elif span is not None:
            text = f"类型谱：{span.label}"
            field = "warn"
        else:
            text = "类型谱：结论未定"
            field = "text_faint"
        self._heading("类型谱")
        # 贴着标题写，不留居中造成的上下双空档（那看起来像画丢了内容）
        self._label(
            0, px(self.scale, 32), text, size=Typography.body, field=field,
            anchor="w", limit=width,
        )


# --------------------------------------------------------------------------
# 3. 映射样本
# --------------------------------------------------------------------------


class SamplesView(CanvasView):
    """映射样本 chip：端口相同 → 同底色 + 底部括线归组。"""

    DEFAULT_HEIGHT = 112.0

    def __init__(self, parent, palette, scale, *, height: float = 0.0, surface: str = ""):
        self._chips: tuple[viz.SampleChip, ...] = ()
        super().__init__(parent, palette, scale, height=height, surface=surface)

    def set_samples(self, chips) -> None:
        self._chips = tuple(chips or ())
        self.invalidate()

    def _draw(self, width: int, height: int) -> None:
        chips = self._chips
        if not chips:
            self._heading("映射样本")
            self._label(
                0, px(self.scale, 32),
                "无映射样本（可用服务器不足，无法比对映射端口）",
                size=Typography.body, field="text_faint", anchor="w", limit=width,
            )
            return
        if self._collapsed(width):
            self._heading("映射样本")
            ports = {chip.mapped.rsplit(":", 1)[-1] for chip in chips}
            kinds = "端口一致（EIM 特征）" if len(ports) == 1 else f"{len(ports)} 种端口（随目的变化）"
            self._label(
                0, px(self.scale, 32), f"映射样本：{len(chips)} 条，{kinds}",
                size=Typography.body, field="text_dim", anchor="w", limit=width,
            )
            return

        palette = self.palette
        head_h = px(self.scale, 22)
        chip_h = px(self.scale, 22)
        chip_gap = px(self.scale, 6)
        text_font = font(Typography.note, family=MONO_FAMILY)
        port_font = font(Typography.label)
        # 行距 = 括线间距 + 端口标注行高 + 呼吸空间；固定 px(17) 在 200% 下
        # 会让「端口 53412」压到下一行 chip 上
        row_gap = px(self.scale, 3) + port_font.metrics("linespace") + px(self.scale, 6)
        # 端口 -> 配色序号：同一映射端口用同一浅底，视觉上自动成组
        port_order: dict[str, int] = {}
        for chip in chips:
            port_order.setdefault(chip.mapped.rsplit(":", 1)[-1], len(port_order))

        self._heading("映射样本", "同一映射端口同底色 · 底部括线归组")

        max_rows = max(1, int((height - head_h) // (chip_h + row_gap)))
        x = 0.0
        y = float(head_h)
        row = 0
        shown = 0
        # (端口, 行 y, 左端 x, 右端 x)：同一行内相邻的同端口 chip 合成一段
        groups: list[list] = []
        for chip in chips:
            port = chip.mapped.rsplit(":", 1)[-1]
            chip_w = text_font.measure(chip.text) + 2 * px(self.scale, 7)
            if x > 0 and x + chip_w > width:
                row += 1
                if row + 1 > max_rows:
                    break
                x = 0.0
                y += chip_h + row_gap
            shade = 0.10 + 0.05 * (port_order[port] % 3)
            fill = tone_fill(palette, chip.tone, shade)
            outline = blend(palette.surface, tone_color(palette, chip.tone), 0.45)
            self.create_polygon(
                round_rect_points(x, y, x + chip_w, y + chip_h, min(px(self.scale, 4), chip_h / 2)),
                fill=fill, outline=outline, width=HAIRLINE,
            )
            self._label(
                x + chip_w / 2.0, y + chip_h / 2.0, chip.text,
                size=Typography.note, field="text", family=MONO_FAMILY, anchor="center",
            )
            if groups and groups[-1][0] == port and groups[-1][1] == y:
                groups[-1][3] = x + chip_w
                groups[-1][4] += 1
            else:
                groups.append([port, y, x, x + chip_w, 1])
            shown += 1
            x += chip_w + chip_gap

        # 底部括线：把同端口的相邻 chip 连成一段，让「端口没变」看得见
        bracket_color = blend(palette.surface, palette.text_dim, 0.65)
        for port, top, left, right, count in groups:
            if count < 2 or right - left < px(self.scale, 30):
                continue  # 单个 chip 不需要括号
            bracket_y = float(top) + chip_h + px(self.scale, 3)
            self.create_line(left, bracket_y, right, bracket_y,
                             fill=bracket_color, width=HAIRLINE)
            for edge in (left, right):
                self.create_line(edge, bracket_y, edge, bracket_y - px(self.scale, 3),
                                 fill=bracket_color, width=HAIRLINE)
            # 括号中间写端口号：4 个 chip 才 1 种端口这件事，一看就懂
            self._label(
                (left + right) / 2.0, bracket_y + px(self.scale, 1), f"端口 {port}",
                size=Typography.label, field="text_faint", anchor="n",
            )

        if shown < len(chips):
            self._label(
                width - px(self.scale, 2), head_h + px(self.scale, 2),
                f"+{len(chips) - shown} 条",
                size=Typography.label, field="text_faint", anchor="ne",
            )


# --------------------------------------------------------------------------
# 4. 场景徽章 / 网关徽标
# --------------------------------------------------------------------------


class ScenariosView(CanvasView):
    """四个典型 P2P 场景的可用性圆点（游戏/BT/远程/语音）。"""

    DEFAULT_HEIGHT = 74.0

    def __init__(self, parent, palette, scale, *, height: float = 0.0, surface: str = ""):
        self._scenarios: tuple[viz.Scenario, ...] = ()
        super().__init__(parent, palette, scale, height=height, surface=surface)

    def set_scenarios(self, scenarios) -> None:
        self._scenarios = tuple(scenarios or ())
        self.invalidate()

    def _draw(self, width: int, height: int) -> None:
        palette = self.palette
        scenarios = self._scenarios
        self._heading("场景可用性", "按 NAT 类型查表")
        if not scenarios:
            self._label(0, height - px(self.scale, 20), "尚未检测",
                        size=Typography.body, field="text_faint", anchor="w")
            return
        count = len(scenarios)
        column_w = width / float(count)
        dot_r = max(3, px(self.scale, 5))
        label_font = font(Typography.body)
        hint_font = font(Typography.label)
        # 两行文字按度量堆叠并整体垂直居中（固定 ±px(6) 在 125% 下会撞行）
        block_h, centers = self._stack(0.0, (label_font, hint_font))
        top = max(px(self.scale, 20), (height - block_h) / 2.0)
        center_y = top + block_h / 2.0
        for index, scenario in enumerate(scenarios):
            x = column_w * index
            dot_x = x + dot_r
            color = tone_color(palette, scenario.tone)
            if scenario.level == "na":
                self.create_oval(
                    dot_x - dot_r, top + centers[0] - dot_r, dot_x + dot_r, top + centers[0] + dot_r,
                    outline=color, width=HAIRLINE, fill="",
                )
            else:
                self.create_oval(
                    dot_x - dot_r, top + centers[0] - dot_r, dot_x + dot_r, top + centers[0] + dot_r,
                    fill=color, outline=color, width=HAIRLINE,
                )
            text_left = dot_x + dot_r + px(self.scale, 6)
            room = int(column_w - (text_left - x) - px(self.scale, 8))
            self.create_text(
                text_left, top + centers[0],
                text=ellipsis(scenario.label, label_font, room),
                fill=palette.text, font=label_font, anchor="w",
            )
            self.create_text(
                text_left, top + centers[1],
                text=ellipsis(scenario.hint, hint_font, room),
                fill=palette.text_dim, font=hint_font, anchor="w",
            )


class GatewayStrip(CanvasView):
    """网关端口映射徽标（UPnP / NAT-PMP / PCP）。"""

    DEFAULT_HEIGHT = 52.0

    def __init__(self, parent, palette, scale, *, height: float = 0.0, surface: str = ""):
        self._badges: tuple[viz.Badge, ...] = ()
        super().__init__(parent, palette, scale, height=height, surface=surface)

    def set_badges(self, badges) -> None:
        self._badges = tuple(badges or ())
        self.invalidate()

    def _draw(self, width: int, height: int) -> None:
        palette = self.palette
        badges = self._badges
        self._heading("网关端口映射")
        if not badges:
            self._label(0, height - px(self.scale, 18), "未检测",
                        size=Typography.body, field="text_faint", anchor="w")
            return
        fnt = font(Typography.label)
        x = 0.0
        y = height - px(self.scale, 26)
        chip_h = px(self.scale, 20)
        for badge in badges:
            text = f"{badge.label} {badge.value}"
            chip_w = fnt.measure(text) + 2 * px(self.scale, 8)
            if x + chip_w > width:
                break
            self.create_polygon(
                round_rect_points(x, y, x + chip_w, y + chip_h, chip_h / 2),
                fill=tone_fill(palette, badge.tone, 0.16),
                outline=blend(palette.surface, tone_color(palette, badge.tone), 0.55),
                width=HAIRLINE,
            )
            self._label(
                x + chip_w / 2.0, y + chip_h / 2.0, text,
                size=Typography.label, field="text", anchor="center",
            )
            x += chip_w + px(self.scale, 6)


# --------------------------------------------------------------------------
# 5. 步骤时间线
# --------------------------------------------------------------------------


class StepsView(CanvasView):
    """步骤时间线：替代埋在日志里的 Test I / II / III。"""

    DEFAULT_HEIGHT = 132.0

    def __init__(self, parent, palette, scale, *, height: float = 0.0, surface: str = ""):
        self._steps: tuple[TestStep, ...] = ()
        super().__init__(parent, palette, scale, height=height, surface=surface)

    # ---- 度量 ----

    def _metrics(self) -> tuple[float, float, object, object]:
        """返回 ``(行高, 表头高, 名称字体, 详情字体)``。

        行高由**字体度量**推导：名称行 + 详情行 + 上下呼吸空间。固定
        ``px(34)`` 在 100% / 125% 下都装不下两行（实测 35/43px 需要，却只有
        34/42px 可用），两行字符会撞在一起。
        """
        name_font = font(Typography.body, bold=True)
        detail_font = font(Typography.label)
        pad_y = px(self.scale, 6)
        row_h = (
            name_font.metrics("linespace") + detail_font.metrics("linespace") + pad_y * 2
        )
        return float(row_h), float(px(self.scale, 22)), name_font, detail_font

    def set_steps(self, steps) -> None:
        self._steps = tuple(steps or ())
        row_h, head_h, _name, _detail = self._metrics()
        wanted = head_h + max(1, len(self._steps)) * row_h
        super().configure(height=max(px(self.scale, self.DEFAULT_HEIGHT), int(wanted)))
        self.invalidate()

    def _draw(self, width: int, height: int) -> None:
        palette = self.palette
        steps = self._steps
        row_h, head_h, name_font, detail_font = self._metrics()
        pad_y = px(self.scale, 6)
        self._heading("检测步骤", "Test I / II / III 与行为采样")
        if not steps:
            self._label(0, head_h + px(self.scale, 8), "尚无步骤记录",
                        size=Typography.body, field="text_faint", anchor="w")
            return
        dot_x = px(self.scale, 7)
        radius = max(3, px(self.scale, 6))
        icon_x = dot_x + radius + px(self.scale, 7)
        text_left = icon_x + px(self.scale, 11)
        limit = max(px(self.scale, 60), int(width - text_left))
        _total, centers = self._stack(0.0, (name_font, detail_font))

        # 先画连接线，让节点盖在线上
        if len(steps) > 1:
            self.create_line(
                dot_x, head_h + row_h / 2.0, dot_x,
                head_h + row_h * (len(steps) - 1) + row_h / 2.0,
                fill=palette.grid, width=HAIRLINE,
            )

        for index, step in enumerate(steps):
            icon, field = status_style(step.status)
            color = self._ink(field)
            row_top = head_h + row_h * index
            name_y = row_top + pad_y + centers[0]
            detail_y = row_top + pad_y + centers[1]
            center_y = row_top + row_h / 2.0
            if step.status == "pending":
                self.create_oval(
                    dot_x - radius, center_y - radius, dot_x + radius, center_y + radius,
                    fill=palette.surface, outline=color, width=HAIRLINE,
                )
            else:
                self.create_oval(
                    dot_x - radius, center_y - radius, dot_x + radius, center_y + radius,
                    fill=color, outline=color, width=HAIRLINE,
                )
            self.create_text(
                icon_x, name_y, text=icon, fill=color, font=name_font, anchor="w",
            )
            self.create_text(
                text_left, name_y,
                text=ellipsis(step.name, name_font, limit),
                fill=palette.text, font=name_font, anchor="w",
            )
            if step.detail:
                self.create_text(
                    text_left, detail_y,
                    text=ellipsis(step.detail, detail_font, limit),
                    fill=palette.text_dim, font=detail_font, anchor="w",
                )


# --------------------------------------------------------------------------
# 组合视图
# --------------------------------------------------------------------------


class ResultView(tk.Frame):
    """结果视图区：把五个子视图纵向叠起来。

    每个子视图内部自带小标题，所以这里不再套额外的 Label，垂直空间全部留给
    图形本身（1366×768 @125% 下每一像素都紧张）。

    这里**不再**自己套滚动容器：整页滚动由 :class:`~natscope.gui.app.App`
    的 body 承担。两层嵌套滚动会让滚轮在"内层滚到底之后是否传给外层"上表现
    不确定，而且内层视口会把矩阵切在中间（看起来像画错了）。
    """

    def __init__(self, parent, palette: Palette, scale: float, *, surface: str = ""):
        self.palette = palette
        self.scale = float(scale)
        self._surface = surface or palette.surface
        super().__init__(parent, bg=self._surface, bd=0, highlightthickness=0)
        holder = self
        gap = px(self.scale, 10)

        self.matrix = MatrixView(holder, palette, scale, surface=self._surface)
        self.spectrum = SpectrumView(holder, palette, scale, surface=self._surface)
        self.samples = SamplesView(holder, palette, scale, surface=self._surface)
        self.scenarios = ScenariosView(holder, palette, scale, surface=self._surface)
        self.gateway = GatewayStrip(holder, palette, scale, surface=self._surface)
        self.steps = StepsView(holder, palette, scale, surface=self._surface)

        self._gap = gap
        self._samples_visible = True
        for widget in (self.matrix, self.spectrum, self.samples,
                       self.scenarios, self.gateway, self.steps):
            widget.pack(fill="x", pady=(0, gap))

    def _show_samples(self, visible: bool) -> None:
        """无映射样本时把整块收起来（viz 约定 ``build_samples()`` 返回空元组）。"""
        if visible == self._samples_visible:
            return
        self._samples_visible = visible
        if visible:
            self.samples.pack(fill="x", pady=(0, self._gap), before=self.scenarios)
        else:
            self.samples.pack_forget()

    # ---- 接口 ----

    def views(self) -> tuple[CanvasView, ...]:
        """全部子视图（换主题、重绘时统一遍历）。"""
        return (self.matrix, self.spectrum, self.samples,
                self.scenarios, self.gateway, self.steps)

    def set_palette(self, palette: Palette) -> None:
        self.palette = palette
        for view in self.views():
            view.set_palette(palette)

    def render(self, nat: NatResult | None, gateway) -> None:
        """按 nat / gateway 重算五个子视图的数据模型并刷新。

        ``gateway`` 为 ``None`` 时网关徽标显示"未检测"——这正是网络探测
        失败时的降级路径：NAT 结论已经算完，不能因为网关未知就整体丢弃。
        """
        if nat is None:
            self.matrix.set_matrix(None)
            self.spectrum.set_spectrum(())
            self.samples.set_samples(())
            self.scenarios.set_scenarios(())
            self.gateway.set_badges(())
            self.steps.set_steps(())
            self._show_samples(True)
            return
        chips = viz.build_samples(nat)
        self.matrix.set_matrix(viz.build_matrix(nat))
        self.spectrum.set_spectrum(viz.build_spectrum(nat))
        self.samples.set_samples(chips)
        self.scenarios.set_scenarios(viz.build_scenarios(nat))
        self.gateway.set_badges(viz.build_badges(gateway))
        self.steps.set_steps(nat.steps)
        self._show_samples(bool(chips))


__all__ = [
    "COLLAPSE_WIDTH",
    "CanvasView",
    "GatewayStrip",
    "MatrixView",
    "ResultView",
    "STEP_STYLE",
    "SamplesView",
    "ScenariosView",
    "SpectrumView",
    "StepsView",
    "status_style",
]
