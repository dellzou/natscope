"""配色方案：浅色 / 深色，默认跟随系统。"""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
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


LIGHT = Palette(
    name="light",
    bg="#F4F5F7",
    surface="#FFFFFF",
    surface_alt="#FAFBFC",
    border="#E2E5EA",
    text="#15181D",
    text_dim="#5C636E",
    text_faint="#8A919C",
    accent="#2563EB",
    accent_text="#FFFFFF",
    ok="#15803D",
    warn="#B45309",
    err="#B91C1C",
    header="#1E3A5F",
    header_text="#FFFFFF",
    track="#E8EBF0",
)

DARK = Palette(
    name="dark",
    bg="#14161A",
    surface="#1D2026",
    surface_alt="#23272E",
    border="#2F343C",
    text="#E9ECF1",
    text_dim="#A2A9B4",
    text_faint="#7A818C",
    accent="#5B9CF8",
    accent_text="#0B1220",
    ok="#4ADE80",
    warn="#FBBF24",
    err="#F87171",
    header="#0F1A2B",
    header_text="#E9ECF1",
    track="#2A2F37",
)

PALETTES = {"light": LIGHT, "dark": DARK}


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
    """按 NAT 类型给出语义色。"""
    if classic_type in ("open", "full_cone"):
        return palette.ok
    if classic_type in ("restricted", "port_restricted", "cone_unknown"):
        return palette.warn
    if classic_type in ("symmetric", "blocked"):
        return palette.err
    return palette.text_dim
