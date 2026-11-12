# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""检测报告导出：纯文本 TXT 与卡片式 JPG 图片。

JPG 的渲染分三层，互不越界：

1. :mod:`natscope.viz` 决定**该画什么**（矩阵、类型谱、样本 chip、徽标、场景），
   本模块只负责把模型排到像素上；
2. :func:`_plan` **只算高度**——用字体度量把每个块的高度、换行、省略全部
   算完，得到一个规划（:class:`ReportPlan`），全程不建画布；
3. :func:`_paint` 按算出来的总高建图，再逐块绘制，最后断言画布高度与规划
   一致，**绝不裁剪**。

v1.0 的固定 ``H = 1500`` 画布 + ``crop(min(y, H))`` 会把超出部分静默吃掉
（实测最坏用例约 1526px），v1.1 用两遍布局彻底删掉这条路径；单页超过
:data:`MAX_PAGE_H` 时自动分页为 ``报告_1.jpg`` / ``报告_2.jpg``，页脚标
「第 n/N 页」。

配色一律取自 :class:`natscope.theme.Palette`，NAT 类型色统一走
:func:`natscope.theme.type_color`，渲染代码里**不允许出现字面量 hex**——
换主题只需换一个 Palette 实例。
"""

from __future__ import annotations

import os
import platform
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import __version__, viz
from .gateway import GatewayReport
from .nat import NatResult
from .servers import TIER_LABEL, ProbeSummary
from .theme import Palette, Typography, type_color

if TYPE_CHECKING:  # 只在类型检查时借 PIL，运行期仍由函数内部惰性导入
    from PIL.Image import Image

FONT_CANDIDATES_BOLD = [
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
]
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\arial.ttf",
]


@dataclass
class ReportContext:
    nat: NatResult
    gateway: GatewayReport | None = None
    probe: ProbeSummary | None = None
    version: str = __version__
    generated_at: str = ""
    os_text: str = ""

    def __post_init__(self):
        if not self.generated_at:
            self.generated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        if not self.os_text:
            self.os_text = _os_text()


def _os_text() -> str:
    try:
        ver = platform.version()
        rel = platform.release()
        if ver.startswith("10.0.2"):
            build = ver.split(".")[-1]
            try:
                if int(build) >= 22000:
                    return f"Windows 11 (build {build})"
            except ValueError:
                pass
        return f"{platform.system()} {rel} (build {ver.split('.')[-1]})"
    except Exception:
        return platform.platform()


# --------------------------------------------------------------------------
# TXT
# --------------------------------------------------------------------------

_LINE = "=" * 68
_THIN = "-" * 68


def build_report_text(ctx: ReportContext) -> str:
    nat = ctx.nat
    out: list[str] = []
    add = out.append

    add(_LINE)
    add(" NatScope — NAT 类型检测报告")
    add(_LINE)
    add(f"生成时间 : {ctx.generated_at}")
    add(f"软件版本 : NatScope {ctx.version}")
    add(f"操作系统 : {ctx.os_text}")
    add("")

    add("【一、网络环境】")
    add(f"  本机地址   : {nat.local_ip}:{nat.local_port}")
    add(f"  公网映射   : {nat.public_ip}:{nat.public_port}")
    if ctx.gateway and ctx.gateway.gateway_ip:
        add(f"  默认网关   : {ctx.gateway.gateway_ip}")
    add(f"  主服务器   : {nat.primary_server}")
    if nat.rfc5780_server:
        add(f"  RFC5780 服务器 : {nat.rfc5780_server}")
    add("")

    add("【二、检测结论】")
    add(f"  传统四类型 : {nat.classic_label}")
    if nat.classic_en:
        add(f"  英文名称   : {nat.classic_en}")
    add(f"  映射行为   : {nat.mapping_label}")
    add(f"  过滤行为   : {nat.filtering_label}")
    if not nat.change_request_supported:
        add("  备注       : 本次无服务器响应 CHANGE-REQUEST，过滤行为不可测")
    add("")

    add("【三、行为矩阵】")
    add(_THIN)
    matrix = viz.build_matrix(nat)
    add("  " + _pad("", 20) + "".join(_pad(viz.COL_CODE[c], 18) for c in matrix.cols))
    for row, line in zip(matrix.rows, matrix.cells):
        cells = "".join(_pad(_cell_marker(cell) + cell.title, 18) for cell in line)
        add("  " + _pad(viz.ROW_LABEL[row], 20) + cells)
    add("  标记：* = 结论所在格   ? = 候选（无法细分）   · = 已排除   — = 不适用")
    if matrix.note:
        add(f"  说明：{matrix.note}")
    add("")

    add("【四、场景可用性】")
    for sc in viz.build_scenarios(nat):
        add(f"  {_pad(sc.label, 16)}: {viz.LEVEL_LABEL[sc.level]}")
    add("")

    add("【五、结论说明】")
    add(_THIN)
    for line in _wrap(nat.summary, 62, "  "):
        add(line)
    add("")

    add("【六、测试明细】")
    add(_THIN)
    for step in nat.steps:
        add(f"  {step.icon} {step.name}")
        if step.detail:
            for line in _wrap(step.detail, 58, "      "):
                add(line)
    if nat.samples:
        add("")
        add("  映射样本（目的地址 → 公网映射）：")
        for s in nat.samples:
            add(f"    {s.dst_ip}:{s.dst_port}  →  {s.mapped_ip}:{s.mapped_port}")
    add("")

    add("【七、网关端口映射能力】")
    add(_THIN)
    if ctx.gateway:
        gw = ctx.gateway
        add(f"  默认网关 : {gw.gateway_ip or '未获取'}")
        for cap in gw.capabilities:
            add(f"  {cap.name:<9}: {cap.state_label}" +
                (f"  外网 {cap.external_ip}" if cap.external_ip else ""))
            if cap.detail:
                for line in _wrap(cap.detail, 56, "             "):
                    add(line)
        add("")
        for line in _wrap(gw.summary, 62, "  "):
            add(line)
    else:
        add("  未执行网关探测")
    add("")

    if ctx.probe:
        p = ctx.probe
        add("【八、STUN 服务器统计】")
        add(_THIN)
        add(f"  总计 {len(p.results)} 个：优选 {len(p.preferred)} / "
            f"备选 {len(p.backup)} / 不可用 {len(p.unavailable)}")
        add("")
        add("  可用服务器（按延迟排序）：")
        for r in p.usable[:40]:
            tag = TIER_LABEL.get(r.tier, r.tier)
            extra = "  [RFC5780]" if r.supports_rfc5780 else ""
            add(f"    {tag}  {r.server.label:<34} {r.rtt_ms:6.0f} ms{extra}")
        if len(p.usable) > 40:
            add(f"    … 其余 {len(p.usable) - 40} 个略")
        if p.unavailable:
            add("")
            add("  不可用服务器：")
            for r in p.unavailable[:20]:
                add(f"    {r.server.label:<34} {r.error[:40]}")
            if len(p.unavailable) > 20:
                add(f"    … 其余 {len(p.unavailable) - 20} 个略")
        add("")

    notes = collect_notes(ctx)
    if notes:
        add("【九、提示与警告】")
        add(_THIN)
        for n in notes:
            for line in _wrap("• " + n, 62, "  "):
                add(line)
        add("")

    add(_LINE)
    add(" 说明：STUN 走 UDP，HTTP 代理（如 127.0.0.1:7890）无法代理 UDP。")
    add(" 若结果与预期不符，请确认未处于系统级代理 / VPN / 强制透明代理环境。")
    add(_LINE)
    return "\n".join(out)


def collect_notes(ctx: ReportContext) -> list[str]:
    """TXT 与 JPG 共用的提示汇总：先 nat.warnings，再 gateway.notes。

    两处以前各写一套（JPG 还漏合并了 gateway.notes），现在只剩这一个入口，
    内容与顺序在两种导出里必然一致。重复文案已去重。
    """
    merged: list[str] = []
    seen: set[str] = set()
    for item in list(ctx.nat.warnings) + (list(ctx.gateway.notes) if ctx.gateway else []):
        text = (item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        merged.append(text)
    return merged


def _display_width(text: str) -> int:
    """按显示宽度计字符（CJK 记 2 列），用于 TXT 里的列对齐。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    """把文本右补空格到指定显示宽度（中英混排也能对齐）。"""
    return text + " " * max(0, width - _display_width(text))


def _cell_marker(cell: viz.Cell) -> str:
    """TXT 矩阵里的状态标记：结论 / 候选 / 已排除 / 不适用。"""
    return {
        viz.STATE_ACTIVE: "*",
        viz.STATE_RELATED: "?",
        viz.STATE_NA: "—",
    }.get(cell.state, "·")


def _tokenize(text: str) -> list[str]:
    """切成换行用的最小单元：CJK/全角标点逐字成词，ASCII 连续串整词成词。

    整词成词是为了不让 ``CHANGE-REQUEST`` 这种术语被拦腰截断——
    v1.0 的逐字符换行会把 ``-REQUEST`` 甩到下一行，读起来像手滑。
    """
    tokens: list[str] = []
    cur = ""
    for ch in text:
        if ch == "\n":
            if cur:
                tokens.append(cur)
                cur = ""
            tokens.append("\n")
        elif ch == " ":
            # 空格单独成词：换行时它会被丢掉，不会在行首留出一格空白
            cur += ch
            tokens.append(cur)
            cur = ""
        elif ord(ch) > 0x2E80:
            if cur:
                tokens.append(cur)
                cur = ""
            tokens.append(ch)
        else:
            cur += ch
    if cur:
        tokens.append(cur)
    return tokens


def _wrap(text: str, width: int, indent: str = "") -> list[str]:
    """按显示宽度换行（中文按 2 列计），优先在词边界断开。"""
    lines: list[str] = []
    cur = ""
    cur_w = 0
    for token in _tokenize(text):
        if token == "\n":
            lines.append(indent + cur)
            cur, cur_w = "", 0
            continue
        if not cur and token.isspace():
            continue  # 断行处的空格不该跑到下一行行首
        w = _display_width(token)
        if w > width:  # 单词本身超宽：退化为逐字符硬切
            for ch in token:
                cw = _display_width(ch)
                if cur_w + cw > width:
                    lines.append(indent + cur)
                    cur, cur_w = "", 0
                cur += ch
                cur_w += cw
            continue
        if cur_w + w > width:
            lines.append(indent + cur)
            cur, cur_w = "", 0
            if token.isspace():
                continue
        cur += token
        cur_w += w
    if cur or not lines:
        lines.append(indent + cur)
    return lines


def save_text(ctx: ReportContext, path: str) -> str:
    content = build_report_text(ctx)
    with open(path, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(content)
    return path


# --------------------------------------------------------------------------
# JPG — 字体度量
# --------------------------------------------------------------------------

IMG_W = 960
PAD = 40
#: 单页最大高度（1x 像素）。超过即自动分页。
MAX_PAGE_H = 4000
#: Typography 里的点值 -> Pillow 像素字号
PX_PER_PT = 1.45
#: 提示块最多渲染多少条（再多就指向 TXT，避免无限长图）
MAX_NOTE_ITEMS = 24
#: 测试明细每张卡最多多少行（保证单块高度远小于页高，分页永远切得动）
_STEPS_PER_BLOCK = 22

#: 字体角色 -> (Typography 令牌, 是否加粗)
_ROLE_SPEC: dict[str, tuple[int, bool]] = {
    "brand": (Typography.title, True),
    "brand_sub": (Typography.note, False),
    "display": (Typography.display, True),
    "display_sub": (Typography.value, False),
    "section": (Typography.heading, True),
    "label": (Typography.label, False),
    "value": (Typography.value, True),
    "body": (Typography.body, False),
    "small": (Typography.note, False),
    "tiny": (Typography.label, False),
    "mono": (Typography.mono, False),
    "cell": (Typography.value, True),
}


def _load_font(size: int, bold: bool = False):
    from PIL import ImageFont

    for path in FONT_CANDIDATES_BOLD if bold else FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


class _Metrics:
    """字体度量与三分（换行 / 省略 / 宽度）。

    单字宽度走缓存：``_wrap_px`` 以前对每个字符都调一次 ``textbbox``，
    一段 300 字的说明就是 300 次全串测量（O(n²)）。现在每个 (角色, 字符)
    只测一次，长度累积即可。
    """

    def __init__(self, scale: float = 1.0) -> None:
        self.scale = float(scale) if scale > 0 else 1.0
        self._fonts: dict[str, object] = {}
        self._char_w: dict[tuple[str, str], float] = {}

    def px(self, pt: int) -> int:
        """点值 -> 像素字号（按导出倍率放大）。"""
        return max(8, int(round(pt * PX_PER_PT * self.scale)))

    def font(self, role: str):
        cached = self._fonts.get(role)
        if cached is None:
            token, bold = _ROLE_SPEC[role]
            cached = _load_font(self.px(token), bold)
            self._fonts[role] = cached
        return cached

    def line(self, role: str) -> int:
        """行距：字体的 ascent+descent，再留 20% 呼吸。"""
        font = self.font(role)
        try:
            ascent, descent = font.getmetrics()
            base = ascent + descent
        except Exception:  # 位图兜底字体没有 getmetrics
            base = self.px(_ROLE_SPEC[role][0])
        return max(1, int(round(base * 1.2)))

    def width(self, role: str, text: str) -> int:
        if not text:
            return 0
        font = self.font(role)
        try:
            return int(round(font.getlength(text)))
        except AttributeError:  # 极老的 Pillow
            return int(round(len(text) * self.px(_ROLE_SPEC[role][0]) * 0.55))

    def _char(self, role: str, ch: str) -> float:
        key = (role, ch)
        w = self._char_w.get(key)
        if w is None:
            font = self.font(role)
            try:
                w = float(font.getlength(ch))
            except AttributeError:
                w = self.px(_ROLE_SPEC[role][0]) * 0.55
            self._char_w[key] = w
        return w

    def wrap(self, role: str, text: str, max_px: int) -> list[str]:
        """按像素宽度换行，优先在词边界断开（ASCII 单词不拦腰截断）。"""
        lines: list[str] = []
        cur = ""
        cur_w = 0.0
        for token in _tokenize(text):
            if token == "\n":
                lines.append(cur)
                cur, cur_w = "", 0.0
                continue
            if not cur and token.isspace():
                continue
            w = self.width(role, token)
            if w > max_px:  # 单词本身超宽：退化为逐字符硬切
                for ch in token:
                    cw = self._char(role, ch)
                    if cur and cur_w + cw > max_px:
                        lines.append(cur)
                        cur, cur_w = "", 0.0
                    cur += ch
                    cur_w += cw
                continue
            if cur and cur_w + w > max_px:
                lines.append(cur)
                cur, cur_w = "", 0.0
                if token.isspace():
                    continue
            cur += token
            cur_w += w
        if cur or not lines:
            lines.append(cur)
        return lines

    def ellipsize(self, role: str, text: str, max_px: int) -> str:
        """像素级截断加省略号——中英混排按字符数截会错位。"""
        if max_px <= 0:
            return ""
        if self.width(role, text) <= max_px:
            return text
        ell = "…"
        budget = max_px - self.width(role, ell)
        if budget <= 0:
            return ell
        out: list[str] = []
        used = 0.0
        for ch in text:
            w = self._char(role, ch)
            if used + w > budget:
                break
            out.append(ch)
            used += w
        return "".join(out).rstrip() + ell


# --------------------------------------------------------------------------
# JPG — 最小场景图元
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Line:
    """一段文字（坐标相对所属块顶部）。"""

    text: str
    role: str
    color: str
    x: int
    dy: int
    anchor: str = "la"


@dataclass(frozen=True)
class _Shape:
    """一个几何图元。``kind`` ∈ rect / rrect / line / ellipse / poly。"""

    kind: str
    box: tuple[int, int, int, int] = (0, 0, 0, 0)
    fill: str = ""
    outline: str = ""
    width: int = 1
    radius: int = 0
    points: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class _Block:
    """一个排版块：高度在规划阶段就定死，绘制阶段只按坐标画。"""

    kind: str
    height: int
    tail: int = 0
    shapes: tuple[_Shape, ...] = ()
    lines: tuple[_Line, ...] = ()

    @property
    def body_h(self) -> int:
        """块本体的高度（不含尾部间距）。"""
        return self.height - self.tail


@dataclass(frozen=True)
class PagePlan:
    """一页的排版结果。``height`` 就是最终画布高度。"""

    blocks: tuple[_Block, ...]
    height: int

    @property
    def body_h(self) -> int:
        return sum(b.height for b in self.blocks) - (self.blocks[-1].tail if self.blocks else 0)


@dataclass(frozen=True)
class ReportPlan:
    """整份报告的排版规划（可能有多页）。"""

    width: int
    scale: float
    pages: tuple[PagePlan, ...]

    @property
    def heights(self) -> tuple[int, ...]:
        return tuple(p.height for p in self.pages)

    @property
    def height(self) -> int:
        """单页报告的画布高度（多页时返回首页高度）。"""
        return self.pages[0].height if self.pages else 0

    @property
    def texts(self) -> tuple[str, ...]:
        """规划里的全部文字，供测试断言「没有内容被丢掉」。"""
        return tuple(
            line.text for page in self.pages for block in page.blocks for line in block.lines
        )


def _mix_hex(a: str, b: str, t: float) -> str:
    """两个 ``#RRGGBB`` 按比例混合，``t`` 为 ``b`` 的占比。"""
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)

    def parts(x: str) -> tuple[int, int, int]:
        s = x.lstrip("#")
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)

    ar, ag, ab = parts(a)
    br, bg, bb = parts(b)
    return "#{:02X}{:02X}{:02X}".format(
        int(round(ar + (br - ar) * t)),
        int(round(ag + (bg - ag) * t)),
        int(round(ab + (bb - ab) * t)),
    )


def _tone_color(p: Palette, tone: str) -> str:
    """语义色调 -> Palette 颜色（唯一入口，别在别处再写一份）。"""
    return {
        viz.TONE_OK: p.ok,
        viz.TONE_WARN: p.warn,
        viz.TONE_BAD: p.err,
        viz.TONE_NA: p.text_dim,
    }.get(tone, p.text_dim)


def _gateway_short(gw: GatewayReport | None) -> str:
    """三小卡里「端口映射」的值：只列真正可用的服务名。"""
    if gw is None:
        return "未检测"
    names = [
        c.name.replace("UPnP-IGD", "UPnP").replace("NAT-PMP", "PMP")
        for c in gw.capabilities
        if c.available
    ]
    if names:
        return " / ".join(names)
    if any(c.state == "limited" for c in gw.capabilities):
        return "存在但受限"
    return "未发现"


# --------------------------------------------------------------------------
# JPG — 规划（只算高度）
# --------------------------------------------------------------------------


class _Planner:
    """把 ReportContext 排成一串 :class:`_Block`，全程不建画布。"""

    def __init__(self, ctx: ReportContext, palette: Palette, metrics: _Metrics, width: int):
        self.ctx = ctx
        self.p = palette
        self.m = metrics
        self.width = width
        self.scale = width / IMG_W

    # ---- 几何 ----
    def s(self, v: float) -> int:
        return int(round(v * self.scale))

    @property
    def pad(self) -> int:
        return self.s(PAD)

    @property
    def cw(self) -> int:
        """内容区宽度（去掉左右页边距）。"""
        return self.width - self.pad * 2

    def _card(self, x0: int, x1: int, h: int, fill: str, outline: str, radius: int = 14) -> _Shape:
        return _Shape(
            "rrect",
            (x0, 0, x1, h - 1),
            fill=fill,
            outline=outline,
            radius=self.s(radius),
        )

    # ---- 各块 ----

    def header(self) -> _Block:
        p, m = self.p, self.m
        h = self.s(96)
        x1 = self.width - self.pad
        brand_w = m.width("brand", "NatScope")
        return _Block(
            kind="header",
            height=h + self.s(24),
            tail=self.s(24),
            shapes=(_Shape("rect", (0, 0, self.width, h - 1), fill=p.header),),
            lines=(
                _Line("NatScope", "brand", p.header_text, self.pad, h // 2, "lm"),
                _Line(
                    "NAT 类型检测报告",
                    "brand_sub",
                    p.header_text,
                    self.pad + brand_w + self.s(16),
                    h // 2 + self.s(2),
                    "lm",
                ),
                _Line(self.ctx.generated_at, "small", p.header_text, x1, h // 2, "rm"),
            ),
        )

    def verdict(self) -> _Block:
        p, m = self.p, self.m
        nat = self.ctx.nat
        title, sub, _tone = viz.verdict(nat)
        color = type_color(p, nat.classic_type)
        padx = self.s(26)
        lab_y = self.s(16)
        title_y = lab_y + m.line("label") + self.s(4)
        sub_y = title_y + m.line("display") + self.s(2)
        h = sub_y + m.line("display_sub") + self.s(18)
        codes = f"{nat.mapping_behavior.upper()} / {nat.filtering_behavior.upper()}"
        codes_w = m.width("body", codes) + self.s(24)
        text_max = self.cw - padx * 2 - codes_w
        return _Block(
            kind="verdict",
            height=h + self.s(18),
            tail=self.s(18),
            shapes=(
                self._card(self.pad, self.width - self.pad, h, p.surface, p.border, 16),
                # 强调条上下内缩，避免方角顶出卡片的圆角轮廓
                _Shape(
                    "rrect",
                    (self.pad, self.s(8), self.pad + self.s(6), h - 1 - self.s(8)),
                    fill=color,
                    radius=self.s(3),
                ),
            ),
            lines=(
                _Line("检测结论", "label", p.text_dim, self.pad + padx, lab_y),
                _Line(
                    m.ellipsize("display", title, text_max),
                    "display",
                    color,
                    self.pad + padx,
                    title_y,
                ),
                _Line(
                    m.ellipsize("display_sub", sub, text_max),
                    "display_sub",
                    p.text_dim,
                    self.pad + padx,
                    sub_y,
                ),
                _Line(codes, "body", p.text_faint, self.width - self.pad - padx, sub_y + self.s(2), "ra"),
            ),
        )

    def trio(self) -> _Block:
        p, m = self.p, self.m
        nat, gw = self.ctx.nat, self.ctx.gateway
        gap = self.s(18)
        cw3 = (self.cw - gap * 2) // 3
        label_y = self.s(16)
        value_y = label_y + m.line("label") + self.s(6)
        h = value_y + m.line("value") + self.s(16)

        gw_color = p.text_dim
        if gw is not None:
            gw_color = p.ok if gw.any_available else (
                p.warn if any(c.state == "limited" for c in gw.capabilities) else p.text_dim
            )
        items = (
            ("映射行为 Mapping", viz.MAPPING_LABEL.get(nat.mapping_behavior, "不可测"), p.accent),
            (
                "过滤行为 Filtering",
                viz.FILTERING_LABEL.get(nat.filtering_behavior, "不可测"),
                p.accent,
            ),
            ("端口映射 Gateway", _gateway_short(gw), gw_color),
        )
        shapes: list[_Shape] = []
        lines: list[_Line] = []
        for i, (label, value, color) in enumerate(items):
            x0 = self.pad + i * (cw3 + gap)
            x1 = x0 + cw3
            shapes.append(self._card(x0, x1, h, p.surface, p.border))
            lines.append(_Line(label, "label", p.text_faint, x0 + self.s(18), label_y))
            lines.append(
                _Line(
                    m.ellipsize("value", value, cw3 - self.s(36)),
                    "value",
                    color,
                    x0 + self.s(18),
                    value_y,
                )
            )
        return _Block(
            kind="trio",
            height=h + self.s(18),
            tail=self.s(18),
            shapes=tuple(shapes),
            lines=tuple(lines),
        )

    def spectrum(self) -> _Block:
        p, m = self.p, self.m
        segs = viz.build_spectrum(self.ctx.nat)
        if not segs:
            return _Block(kind="spectrum", height=0, tail=0)
        inner = self.s(20)
        gap = self.s(6)
        x0 = self.pad + inner
        inner_w = self.cw - inner * 2
        title_lh = m.line("section")
        bar_h = self.s(12)
        title_y = self.s(18)
        span = viz.spectrum_span(segs)
        bar_y = title_y + title_lh + self.s(16)
        label_y = bar_y + bar_h + self.s(14)
        sub_y = label_y + m.line("small") + self.s(2)
        span_y = sub_y + m.line("tiny") + self.s(8)
        h = (span_y + m.line("tiny") if span else sub_y + m.line("tiny")) + self.s(18)

        n = len(segs)
        seg_w = max(self.s(40), (inner_w - gap * (n - 1)) // n)

        current = [s for s in segs if s.state == "current"]
        if current:
            caption = "当前 · " + current[0].label
            cap_tone = current[0].tone
        elif span:
            # 候选是「落在区间内」，不是「三个都命中」，措辞别写成并列
            caption = "可能 · 落在区间内"
            cap_tone = viz.TONE_WARN
        else:
            caption = "结论未定"
            cap_tone = viz.TONE_NA

        warn = p.warn
        cand_fill = _mix_hex(p.surface, warn, 0.28)
        grow = self.s(2)
        first_x = last_x = 0
        mid = 0
        shapes: list[_Shape] = [self._card(self.pad, self.width - self.pad, h, p.surface, p.border)]
        lines: list[_Line] = [
            _Line("类型谱 Spectrum", "section", p.text, x0, title_y),
            _Line(
                caption,
                "small",
                _tone_color(p, cap_tone),
                self.width - self.pad - inner,
                title_y + title_lh // 2,
                "rm",
            ),
        ]

        if span is not None:
            # 三段候选 = **一条**区间条（不是三个胶囊）：整段画一个略加粗的胶囊，
            # 内部再用卡片底色细线分出三段，读作「落在这三段构成的区间内」。
            first_x = x0 + span.start * (seg_w + gap)
            last_x = x0 + span.end * (seg_w + gap) + seg_w
            mid = (first_x + last_x) // 2
            shapes.append(
                _Shape(
                    "rrect",
                    (first_x, bar_y - grow, last_x, bar_y + bar_h + grow),
                    fill=cand_fill,
                    outline=warn,
                    radius=max(1, bar_h // 2 + grow),
                )
            )
            for k in range(span.start, span.end):
                x_div = x0 + k * (seg_w + gap) + seg_w + gap // 2
                shapes.append(
                    _Shape(
                        "line",
                        (x_div, bar_y + self.s(1), x_div, bar_y + bar_h - self.s(1)),
                        fill=p.surface,
                        width=max(1, self.s(1)),
                    )
                )

        for i, seg in enumerate(segs):
            sx = x0 + i * (seg_w + gap)
            cx = sx + seg_w // 2
            tone = _tone_color(p, seg.tone)
            in_span = span is not None and span.start <= i <= span.end
            if seg.state == "current":
                fill, outline, text_color = tone, tone, p.text
            elif seg.state == "candidate":
                # 候选一律用「不确定」的警示色，不跟着各自的语义色走：
                # 完全锥型那段是绿色的话，看起来像「这个已经确定了」。
                fill, outline, text_color = cand_fill, warn, p.text_dim
            else:
                fill, outline, text_color = p.overlay, p.grid, p.text_faint
            if not in_span:  # 候选段整体由上面那条区间条表达
                shapes.append(
                    _Shape(
                        "rrect",
                        (sx, bar_y, sx + seg_w, bar_y + bar_h),
                        fill=fill,
                        outline=outline,
                        radius=max(1, bar_h // 2),
                    )
                )
            lines.append(_Line(seg.label, "small", text_color, cx, label_y, "ma"))
            lines.append(_Line(seg.sub, "tiny", p.text_faint, cx, sub_y, "ma"))

        tip = self.s(9)
        if span is not None:
            # 区间中央放**一个**三角。三段各画一个会读成「三个都命中」或「要挑一个」。
            base_y = bar_y - grow - self.s(1)
            shapes.append(
                _Shape(
                    "poly",
                    points=(
                        (mid - tip // 2, base_y - tip),
                        (mid + tip // 2, base_y - tip),
                        (mid, base_y),
                    ),
                    fill=warn,
                )
            )
            lines.append(
                _Line(
                    m.ellipsize("tiny", span.label, last_x - first_x),
                    "tiny",
                    warn,
                    mid,
                    span_y,
                    "ma",
                )
            )
        elif current:
            cx = x0 + segs.index(current[0]) * (seg_w + gap) + seg_w // 2
            shapes.append(
                _Shape(
                    "poly",
                    points=(
                        (cx - tip // 2, bar_y - tip),
                        (cx + tip // 2, bar_y - tip),
                        (cx, bar_y - self.s(1)),
                    ),
                    fill=_tone_color(p, current[0].tone),
                )
            )
        return _Block(
            kind="spectrum",
            height=h + self.s(18),
            tail=self.s(18),
            shapes=tuple(shapes),
            lines=tuple(lines),
        )

    def matrix(self) -> _Block:
        p, m = self.p, self.m
        mat = viz.build_matrix(self.ctx.nat)
        inner = self.s(20)
        gap = self.s(8)
        x0 = self.pad + inner
        inner_w = self.cw - inner * 2
        row_head_w = self.s(96)
        col_w = max(self.s(80), (inner_w - row_head_w - gap * 3) // 3)
        head_lh = m.line("small")
        code_lh = m.line("tiny")
        head_h = head_lh + self.s(2) + code_lh
        cell_h = self.s(64)
        pill = self.s(7)

        title_y = self.s(18)
        head_y = title_y + m.line("section") + self.s(14)
        grid_y = head_y + head_h + self.s(10)
        grid_h = cell_h * 3 + gap * 2
        note_lines: list[str] = []
        if mat.note:
            note_lines = m.wrap("tiny", "说明：" + mat.note, inner_w)
        note_y = grid_y + grid_h + self.s(12)
        h = note_y + len(note_lines) * m.line("tiny") + self.s(16)

        shapes: list[_Shape] = [self._card(self.pad, self.width - self.pad, h, p.surface, p.border)]
        lines: list[_Line] = [_Line("行为矩阵 Behavior Matrix", "section", p.text, x0, title_y)]

        # 列头
        for j, col in enumerate(mat.cols):
            cx0 = x0 + row_head_w + gap + j * (col_w + gap)
            cx = cx0 + col_w // 2
            lines.append(
                _Line(
                    m.ellipsize("small", viz.COL_LABEL[col], col_w - self.s(6)),
                    "small",
                    p.text_dim,
                    cx,
                    head_y,
                    "ma",
                )
            )
            lines.append(
                _Line(viz.COL_CODE[col], "tiny", p.accent, cx, head_y + head_lh + self.s(2), "ma")
            )

        # 行头 + 单元格
        for i, row in enumerate(mat.rows):
            ry = grid_y + i * (cell_h + gap)
            lines.append(
                _Line(
                    m.ellipsize("small", viz.ROW_SHORT[row], row_head_w - self.s(8)),
                    "small",
                    p.text_dim,
                    x0,
                    ry + cell_h // 2 - self.s(9),
                )
            )
            lines.append(
                _Line(viz.ROW_CODE[row], "tiny", p.accent, x0, ry + cell_h // 2 + self.s(6))
            )
            for j, cell in enumerate(mat.cells[i]):
                cx0 = x0 + row_head_w + gap + j * (col_w + gap)
                cx1 = cx0 + col_w
                # 候选格用统一的候选色（display_tone），不显示各自的类型色：
                # 「完全锥型」那格若是绿色，会读出「它更可能是对的」，
                # 而结论恰恰是「三者分不出来」。
                tone = _tone_color(p, cell.display_tone)
                if cell.state == viz.STATE_ACTIVE:
                    fill = _mix_hex(p.surface, tone, 0.18)
                    outline = tone
                    title_color = tone
                elif cell.state == viz.STATE_RELATED:
                    fill = p.surface  # 无填充：只有统一描边 + 统一色点
                    outline = tone
                    title_color = p.text
                elif cell.state == viz.STATE_NA:
                    fill = p.overlay
                    outline = p.grid
                    title_color = p.text_faint
                else:
                    fill = p.surface_alt
                    outline = p.grid
                    title_color = p.text_faint
                shapes.append(
                    _Shape(
                        "rrect",
                        (cx0, ry, cx1, ry + cell_h),
                        fill=fill,
                        outline=outline,
                        radius=self.s(8),
                    )
                )
                cy = ry + cell_h // 2
                lines.append(
                    _Line(
                        m.ellipsize("cell", cell.title, col_w - self.s(16)),
                        "cell",
                        title_color,
                        cx0 + col_w // 2,
                        cy - self.s(7),
                        "mm",
                    )
                )
                lines.append(
                    _Line(
                        m.ellipsize("tiny", cell.sub, col_w - self.s(16)),
                        "tiny",
                        p.text_faint,
                        cx0 + col_w // 2,
                        cy + self.s(5),
                        "ma",
                    )
                )
                if cell.state == viz.STATE_RELATED:
                    dot = self.s(3)
                    shapes.append(
                        _Shape(
                            "ellipse",
                            (
                                cx1 - self.s(14) - dot,
                                ry + self.s(12) - dot,
                                cx1 - self.s(14) + dot,
                                ry + self.s(12) + dot,
                            ),
                            fill=tone,
                        )
                    )

        for k, text in enumerate(note_lines):
            lines.append(
                _Line(text, "tiny", p.text_dim, x0, note_y + k * m.line("tiny"))
            )

        return _Block(
            kind="matrix",
            height=h + self.s(18),
            tail=self.s(18),
            shapes=tuple(shapes),
            lines=tuple(lines),
        )

    def info(self) -> _Block:
        p, m = self.p, self.m
        nat, gw = self.ctx.nat, self.ctx.gateway
        inner = self.s(20)
        x0 = self.pad + inner
        inner_w = self.cw - inner * 2
        label_y = self.s(16)
        row_h = max(m.line("mono"), m.line("small")) + self.s(10)
        rows_y = label_y + m.line("label") + self.s(10)
        h = rows_y + row_h * 2 + self.s(14)
        col_w = inner_w // 2
        label_w = self.s(72)
        rows = (
            ("本机地址", f"{nat.local_ip}:{nat.local_port}"),
            ("公网映射", f"{nat.public_ip}:{nat.public_port}"),
            ("默认网关", (gw.gateway_ip if gw else "") or "—"),
            ("主 STUN", nat.primary_server or "—"),
        )
        shapes: list[_Shape] = [self._card(self.pad, self.width - self.pad, h, p.surface, p.border)]
        lines: list[_Line] = [_Line("网络信息", "label", p.text_dim, x0, label_y)]
        for i, (k, v) in enumerate(rows):
            r, c = divmod(i, 2)
            cx = x0 + c * col_w
            cy = rows_y + r * row_h
            lines.append(_Line(k, "tiny", p.text_faint, cx, cy + self.s(3)))
            lines.append(
                _Line(
                    m.ellipsize("mono", v, col_w - label_w - self.s(16)),
                    "mono",
                    p.text,
                    cx + label_w,
                    cy,
                )
            )
        return _Block(
            kind="info",
            height=h + self.s(18),
            tail=self.s(18),
            shapes=tuple(shapes),
            lines=tuple(lines),
        )

    def summary(self) -> _Block:
        p, m = self.p, self.m
        inner = self.s(20)
        x0 = self.pad + inner
        inner_w = self.cw - inner * 2
        label_y = self.s(16)
        body_y = label_y + m.line("label") + self.s(10)
        body = m.wrap("body", self.ctx.nat.summary, inner_w)
        lh = m.line("body")
        h = body_y + len(body) * lh + self.s(14)
        shapes = [self._card(self.pad, self.width - self.pad, h, p.surface_alt, p.border)]
        lines = [_Line("结论说明", "label", p.text_dim, x0, label_y)]
        for i, text in enumerate(body):
            lines.append(_Line(text, "body", p.text, x0, body_y + i * lh))
        return _Block(
            kind="summary",
            height=h + self.s(18),
            tail=self.s(18),
            shapes=tuple(shapes),
            lines=tuple(lines),
        )

    def scenarios(self) -> _Block:
        p, m = self.p, self.m
        items = viz.build_scenarios(self.ctx.nat)
        inner = self.s(20)
        gap = self.s(12)
        x0 = self.pad + inner
        inner_w = self.cw - inner * 2
        chip_w = max(self.s(80), (inner_w - gap * (len(items) - 1)) // max(1, len(items)))
        title_y = self.s(18)
        chip_y = title_y + m.line("section") + self.s(12)
        label_y = chip_y + self.s(14)
        hint_y = label_y + m.line("small") + self.s(6)
        chip_h = (hint_y - chip_y) + m.line("value") + self.s(14)
        h = chip_y + chip_h + self.s(16)

        shapes: list[_Shape] = [self._card(self.pad, self.width - self.pad, h, p.surface, p.border)]
        lines: list[_Line] = [_Line("场景可用性 Scenarios", "section", p.text, x0, title_y)]
        for i, sc in enumerate(items):
            sx = x0 + i * (chip_w + gap)
            tone = _tone_color(p, sc.tone)
            shapes.append(
                _Shape(
                    "rrect",
                    (sx, chip_y, sx + chip_w, chip_y + chip_h),
                    fill=p.surface_alt,
                    outline=p.grid,
                    radius=self.s(10),
                )
            )
            dot = self.s(4)
            shapes.append(
                _Shape(
                    "ellipse",
                    (
                        sx + self.s(14) - dot,
                        label_y + m.line("small") // 2 - dot,
                        sx + self.s(14) + dot,
                        label_y + m.line("small") // 2 + dot,
                    ),
                    fill=tone,
                )
            )
            lines.append(
                _Line(
                    m.ellipsize("small", sc.label, chip_w - self.s(32)),
                    "small",
                    p.text_dim,
                    sx + self.s(26),
                    label_y,
                )
            )
            lines.append(
                _Line(
                    m.ellipsize("value", sc.hint, chip_w - self.s(28)),
                    "value",
                    tone,
                    sx + self.s(14),
                    hint_y,
                )
            )
        return _Block(
            kind="scenarios",
            height=h + self.s(18),
            tail=self.s(18),
            shapes=tuple(shapes),
            lines=tuple(lines),
        )

    def steps(self) -> list[_Block]:
        """测试明细；行数过多时拆成多张卡（续），避免单块高到无法分页。"""
        steps = self.ctx.nat.steps
        if not steps:
            return [self._steps_card([], 0)]
        chunks = [
            steps[i:i + _STEPS_PER_BLOCK] for i in range(0, len(steps), _STEPS_PER_BLOCK)
        ]
        return [self._steps_card(chunk, k) for k, chunk in enumerate(chunks)]

    def _steps_card(self, steps: list, part: int) -> _Block:
        p, m = self.p, self.m
        inner = self.s(20)
        x0 = self.pad + inner
        inner_w = self.cw - inner * 2
        label_y = self.s(16)
        row_y = label_y + m.line("label") + self.s(10)
        row_h = max(m.line("body"), m.line("small")) + self.s(9)
        if steps:
            h = row_y + len(steps) * row_h + self.s(12)
        else:
            h = row_y + m.line("small") + self.s(14)
        name_w = self.s(238)
        detail_x = x0 + name_w + self.s(28)
        detail_w = x0 + inner_w - detail_x
        title = "测试明细" if part == 0 else f"测试明细（续 {part + 1}）"
        shapes: list[_Shape] = [self._card(self.pad, self.width - self.pad, h, p.surface, p.border)]
        lines: list[_Line] = [_Line(title, "label", p.text_dim, x0, label_y)]
        if not steps:
            lines.append(_Line("没有执行记录", "small", p.text_faint, x0, row_y))
        for i, step in enumerate(steps):
            sy = row_y + i * row_h
            color = {"ok": p.ok, "fail": p.err, "skip": p.text_faint}.get(step.status, p.text_dim)
            lines.append(_Line(step.icon, "body", color, x0, sy))
            lines.append(
                _Line(m.ellipsize("body", step.name, name_w), "body", p.text, x0 + self.s(22), sy)
            )
            if step.detail:
                lines.append(
                    _Line(
                        m.ellipsize("small", step.detail, detail_w),
                        "small",
                        p.text_dim,
                        detail_x,
                        sy + self.s(1),
                    )
                )
        return _Block(
            kind="steps",
            height=h + self.s(18),
            tail=self.s(18),
            shapes=tuple(shapes),
            lines=tuple(lines),
        )

    def samples(self) -> _Block:
        p, m = self.p, self.m
        chips = viz.build_samples(self.ctx.nat)
        if not chips:
            return _Block(kind="samples", height=0, tail=0)
        inner = self.s(20)
        gap = self.s(12)
        x0 = self.pad + inner
        inner_w = self.cw - inner * 2
        chip_w = (inner_w - gap) // 2
        label_y = self.s(16)
        first_y = label_y + m.line("label") + self.s(10)
        mono_lh = m.line("mono")
        tiny_lh = m.line("tiny")
        chip_h = self.s(10) + mono_lh + self.s(2) + tiny_lh + self.s(10)
        rows = (len(chips) + 1) // 2
        h = first_y + rows * chip_h + (rows - 1) * gap + self.s(14)

        shapes: list[_Shape] = [self._card(self.pad, self.width - self.pad, h, p.surface, p.border)]
        lines: list[_Line] = [
            _Line("映射样本 Mapping Samples", "label", p.text_dim, x0, label_y)
        ]
        for i, chip in enumerate(chips):
            r, c = divmod(i, 2)
            sx = x0 + c * (chip_w + gap)
            sy = first_y + r * (chip_h + gap)
            tone = _tone_color(p, chip.tone)
            shapes.append(
                _Shape(
                    "rrect",
                    (sx, sy, sx + chip_w, sy + chip_h),
                    fill=p.surface_alt,
                    outline=p.grid,
                    radius=self.s(8),
                )
            )
            lines.append(
                _Line(
                    f"#{chip.index}  " + chip.text,
                    "mono",
                    p.text,
                    sx + self.s(12),
                    sy + self.s(10),
                )
            )
            tag = "端口一致" if chip.stable else "端口漂移"
            lines.append(
                _Line(
                    m.ellipsize("tiny", f"{tag} · {chip.server}", chip_w - self.s(24)),
                    "tiny",
                    tone,
                    sx + self.s(12),
                    sy + self.s(10) + mono_lh + self.s(2),
                )
            )
        return _Block(
            kind="samples",
            height=h + self.s(18),
            tail=self.s(18),
            shapes=tuple(shapes),
            lines=tuple(lines),
        )

    def gateway(self) -> _Block:
        p, m = self.p, self.m
        gw = self.ctx.gateway
        badges = viz.build_badges(gw)
        inner = self.s(20)
        gap = self.s(12)
        x0 = self.pad + inner
        inner_w = self.cw - inner * 2
        label_y = self.s(16)
        row_y = label_y + m.line("label") + self.s(10)
        badge_w = max(self.s(80), (inner_w - gap * (len(badges) - 1)) // max(1, len(badges)))
        badge_w = min(badge_w, self.s(230))
        badge_lh = m.line("value")
        badge_sub_lh = m.line("tiny")
        badge_h = badge_lh + self.s(2) + badge_sub_lh + self.s(8)
        body_y = row_y + badge_h + self.s(12)
        body = m.wrap("body", gw.summary if gw else "本次未执行网关探测。", inner_w)
        body_lh = m.line("body")
        h = body_y + len(body) * body_lh + self.s(14)

        shapes: list[_Shape] = [self._card(self.pad, self.width - self.pad, h, p.surface, p.border)]
        lines: list[_Line] = [
            _Line("网关端口映射能力 Gateway", "label", p.text_dim, x0, label_y)
        ]
        for i, badge in enumerate(badges):
            sx = x0 + i * (badge_w + gap)
            tone = _tone_color(p, badge.tone)
            shapes.append(
                _Shape(
                    "rrect",
                    (sx, row_y, sx + badge_w, row_y + badge_h),
                    fill=p.surface_alt,
                    outline=p.grid,
                    radius=self.s(8),
                )
            )
            lines.append(
                _Line(
                    m.ellipsize("value", badge.value, badge_w - self.s(20)),
                    "value",
                    tone,
                    sx + self.s(10),
                    row_y + self.s(4),
                )
            )
            lines.append(
                _Line(
                    m.ellipsize("tiny", badge.label, badge_w - self.s(20)),
                    "tiny",
                    p.text_faint,
                    sx + self.s(10),
                    row_y + self.s(4) + badge_lh + self.s(2),
                )
            )
        for i, text in enumerate(body):
            lines.append(_Line(text, "body", p.text, x0, body_y + i * body_lh))
        return _Block(
            kind="gateway",
            height=h + self.s(18),
            tail=self.s(18),
            shapes=tuple(shapes),
            lines=tuple(lines),
        )

    def servers(self) -> _Block:
        p, m = self.p, self.m
        pr = self.ctx.probe
        if pr is None:
            return _Block(kind="servers", height=0, tail=0)
        inner = self.s(20)
        x0 = self.pad + inner
        label_y = self.s(16)
        row_y = label_y + m.line("label") + self.s(10)
        num_lh = m.line("display_sub")
        cap_lh = m.line("small")
        h = row_y + num_lh + self.s(4) + cap_lh + self.s(16)

        stats = (
            ("总计", str(len(pr.results)), p.text),
            ("优选", str(len(pr.preferred)), p.ok),
            ("备选", str(len(pr.backup)), p.warn),
            ("不可用", str(len(pr.unavailable)), p.err),
            ("RFC5780", str(len(pr.rfc5780_servers)), p.accent),
        )
        step = self.cw // len(stats)
        shapes: list[_Shape] = [self._card(self.pad, self.width - self.pad, h, p.surface, p.border)]
        lines: list[_Line] = [_Line("STUN 服务器", "label", p.text_dim, x0, label_y)]
        for i, (label, value, color) in enumerate(stats):
            sx = x0 + i * step
            lines.append(_Line(value, "display_sub", color, sx, row_y))
            lines.append(_Line(label, "small", p.text_faint, sx, row_y + num_lh + self.s(4)))
        return _Block(
            kind="servers",
            height=h + self.s(18),
            tail=self.s(18),
            shapes=tuple(shapes),
            lines=tuple(lines),
        )

    def notes(self) -> _Block:
        """提示与警告：**全部**渲染（v1.0 的 ``notes[:4]`` 会把第 5 条静默丢掉）。"""
        p, m = self.p, self.m
        items = collect_notes(self.ctx)
        if not items:
            return _Block(kind="notes", height=0, tail=0)
        truncated = max(0, len(items) - MAX_NOTE_ITEMS)
        shown = items[:MAX_NOTE_ITEMS]
        inner = self.s(20)
        x0 = self.pad + inner
        inner_w = self.cw - inner * 2
        label_y = self.s(16)
        body_y = label_y + m.line("label") + self.s(8)
        lh = m.line("small")
        lines_text: list[tuple[str, bool]] = []
        for item in shown:
            for k, text in enumerate(m.wrap("small", "• " + item, inner_w)):
                lines_text.append((text, k == 0))
        if truncated:
            lines_text.append((f"• 另有 {truncated} 条提示见 TXT 报告", True))
        h = body_y + len(lines_text) * lh + self.s(14)
        shapes = [self._card(self.pad, self.width - self.pad, h, p.surface_alt, p.border)]
        lines: list[_Line] = [_Line("提示与警告", "label", p.text_dim, x0, label_y)]
        hang = m.width("small", "• ")  # 续行悬挂缩进，与首行文字左对齐
        for i, (text, first) in enumerate(lines_text):
            lines.append(
                _Line(
                    text,
                    "small",
                    p.text_dim,
                    x0 + (0 if first else hang),
                    body_y + i * lh,
                )
            )
        return _Block(
            kind="notes",
            height=h + self.s(18),
            tail=self.s(18),
            shapes=tuple(shapes),
            lines=tuple(lines),
        )

    def build(self) -> list[_Block]:
        """按阅读顺序排出整份报告的所有块。"""
        blocks = [
            self.header(),
            self.verdict(),
            self.trio(),
            self.spectrum(),
            self.matrix(),
            self.info(),
            self.summary(),
            self.scenarios(),
            *self.steps(),
            self.samples(),
            self.gateway(),
            self.servers(),
            self.notes(),
        ]
        return [b for b in blocks if b.height > 0]


# --------------------------------------------------------------------------
# JPG — 分页与绘制
# --------------------------------------------------------------------------


def _footer_height(scale: float) -> int:
    return int(round(60 * scale))


def _mini_header_height(scale: float) -> int:
    return int(round(72 * scale))


def _plan(ctx: ReportContext, palette: Palette, metrics: _Metrics, width: int) -> list[_Block]:
    """第一遍：只算高度，不建画布。"""
    return _Planner(ctx, palette, metrics, width).build()


def _pack(
    blocks: list[_Block], capacity: int, footer_h: int, mini_h: int
) -> list[list[_Block]]:
    """贪心装页：每页内容高度不超过 ``capacity``（块是不可分割的单位）。"""
    pages: list[list[_Block]] = []
    cur: list[_Block] = []
    used = 0
    for block in blocks:
        avail = capacity - footer_h - (mini_h if pages else 0)
        if cur and used + block.height > avail:
            pages.append(cur)
            cur = [block]
            used = block.height
        else:
            cur.append(block)
            used += block.height
    if cur:
        pages.append(cur)
    return pages


def _paginate(blocks: list[_Block], scale: float) -> tuple[PagePlan, ...]:
    """按块切页；单页内容高度上限 ``MAX_PAGE_H``。

    先按上限粗排得到页数 N，再按「总高 / N」重排一次，避免最后一页只剩一张
    小卡片（丑且浪费）。块不可再分，所以极端情况下仍可能有一块独占一页。
    """
    limit = int(round(MAX_PAGE_H * scale))
    footer_h = _footer_height(scale)
    mini_h = _mini_header_height(scale)

    groups = _pack(blocks, limit, footer_h, mini_h)
    if len(groups) > 1:
        # 在「不增加页数」的前提下找最小的容量，让各页高度尽量接近
        total = sum(b.height for b in blocks)
        chrome = len(groups) * (footer_h + mini_h)
        target = max(-(-(total + chrome) // len(groups)), max(b.height for b in blocks))
        lo, hi, best = target, limit, groups
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = _pack(blocks, mid, footer_h, mini_h)
            if len(candidate) <= len(groups):
                best = candidate
                hi = mid - 1
            else:
                lo = mid + 1
        groups = best

    plans: list[PagePlan] = []
    for i, group in enumerate(groups):
        content = sum(b.height for b in group) - group[-1].tail
        plans.append(
            PagePlan(blocks=tuple(group), height=content + footer_h + (mini_h if i else 0))
        )
    return tuple(plans)


def plan_report(
    ctx: ReportContext,
    palette: Palette,
    width: int = IMG_W,
    supersample: int = 1,
) -> ReportPlan:
    """只做排版规划（供测试与需要预先知道尺寸的调用方使用）。"""
    factor = max(1, int(supersample))
    w = int(width) * factor
    scale = w / IMG_W
    metrics = _Metrics(scale)
    blocks = _plan(ctx, palette, metrics, w)
    return ReportPlan(width=w, scale=scale, pages=_paginate(blocks, scale))


def _draw_text(draw, xy, text: str, font, fill: str, anchor: str) -> None:
    try:
        draw.text(xy, text, font=font, fill=fill, anchor=anchor)
    except (ValueError, TypeError):  # 位图兜底字体不支持 anchor
        draw.text(xy, text, font=font, fill=fill)


def _draw_shape(draw, shape: _Shape, oy: int) -> None:
    x0, y0, x1, y1 = shape.box
    fill = shape.fill or None
    outline = shape.outline or None
    try:
        if shape.kind == "rect":
            draw.rectangle([x0, y0 + oy, x1, y1 + oy], fill=fill, outline=outline,
                           width=shape.width)
        elif shape.kind == "rrect":
            h = max(1, y1 - y0)
            w = max(1, x1 - x0)
            radius = max(0, min(shape.radius, h // 2, w // 2))
            draw.rounded_rectangle([x0, y0 + oy, x1, y1 + oy], radius=radius,
                                   fill=fill, outline=outline, width=shape.width)
        elif shape.kind == "line":
            draw.line([x0, y0 + oy, x1, y1 + oy], fill=fill, width=shape.width)
        elif shape.kind == "ellipse":
            draw.ellipse([x0, y0 + oy, x1, y1 + oy], fill=fill, outline=outline,
                         width=shape.width)
        elif shape.kind == "poly":
            draw.polygon([(px, py + oy) for px, py in shape.points], fill=fill, outline=outline)
    except (ValueError, TypeError):
        # 退化尺寸（宽或高为 0）在 Pillow 里会抛错，跳过即可，不影响版式
        return


def _paint_page(
    page: PagePlan,
    ctx: ReportContext,
    palette: Palette,
    metrics: _Metrics,
    index: int,
    total: int,
) -> "Image":
    """第二遍：按规划高度建图并逐块绘制，最后断言高度一致。"""
    from PIL import Image, ImageDraw

    p = palette
    scale = metrics.scale
    width = int(round(IMG_W * scale))
    img = Image.new("RGB", (width, page.height), p.bg)
    draw = ImageDraw.Draw(img)

    cursor = 0
    if index > 0:
        mini_h = _mini_header_height(scale)
        draw.rectangle([0, 0, width, mini_h - 1], fill=p.header)
        _draw_text(
            draw,
            (int(round(PAD * scale)), mini_h // 2),
            "NatScope — NAT 类型检测报告（续）",
            metrics.font("brand_sub"),
            p.header_text,
            "lm",
        )
        _draw_text(
            draw,
            (width - int(round(PAD * scale)), mini_h // 2),
            f"第 {index + 1}/{total} 页",
            metrics.font("small"),
            p.header_text,
            "rm",
        )
        cursor = mini_h

    # 先画所有形状（卡片底、强调条、色块），再画所有文字，图层天然正确
    for block in page.blocks:
        for shape in block.shapes:
            _draw_shape(draw, shape, cursor)
        cursor += block.height
    cursor = mini_h if index > 0 else 0
    for block in page.blocks:
        for line in block.lines:
            _draw_text(
                draw,
                (line.x, cursor + line.dy),
                line.text,
                metrics.font(line.role),
                line.color,
                line.anchor,
            )
        cursor += block.height

    # ---- 页脚 ----
    footer_h = _footer_height(scale)
    pad = int(round(PAD * scale))
    top = page.height - footer_h + int(round(6 * scale))
    draw.line([pad, top, width - pad, top], fill=p.border, width=max(1, int(round(scale))))
    text_y = page.height - footer_h // 2 - int(round(4 * scale))
    _draw_text(
        draw,
        (pad, text_y),
        f"由 NatScope {ctx.version} 生成 · {ctx.os_text}",
        metrics.font("small"),
        p.text_faint,
        "lm",
    )
    right = f"第 {index + 1}/{total} 页 · STUN over UDP" if total > 1 else "STUN over UDP"
    _draw_text(
        draw,
        (width - pad, text_y),
        right,
        metrics.font("small"),
        p.text_faint,
        "rm",
    )

    assert img.height == page.height, (
        f"画布高度 {img.height} 与规划 {page.height} 不一致"
    )
    return img


def render_report_pages(
    ctx: ReportContext,
    palette: Palette,
    width: int = IMG_W,
    supersample: int = 1,
) -> list["Image"]:
    """渲染全部页面，返回 PIL.Image 列表（通常只有一页）。"""
    plan = plan_report(ctx, palette, width=width, supersample=supersample)
    metrics = _Metrics(plan.scale)
    total = len(plan.pages)
    return [
        _paint_page(page, ctx, palette, metrics, i, total)
        for i, page in enumerate(plan.pages)
    ]


def render_report_image(
    ctx: ReportContext,
    palette: Palette,
    width: int = IMG_W,
    supersample: int = 1,
) -> "Image":
    """渲染报告首页（兼容 v1.0 的单图接口）。"""
    return render_report_pages(ctx, palette, width=width, supersample=supersample)[0]


def measure_report(
    ctx: ReportContext,
    palette: Palette,
    width: int = IMG_W,
    supersample: int = 1,
) -> tuple[int, ...]:
    """返回每页的期望高度，与 :func:`render_report_pages` 的输出一一对应。"""
    return plan_report(ctx, palette, width=width, supersample=supersample).heights


def _page_path(path: str, index: int, total: int) -> str:
    """多页时把 ``报告.jpg`` 变成 ``报告_1.jpg`` / ``报告_2.jpg``。"""
    if total <= 1:
        return path
    root, ext = os.path.splitext(path)
    return f"{root}_{index}{ext}"


def save_image(
    ctx: ReportContext,
    path: str,
    palette: Palette,
    quality: int = 92,
    width: int = IMG_W,
    supersample: int = 1,
) -> list[str]:
    """导出 JPG，返回实际写出的文件路径列表（通常只有一个）。

    高清导出有两条等价路径：``width=1920`` 或 ``width=960, supersample=2``
    —— 都是「按 1920 排版、字号同比放大 2 倍」，而不是先画小图再放大
    （缩放会把 4K 屏上的小字糊成一团）。``supersample`` 只是
    ``width * N`` 的语法糖，默认 1x。宽度 ≥1600 时建议开启，否则 4K 屏
    全屏查看会发虚。

    单页超过 :data:`MAX_PAGE_H` 时自动分页，文件名为 ``<原名>_1.jpg`` /
    ``<原名>_2.jpg``；只有一页时就用传入的 ``path``。
    """
    images = render_report_pages(ctx, palette, width=width, supersample=supersample)
    total = len(images)
    written: list[str] = []
    for i, img in enumerate(images, start=1):
        target = _page_path(path, i, total)
        img.save(target, "JPEG", quality=quality, optimize=True, progressive=True)
        written.append(target)
    return written


def default_filename(prefix: str, ext: str) -> str:
    return f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}.{ext}"
