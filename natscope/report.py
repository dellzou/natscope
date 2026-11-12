# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""检测报告导出：纯文本 TXT 与卡片式 JPG 图片。"""

from __future__ import annotations

import os
import platform
import textwrap
import time
from dataclasses import dataclass, field

from . import __version__
from .nat import NatResult
from .gateway import GatewayReport
from .servers import TIER_LABEL, TIER_PREFERRED, ProbeSummary
from .theme import Palette

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

    add("【三、结论说明】")
    add(_THIN)
    for line in _wrap(nat.summary, 62, "  "):
        add(line)
    add("")

    add("【四、测试明细】")
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

    add("【五、网关端口映射能力】")
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
        add("【六、STUN 服务器统计】")
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

    notes = list(nat.warnings)
    if ctx.gateway:
        notes.extend(ctx.gateway.notes)
    if notes:
        add("【七、提示与警告】")
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


def _wrap(text: str, width: int, indent: str = "") -> list[str]:
    """按显示宽度换行（中文按 2 列计）。"""
    lines: list[str] = []
    cur = ""
    cur_w = 0
    for ch in text:
        if ch == "\n":
            lines.append(indent + cur)
            cur, cur_w = "", 0
            continue
        w = 2 if ord(ch) > 0x2E80 else 1
        if cur_w + w > width:
            lines.append(indent + cur)
            cur, cur_w = "", 0
        cur += ch
        cur_w += w
    if cur:
        lines.append(indent + cur)
    return lines or [indent]


def save_text(ctx: ReportContext, path: str) -> str:
    content = build_report_text(ctx)
    with open(path, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(content)
    return path


# --------------------------------------------------------------------------
# JPG
# --------------------------------------------------------------------------

IMG_W = 960
PAD = 40


def _load_font(size: int, bold: bool = False):
    from PIL import ImageFont

    for path in FONT_CANDIDATES_BOLD if bold else FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _text_w(draw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _wrap_px(draw, text: str, font, max_w: int) -> list[str]:
    lines, cur = [], ""
    for ch in text:
        if _text_w(draw, cur + ch, font) > max_w:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def render_report_image(ctx: ReportContext, palette: Palette):
    """绘制报告图片，返回 PIL.Image。"""
    from PIL import Image, ImageDraw

    nat = ctx.nat
    p = palette
    draw_probe = Image.new("RGB", (10, 10))
    probe_draw = ImageDraw.Draw(draw_probe)

    f_brand = _load_font(26, True)
    f_brand_sub = _load_font(13)
    f_title = _load_font(38, True)
    f_title_en = _load_font(15)
    f_label = _load_font(12)
    f_value = _load_font(17, True)
    f_body = _load_font(14)
    f_small = _load_font(12)
    f_mono = _load_font(13)

    # 先试算高度，再正式绘制（简单做法：一次性画到大图，最后裁剪）
    H = 1500
    img = Image.new("RGB", (IMG_W, H), p.bg)
    d = ImageDraw.Draw(img)

    y = 0
    # ---- 头部 ----
    d.rectangle([0, 0, IMG_W, 96], fill=p.header)
    d.text((PAD, 26), "NatScope", font=f_brand, fill=p.header_text)
    d.text((PAD + _text_w(d, "NatScope", f_brand) + 16, 38),
           "NAT 类型检测报告", font=f_brand_sub, fill=p.header_text)
    stamp = ctx.generated_at
    d.text((IMG_W - PAD - _text_w(d, stamp, f_small), 40), stamp, font=f_small, fill=p.header_text)
    y = 96 + 24

    # ---- 结论卡 ----
    card_h = 158
    d.rounded_rectangle([PAD, y, IMG_W - PAD, y + card_h], 16, fill=p.surface, outline=p.border)
    accent = _type_color(p, nat.classic_type)
    d.rounded_rectangle([PAD, y, PAD + 6, y + card_h], 3, fill=accent)
    d.text((PAD + 26, y + 20), "检测结论", font=f_label, fill=p.text_dim)
    d.text((PAD + 26, y + 44), nat.classic_label, font=f_title, fill=accent)
    if nat.classic_en:
        d.text((PAD + 26, y + 96), nat.classic_en, font=f_title_en, fill=p.text_dim)
    # 右侧映射/过滤缩写
    right = f"{nat.mapping_behavior.upper()} / {nat.filtering_behavior.upper()}"
    d.text((IMG_W - PAD - 26 - _text_w(d, right, f_body), y + 96), right,
           font=f_body, fill=p.text_faint)
    y += card_h + 18

    # ---- 三列小卡 ----
    gap = 18
    cw = (IMG_W - PAD * 2 - gap * 2) // 3
    items = [
        ("映射行为 Mapping", _short_mapping(nat.mapping_behavior), p.accent),
        ("过滤行为 Filtering", _short_filtering(nat.filtering_behavior), p.accent),
        ("端口映射 Gateway", _gateway_short(ctx.gateway), p.ok if ctx.gateway and ctx.gateway.any_available else p.text_dim),
    ]
    for i, (label, value, color) in enumerate(items):
        x0 = PAD + i * (cw + gap)
        d.rounded_rectangle([x0, y, x0 + cw, y + 92], 14, fill=p.surface, outline=p.border)
        d.text((x0 + 18, y + 16), label, font=f_label, fill=p.text_dim)
        d.text((x0 + 18, y + 42), value, font=f_value, fill=color)
    y += 92 + 24

    # ---- 网络信息 ----
    info_h = 128
    d.rounded_rectangle([PAD, y, IMG_W - PAD, y + info_h], 14, fill=p.surface, outline=p.border)
    d.text((PAD + 20, y + 14), "网络信息", font=f_label, fill=p.text_dim)
    rows = [
        ("本机地址", f"{nat.local_ip}:{nat.local_port}"),
        ("公网映射", f"{nat.public_ip}:{nat.public_port}"),
        ("默认网关", (ctx.gateway.gateway_ip if ctx.gateway else "") or "—"),
        ("主 STUN", nat.primary_server or "—"),
    ]
    col_w = (IMG_W - PAD * 2 - 40) // 2
    for i, (k, v) in enumerate(rows):
        cx = PAD + 20 + (i % 2) * col_w
        cy = y + 42 + (i // 2) * 40
        d.text((cx, cy), k, font=f_small, fill=p.text_faint)
        d.text((cx + 76, cy - 2), v, font=f_mono, fill=p.text)
    y += info_h + 20

    # ---- 结论说明 ----
    body_lines = _wrap_px(probe_draw, nat.summary, f_body, IMG_W - PAD * 2 - 44)
    desc_h = 44 + len(body_lines) * 24
    d.rounded_rectangle([PAD, y, IMG_W - PAD, y + desc_h], 14, fill=p.surface_alt, outline=p.border)
    d.text((PAD + 20, y + 14), "结论说明", font=f_label, fill=p.text_dim)
    for i, line in enumerate(body_lines):
        d.text((PAD + 22, y + 38 + i * 24), line, font=f_body, fill=p.text)
    y += desc_h + 20

    # ---- 测试明细 ----
    step_h = 34 + len(nat.steps) * 26
    d.rounded_rectangle([PAD, y, IMG_W - PAD, y + step_h], 14, fill=p.surface, outline=p.border)
    d.text((PAD + 20, y + 14), "测试明细", font=f_label, fill=p.text_dim)
    ly = y + 40
    for step in nat.steps:
        color = {"ok": p.ok, "fail": p.err, "skip": p.text_faint}.get(step.status, p.text_dim)
        d.text((PAD + 22, ly), step.icon, font=f_body, fill=color)
        name = step.name if len(step.name) <= 26 else step.name[:25] + "…"
        d.text((PAD + 44, ly), name, font=f_body, fill=p.text)
        if step.detail:
            det = step.detail if len(step.detail) <= 40 else step.detail[:39] + "…"
            d.text((PAD + 300, ly), det, font=f_small, fill=p.text_dim)
        ly += 26
    y += step_h + 20

    # ---- 服务器统计 ----
    if ctx.probe:
        pr = ctx.probe
        sv_h = 118
        d.rounded_rectangle([PAD, y, IMG_W - PAD, y + sv_h], 14, fill=p.surface, outline=p.border)
        d.text((PAD + 20, y + 14), "STUN 服务器", font=f_label, fill=p.text_dim)
        stats = [
            ("总计", str(len(pr.results)), p.text),
            ("优选", str(len(pr.preferred)), p.ok),
            ("备选", str(len(pr.backup)), p.warn),
            ("不可用", str(len(pr.unavailable)), p.err),
            ("支持 RFC5780", str(len(pr.rfc5780_servers)), p.accent),
        ]
        sx = PAD + 22
        for label, val, color in stats:
            d.text((sx, y + 44), val, font=_load_font(22, True), fill=color)
            d.text((sx, y + 74), label, font=f_small, fill=p.text_faint)
            sx += 150
        y += sv_h + 20

    # ---- 警告 ----
    notes = list(nat.warnings)
    if notes:
        lines: list[str] = []
        for n in notes[:4]:
            lines.extend(_wrap_px(probe_draw, "• " + n, f_small, IMG_W - PAD * 2 - 44))
        note_h = 34 + len(lines) * 20
        d.rounded_rectangle([PAD, y, IMG_W - PAD, y + note_h], 14, fill=p.surface_alt, outline=p.border)
        d.text((PAD + 20, y + 12), "提示", font=f_label, fill=p.text_dim)
        for i, line in enumerate(lines):
            d.text((PAD + 22, y + 34 + i * 20), line, font=f_small, fill=p.text_dim)
        y += note_h + 18

    # ---- 页脚 ----
    y += 6
    footer = f"由 NatScope {ctx.version} 生成 · {ctx.os_text}"
    d.line([PAD, y, IMG_W - PAD, y], fill=p.border)
    d.text((PAD, y + 12), footer, font=f_small, fill=p.text_faint)
    d.text((IMG_W - PAD - _text_w(d, "STUN over UDP", f_small), y + 12),
           "STUN over UDP", font=f_small, fill=p.text_faint)
    y += 46

    return img.crop((0, 0, IMG_W, min(y, H)))


def _type_color(p: Palette, classic_type: str) -> str:
    if classic_type in ("open", "full_cone"):
        return p.ok
    if classic_type in ("restricted", "port_restricted", "cone_unknown"):
        return p.warn
    if classic_type in ("symmetric", "blocked"):
        return p.err
    return p.text_dim


def _short_mapping(code: str) -> str:
    return {
        "eim": "端点无关 EIM",
        "adm": "地址相关 ADM",
        "apdm": "地址+端口 APDM",
    }.get(code, "不可测")


def _short_filtering(code: str) -> str:
    return {
        "eif": "端点无关 EIF",
        "adf": "地址相关 ADF",
        "apdf": "地址+端口 APDF",
    }.get(code, "不可测")


def _gateway_short(gw: GatewayReport | None) -> str:
    if not gw:
        return "未检测"
    names = [c.name.replace("UPnP-IGD", "UPnP").replace("NAT-PMP", "PMP") for c in gw.capabilities if c.available]
    if names:
        return " / ".join(names)
    if any(c.state == "limited" for c in gw.capabilities):
        return "存在但受限"
    return "未发现"


def save_image(ctx: ReportContext, path: str, palette: Palette, quality: int = 92) -> str:
    img = render_report_image(ctx, palette)
    img.save(path, "JPEG", quality=quality, optimize=True, progressive=True)
    return path


def default_filename(prefix: str, ext: str) -> str:
    return f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}.{ext}"
