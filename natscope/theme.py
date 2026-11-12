# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""配色与字体令牌：浅色 / 深色，默认跟随系统。

三层令牌：
  * :class:`Palette` —— 颜色，全部为 ``#RRGGBB`` 字符串，GUI 与报告共用；
  * :class:`Typography` —— 字号点值表，避免 8/9/10/11/12/16/22 这些魔数散落各处；
  * :func:`type_color` —— NAT 类型到语义色的唯一映射（报告与 GUI 都应走它）。

对比度约束（WCAG 2.1）：正文与小字对背景至少 4.5:1；1px 边框对背景至少
1.3:1，否则高分屏上卡片边框会"消失"。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    """一套完整配色。

    前 15 个字段是 v1.0 就有的，构造时全部用关键字参数；v1.1 新增的
    ``shadow`` / ``overlay`` / ``grid`` / ``radius`` 都带默认值，老代码
    不传也不会炸。
    """

    name: str
    bg: str
    surface: str
    surface_alt: str
    border: str
    text: str
    text_dim: str
    text_faint: str
    accent: str
    accent_text: str
    ok: str
    warn: str
    err: str
    header: str
    header_text: str
    track: str
    # ---- v1.1 新增 ----
    shadow: str = "#DCE1E8"
    overlay: str = "#E7EAEF"
    grid: str = "#EDF0F4"
    radius: int = 8


LIGHT = Palette(
    name="light",
    bg="#F4F5F7",
    surface="#FFFFFF",
    surface_alt="#FAFBFC",
    # 1.27:1（对 bg）/ 1.39:1（对 surface）：1px 卡片边框在高分屏上真的看得见了
    border="#D6DBE3",
    text="#15181D",
    text_dim="#5C636E",
    # 4.85:1（对 bg）/ 5.29:1（对 surface）：达到 WCAG AA 正文标准
    text_faint="#646C7A",
    accent="#2563EB",
    accent_text="#FFFFFF",
    ok="#15803D",
    warn="#B45309",
    err="#B91C1C",
    header="#1E3A5F",
    header_text="#FFFFFF",
    track="#E8EBF0",
    shadow="#DCE1E8",
    overlay="#E7EAEF",
    grid="#EDF0F4",
    radius=8,
)

DARK = Palette(
    name="dark",
    bg="#14161A",
    surface="#1D2026",
    surface_alt="#23272E",
    # 1.73:1（对 bg）/ 1.56:1（对 surface）
    border="#3A4049",
    text="#E9ECF1",
    text_dim="#A2A9B4",
    # 5.27:1（对 surface）/ 5.85:1（对 bg）
    text_faint="#8B93A1",
    accent="#5B9CF8",
    accent_text="#0B1220",
    ok="#4ADE80",
    warn="#FBBF24",
    err="#F87171",
    header="#0F1A2B",
    header_text="#E9ECF1",
    track="#2A2F37",
    shadow="#0C0E11",
    overlay="#0E1013",
    grid="#262B32",
    radius=8,
)

PALETTES = {"light": LIGHT, "dark": DARK}


class Typography:
    """字号点值表（tkinter 与 Pillow 都按"点"理解正数）。

    集中在这里之后，GUI 控件与导出的报告图用同一套阶梯，改一处即全局生效。
    """

    #: 结论大字（"完全锥型"那一行）
    display = 22
    #: 卡片标题、页眉品牌名
    title = 16
    #: 分区小标题（弹窗标题）
    heading = 12
    #: 数值块（映射/过滤行为的具体取值）
    value = 11
    #: 正文、按钮
    body = 10
    #: 等宽日志与 IP:Port 这类需要对齐的文本
    mono = 9
    #: 次级说明、状态栏
    note = 9
    #: 字段标签、脚注
    label = 8

    #: 字号阶梯的完整映射，方便遍历/断言
    SCALE = {
        "display": display,
        "title": title,
        "heading": heading,
        "value": value,
        "body": body,
        "mono": mono,
        "note": note,
        "label": label,
    }


def system_theme() -> str:
    """读注册表判断系统应用主题，非 Windows 或读取失败时返回 light。"""
    if sys.platform != "win32":
        return "light"
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        try:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        finally:
            winreg.CloseKey(key)
        return "light" if value else "dark"
    except OSError:
        return "light"


def get_palette(name: str = "auto") -> Palette:
    if name == "auto":
        name = system_theme()
    return PALETTES.get(name, LIGHT)


def type_color(palette: Palette, classic_type: str) -> str:
    """按 NAT 类型给出语义色。

    这是全项目**唯一**的映射实现：``report.py`` 与 ``gui/app.py`` 里的私有
    副本应逐步改为调用本函数（v1.1 先保留私有副本，避免影响既有渲染结果）。
    """
    if classic_type in ("open", "full_cone"):
        return palette.ok
    if classic_type in ("restricted", "port_restricted", "cone_unknown"):
        return palette.warn
    if classic_type in ("symmetric", "blocked"):
        return palette.err
    return palette.text_dim
