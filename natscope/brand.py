# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""NatScope 品牌资产：产品名称、标语、品牌色与图标几何。

本模块只依赖标准库，**导入期绝不加载 tkinter 与 Pillow**：
CLI、单元测试、打包脚本都能以近乎零成本拿到品牌常量与几何参数。
真正需要像素时才调用 :func:`render_mark`，其中惰性导入 Pillow。

几何参数统一用 0..1 归一化坐标表示，Canvas（GUI 绘制）与 Pillow（图标
生成）共享同一份 :data:`MARK_GEOM`，避免两处各画一套导致造型漂移。
"""

from __future__ import annotations

import math

# ---- 文本标识 -------------------------------------------------------------

NAME = "NatScope"
TAGLINE = "NAT 类型检测"
SUBTITLE = "RFC 3489 / RFC 5780"

COPYRIGHT_HOLDER = "ZouNing"
COPYRIGHT_YEAR = 2026
PROJECT_URL = "https://github.com/dellzou"
LICENSE_ID = "PolyForm-Noncommercial-1.0.0"
REQUIRED_NOTICE = (
    f"Required Notice: Copyright {COPYRIGHT_YEAR} {COPYRIGHT_HOLDER} ({PROJECT_URL})"
)
#: Windows AppUserModelID，任务栏分组与通知归属都靠它（T05 打包时使用）。
APP_USER_MODEL_ID = "ZouNing.NatScope"

# ---- 品牌色 ---------------------------------------------------------------

#: 图标底色（渐变起点，左上偏亮）
BRAND_DARK = (30, 58, 95)
#: 图标底色（渐变终点，下方更深，避免纯色平涂的死板）
BRAND_DARK_2 = (16, 32, 54)
#: 强调色：公网端点 / 打洞成功
BRAND_ACCENT = (74, 222, 128)
#: 内网侧线框色
BRAND_WHITE = (255, 255, 255)
#: 强调色的深色版本，留给浅色界面上的色块与文字
BRAND_ACCENT_DEEP = (21, 128, 61)

# ---- 图标几何（全部归一化到 0..1） ----------------------------------------
#
# 造型语义：一个"内网边界"圆环，右上 45° 处开一道 15° 缺口（打出来的洞），
# 一根与缺口两端精确对齐的 45° 连杆穿过缺口伸向环外的绿色端点（公网映射）。
# 三段结构：环（内网）→ 连杆（穿透）→ 绿点（公网）。
#
# 关键约束：
#   1. 连杆底边贴着圆环外缘的弧线走，并与缺口两端端点相接，断裂在几何上
#      不可能出现（早期版本用两点直连的弦做底边，接缝处有一道发丝黑缝）；
#   2. 连杆轴线、缺口中心、绿点圆心三者共线于 45°，读作一次"直线穿透"；
#   3. 绿点与圆环之间始终留出 ring_clearance 的空白，绿点不啃噬环体。
MARK_GEOM: dict[str, float] = {
    # 圆角方底：圆角半径 = 边长 * 12%
    "corner_radius": 0.12,
    # 圆环圆心
    "ring_cx": 0.45,
    "ring_cy": 0.55,
    # 圆环外半径（full / simple 两档）
    "ring_outer_r": 0.265,
    "ring_outer_r_simple": 0.270,
    # 环宽（full 6%，simple 加粗到 10%：16px 下 0.09 只有 1.4px，笔画会发灰）
    "ring_width_full": 0.060,
    "ring_width_simple": 0.100,
    # 缺口角度
    "gap_deg": 15.0,
    # 绿点方位角与到圆心的距离（沿 45° 轴）。距离同时受两条约束夹逼：
    #   - 下界：连杆可见长度要够（32px 下至少 4px），否则读作缺口毛刺；
    #   - 上界：绿点包围盒不得侵入 12% 圆角切角区（w <= 0.74 时刚好安全）
    "ball_angle_deg": -45.0,
    "ball_dist": 0.520,
    "ball_dist_simple": 0.475,
    # 绿点半径，simple 档放大 10%
    "ball_r": 0.105,
    "ball_r_simple_scale": 1.10,
    # 连杆底边向环内咬入的深度：用弧线做底边保证与圆环外缘严丝合缝，
    # 再往内咬 0.4% 边长，让两者的抗锯齿边缘重叠，彻底消除发丝缝隙
    "rod_bite": 0.004,
    # 连杆末端深入绿点内部的比例（相对绿点半径），保证接缝不露
    "rod_tuck": 0.60,
    # 绿点内缘与圆环外缘之间的最小空白
    "ring_clearance": 0.020,
}

#: 连杆底边用几段折线逼近圆环外缘（8 段时 15° 弧的弦高误差 < 0.1px）
ROD_ARC_STEPS = 8

#: 小尺寸阈值：低于该边长改用 simple 造型（两段结构，无连杆）
SIMPLE_BELOW = 32


def hex_color(rgb: tuple[int, int, int]) -> str:
    """把 RGB 三元组转成 ``#RRGGBB``，供 tkinter / Canvas 使用。"""
    r, g, b = (max(0, min(255, int(v))) for v in rgb[:3])
    return f"#{r:02X}{g:02X}{b:02X}"


def pick_variant(size: int) -> str:
    """按目标像素尺寸选择造型：<32px 用 simple，否则用 full。"""
    return "simple" if int(size) < SIMPLE_BELOW else "full"


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    """线性插值两个颜色，``t`` 会被夹到 0..1。"""
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return (
        int(round(a[0] + (b[0] - a[0]) * t)),
        int(round(a[1] + (b[1] - a[1]) * t)),
        int(round(a[2] + (b[2] - a[2]) * t)),
    )


def layout(variant: str = "full") -> dict[str, object]:
    """按造型展开几何参数，并整体平移让图形包围盒正好居中。

    返回值中的长度仍是 0..1 归一化值，调用方乘以边长即可得到像素坐标。
    除图形参数外还返回圆环圆弧的起止角度（PIL 角度制：0° 在 3 点钟方向、
    顺时针为正，与本模块的 y 轴向下的坐标约定一致）。
    """
    g = MARK_GEOM
    simple = variant == "simple"

    r_out = float(g["ring_outer_r_simple"] if simple else g["ring_outer_r"])
    band = float(g["ring_width_simple"] if simple else g["ring_width_full"])
    scale = float(g["ball_r_simple_scale"]) if simple else 1.0
    ball_r = float(g["ball_r"]) * scale
    dist = float(g["ball_dist_simple"] if simple else g["ball_dist"])
    cx, cy = float(g["ring_cx"]), float(g["ring_cy"])

    ang_deg = float(g["ball_angle_deg"])
    ang = math.radians(ang_deg)
    half = math.radians(float(g["gap_deg"]) / 2.0)
    ux, uy = math.cos(ang), math.sin(ang)

    # 缺口两端的端点（位于圆环外半径上）由圆弧采样隐式给出
    ball_c = (cx + dist * ux, cy + dist * uy)

    rod: tuple[tuple[float, float], ...] = ()
    if not simple:
        # 连杆底边沿圆环外缘取一段圆弧（而不是两点直连的弦）：圆环的缺口
        # 端面是径向直切，而弦会从圆环外缘"抄近路"，两者之间会留一道发丝
        # 缝隙。底边跟着弧走，再加 rod_bite 的咬入量，接缝就彻底消失。
        r_base = r_out - float(g["rod_bite"])
        base: list[tuple[float, float]] = []
        for i in range(ROD_ARC_STEPS + 1):
            phi = (ang - half) + (2.0 * half) * i / ROD_ARC_STEPS
            base.append((cx + r_base * math.cos(phi), cy + r_base * math.sin(phi)))
        # 连杆长度：从缺口外缘中点出发，一直伸到绿点内部
        length = (dist - r_base * math.cos(half)) + ball_r * float(g["rod_tuck"])
        tip = [(p[0] + length * ux, p[1] + length * uy) for p in base]
        rod = tuple(base + tip[::-1])

    probes = [
        (cx - r_out, cy - r_out),
        (cx + r_out, cy + r_out),
        (ball_c[0] - ball_r, ball_c[1] - ball_r),
        (ball_c[0] + ball_r, ball_c[1] + ball_r),
        *rod,
    ]
    dx = 0.5 - (min(p[0] for p in probes) + max(p[0] for p in probes)) / 2.0
    dy = 0.5 - (min(p[1] for p in probes) + max(p[1] for p in probes)) / 2.0

    def shift(p: tuple[float, float]) -> tuple[float, float]:
        return (p[0] + dx, p[1] + dy)

    return {
        "variant": variant,
        "corner_radius": float(g["corner_radius"]),
        "ring_center": shift((cx, cy)),
        "ring_outer_r": r_out,
        "ring_inner_r": r_out - band,
        "ring_width": band,
        "gap_deg": float(g["gap_deg"]),
        "arc_start_deg": ang_deg + float(g["gap_deg"]) / 2.0,
        "arc_end_deg": ang_deg - float(g["gap_deg"]) / 2.0 + 360.0,
        "ball_center": shift(ball_c),
        "ball_r": ball_r,
        "rod": tuple(shift(p) for p in rod),
    }


def _paint_tile(img: "object", size: int, corner_ratio: float) -> "object":
    """在给定 RGBA 图上画深色圆角方底（带垂直渐变），返回圆角蒙版。"""
    from PIL import Image, ImageDraw

    grad = Image.new("RGBA", (size, size), BRAND_DARK + (255,))
    gd = ImageDraw.Draw(grad)
    span = max(size - 1, 1)
    for y in range(size):
        gd.line([(0, y), (size, y)], fill=_mix(BRAND_DARK, BRAND_DARK_2, y / span) + (255,))

    mask = Image.new("L", (size, size), 0)
    radius = max(1, int(round(corner_ratio * size)))
    radius = min(radius, size // 2)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    img.paste(grad, (0, 0), mask)
    return mask


def render_mark(size: int, variant: str = "full") -> "object":
    """把品牌图形渲染成 ``size × size`` 的 RGBA 图像（Pillow Image）。

    这里严格按目标尺寸绘制，不做内部超采样——超采样由调用方
    （``tools/make_icons.py``）在 4× 尺寸上渲染再 LANCZOS 缩小完成，
    这样每一档尺寸都能拿到干净的亚像素笔画。
    """
    from PIL import Image, ImageChops, ImageDraw

    size = max(8, int(size))
    lay = layout(variant)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    tile_mask = _paint_tile(img, size, float(lay["corner_radius"]))

    draw = ImageDraw.Draw(img)
    ccx, ccy = lay["ring_center"]  # type: ignore[misc]
    r_out = float(lay["ring_outer_r"])  # type: ignore[arg-type]
    band = float(lay["ring_width"])  # type: ignore[arg-type]
    cx, cy, rr = ccx * size, ccy * size, r_out * size
    width = max(1, int(round(band * size)))
    draw.arc(
        [cx - rr, cy - rr, cx + rr, cy + rr],
        start=float(lay["arc_start_deg"]),  # type: ignore[arg-type]
        end=float(lay["arc_end_deg"]),  # type: ignore[arg-type]
        fill=BRAND_WHITE + (255,),
        width=width,
    )

    rod = lay["rod"]  # type: ignore[assignment]
    if rod:
        draw.polygon([(p[0] * size, p[1] * size) for p in rod], fill=BRAND_WHITE + (255,))

    bcx, bcy = lay["ball_center"]  # type: ignore[misc]
    br = float(lay["ball_r"]) * size  # type: ignore[arg-type]
    draw.ellipse(
        [bcx * size - br, bcy * size - br, bcx * size + br, bcy * size + br],
        fill=BRAND_ACCENT + (255,),
    )

    # 最后用圆角蒙版裁一次：环/连杆/绿点都是画在方底之上的，将来几何微调
    # 时若越过圆角切角区就会被这里切掉，不会漏到透明角上
    img.putalpha(ImageChops.multiply(img.getchannel("A"), tile_mask))
    return img


__all__ = [
    "APP_USER_MODEL_ID",
    "BRAND_ACCENT",
    "BRAND_ACCENT_DEEP",
    "BRAND_DARK",
    "BRAND_DARK_2",
    "BRAND_WHITE",
    "COPYRIGHT_HOLDER",
    "COPYRIGHT_YEAR",
    "LICENSE_ID",
    "MARK_GEOM",
    "NAME",
    "PROJECT_URL",
    "REQUIRED_NOTICE",
    "SIMPLE_BELOW",
    "SUBTITLE",
    "TAGLINE",
    "hex_color",
    "layout",
    "pick_variant",
    "render_mark",
]
