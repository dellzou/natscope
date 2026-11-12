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
                0, height - note_h + px(self.scale, 4), matrix.note,
                size=Typography.label, field="text_dim", limit=width - px(self.scale, 4),
            )

    def _draw_cell(
        self, cell: viz.Cell, x0: float, y0: float, w: float, h: float, show_sub: bool = True
    ) -> None:
        palette = self.palette
        state = cell.state
        if state == viz.STATE_ACTIVE:
            fill = tone_fill(palette, cell.tone, 0.18)
            outline = tone_color(palette, cell.tone)
            border = max(2, px(self.scale, 2))
            ink = "text"
            bold = True
        elif state == viz.STATE_RELATED:
            fill = tone_fill(palette, cell.tone, 0.10)
            outline = blend(palette.surface, tone_color(palette, cell.tone), 0.65)
            border = HAIRLINE
            ink = "text"
            bold = False
        elif state == viz.STATE_NA:
            fill = palette.bg
            outline = palette.grid
            border = HAIRLINE
            ink = "text_faint"
            bold = False
        else:  # dim
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
        if state == viz.STATE_NA:
            return  # na 格只留底，文字由中央浮层承担，避免九格重复同一句话

        center_x = x0 + w / 2.0
        if show_sub and h >= px(self.scale, 44):
            self._label(
                center_x, y0 + h / 2.0 - px(self.scale, 7), cell.title,
                size=Typography.body, field=ink, bold=bold, anchor="center",
                limit=w - px(self.scale, 8),
            )
            self._label(
                center_x, y0 + h / 2.0 + px(self.scale, 9), cell.sub,
                size=Typography.label, field="text_faint", anchor="center",
                limit=w - px(self.scale, 8),
            )
        else:
            self._label(
                center_x, y0 + h / 2.0, cell.title,
                size=Typography.body if h >= px(self.scale, 44) else Typography.label,
                field=ink, bold=bold, anchor="center",
                limit=w - px(self.scale, 4),
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
                if state == viz.STATE_ACTIVE:
                    fill = tone_color(palette, cell_data.tone)
                    outline = fill
                    border = max(2, px(self.scale, 2))
                elif state == viz.STATE_RELATED:
                    fill = tone_fill(palette, cell_data.tone, 0.55)
                    outline = tone_color(palette, cell_data.tone)
                    border = HAIRLINE
                elif state == viz.STATE_NA:
                    fill = palette.bg
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
    """类型谱：从"开放互联网"到"UDP 阻断"的横向色带 + 当前类型三角标记。"""

    DEFAULT_HEIGHT = 72.0

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
        head_h = px(self.scale, 20)
        bar_h = max(px(self.scale, 12), px(self.scale, 16))
        label_h = px(self.scale, 16)
        bar_top = head_h + px(self.scale, 8)
        count = len(segments)
        gap = max(1, px(self.scale, 3))
        seg_w = max(1.0, (width - gap * (count - 1)) / float(count))

        self._heading("类型谱", "从左到右：P2P 友好度递减")

        current_index = -1
        for index, segment in enumerate(segments):
            x0 = index * (seg_w + gap)
            x1 = x0 + seg_w
            if segment.state == "current":
                fill = tone_color(palette, segment.tone)
                outline = fill
                border = HAIRLINE
                current_index = index
            elif segment.state == "candidate":
                fill = blend(palette.surface, tone_color(palette, segment.tone), 0.55)
                outline = tone_color(palette, segment.tone)
                border = HAIRLINE
            else:
                fill = blend(palette.surface, tone_color(palette, segment.tone), 0.18)
                outline = palette.grid
                border = HAIRLINE
            self.create_polygon(
                round_rect_points(x0, bar_top, x1, bar_top + bar_h, min(px(self.scale, 3), bar_h / 2)),
                fill=fill, outline=outline, width=border,
            )
            active = segment.state in ("current", "candidate")
            self._label(
                x0 + seg_w / 2.0, bar_top + bar_h + px(self.scale, 4),
                segment.label, size=Typography.label,
                field="text" if active else "text_faint",
                bold=segment.state == "current", anchor="n",
                limit=seg_w - px(self.scale, 2),
            )

        if 0 <= current_index < count:
            marker_x = current_index * (seg_w + gap) + seg_w / 2.0
            half = px(self.scale, 5)
            top = bar_top - px(self.scale, 5)
            self.create_polygon(
                marker_x, bar_top - 1,
                marker_x - half, top,
                marker_x + half, top,
                fill=palette.text, outline="",
            )

    def _draw_collapsed(self, width: int, height: int, segments) -> None:
        current = [s for s in segments if s.state == "current"]
        candidates = [s for s in segments if s.state == "candidate"]
        if current:
            text = f"类型谱：当前为 {current[0].label}（{current[0].sub}），P2P 友好度 {current[0].score}/100"
        elif candidates:
            text = "类型谱：锥型（未细分）—— " + " / ".join(s.label for s in candidates)
        else:
            text = "类型谱：无明显位置（" + " → ".join(s.label for s in segments) + "）"
        self._heading("类型谱")
        # 贴着标题写，不留居中造成的上下双空档（那看起来像画丢了内容）
        self._label(
            0, px(self.scale, 32), text, size=Typography.body, field="text_dim",
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
        # 行距要能容下括线与端口标注，否则上一行的括线会贴到下一行 chip 上
        row_gap = px(self.scale, 17)
        text_font = font(Typography.note, family=MONO_FAMILY)
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
        center_y = height - px(self.scale, 26)
        for index, scenario in enumerate(scenarios):
            x = column_w * index
            dot_x = x + dot_r
            color = tone_color(palette, scenario.tone)
            if scenario.level == "na":
                self.create_oval(
                    dot_x - dot_r, center_y - dot_r, dot_x + dot_r, center_y + dot_r,
                    outline=color, width=HAIRLINE, fill="",
                )
            else:
                self.create_oval(
                    dot_x - dot_r, center_y - dot_r, dot_x + dot_r, center_y + dot_r,
                    fill=color, outline=color, width=HAIRLINE,
                )
            text_left = dot_x + dot_r + px(self.scale, 6)
            self._label(
                text_left, center_y - px(self.scale, 6), scenario.label,
                size=Typography.body, field="text", anchor="w",
                limit=column_w - (text_left - x) - px(self.scale, 8),
            )
            self._label(
                text_left, center_y + px(self.scale, 6), scenario.hint,
                size=Typography.label, field="text_dim", anchor="w",
                limit=column_w - (text_left - x) - px(self.scale, 8),
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

    def set_steps(self, steps) -> None:
        self._steps = tuple(steps or ())
        row_h = px(self.scale, 34)
        head_h = px(self.scale, 22)
        wanted = head_h + max(1, len(self._steps)) * row_h
        super().configure(height=max(px(self.scale, self.DEFAULT_HEIGHT), wanted))
        self.invalidate()

    def _draw(self, width: int, height: int) -> None:
        palette = self.palette
        steps = self._steps
        head_h = px(self.scale, 22)
        row_h = px(self.scale, 34)
        self._heading("检测步骤", "Test I / II / III 与行为采样")
        if not steps:
            self._label(0, head_h + px(self.scale, 8), "尚无步骤记录",
                        size=Typography.body, field="text_faint", anchor="w")
            return
        dot_x = px(self.scale, 7)
        radius = max(3, px(self.scale, 6))
        icon_x = dot_x + radius + px(self.scale, 7)
        text_left = icon_x + px(self.scale, 11)
        limit = max(px(self.scale, 60), width - text_left)

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
            center_y = head_h + row_h * index + row_h / 2.0
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
            self._label(
                icon_x, center_y, icon, size=Typography.body, field=field,
                bold=True, anchor="w",
            )
            self._label(
                text_left, center_y - px(self.scale, 7), step.name,
                size=Typography.body, field="text", bold=True, anchor="w",
                limit=limit,
            )
            if step.detail:
                self._label(
                    text_left, center_y + px(self.scale, 7), step.detail,
                    size=Typography.label, field="text_dim", anchor="w", limit=limit,
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

        for widget in (self.matrix, self.spectrum, self.samples,
                       self.scenarios, self.gateway, self.steps):
            widget.pack(fill="x", pady=(0, gap))

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
            return
        self.matrix.set_matrix(viz.build_matrix(nat))
        self.spectrum.set_spectrum(viz.build_spectrum(nat))
        self.samples.set_samples(viz.build_samples(nat))
        self.scenarios.set_scenarios(viz.build_scenarios(nat))
        self.gateway.set_badges(viz.build_badges(gateway))
        self.steps.set_steps(nat.steps)


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
