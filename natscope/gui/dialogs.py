# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""弹窗：STUN 服务器管理、首次运行说明。

两处 v1.0 的硬编码在 v1.1 修掉：

* ``geometry("560x560")`` / ``minsize(480, 460)`` 是**物理像素**，200% 缩放下
  只相当于设计尺寸的一半，内容全被挤扁 → 现在一律乘 ``scale`` 并夹到工作区内；
* 列表项 ``f" {mark}  {key:<34} {tag}"`` 按**字符**对齐，而中文是双宽，
  「内置 / 自定义」这一列永远错位 → 现在用 :func:`natscope.gui.widgets.pad_to_px`
  按像素补齐。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox

from .. import brand
from ..config import Config
from ..servers import DEFAULT_SERVERS, parse_server
from ..theme import Palette, Typography, get_palette
from . import metrics
from .widgets import (
    GhostButton,
    PillButton,
    ellipsis,
    font,
    hairline,
    pad_to_px,
    px,
)

FONT = "Microsoft YaHei UI"

#: 服务器列表弹窗的设计尺寸（100% 缩放下的物理像素）
DIALOG_SIZE = (600, 580)
DIALOG_MIN = (520, 460)
#: 首次运行说明弹窗的设计尺寸
FIRST_RUN_SIZE = (560, 480)


class ServerDialog(tk.Toplevel):
    """STUN 服务器列表管理。"""

    def __init__(
        self,
        master: tk.Misc,
        config: Config,
        palette: Palette | None = None,
        scale: float = 1.0,
        on_change=None,
    ):
        super().__init__(master)
        self.cfg = config
        self.on_change = on_change
        self.palette = palette or get_palette(config.theme)
        self.scale = max(1.0, float(scale))
        palette = self.palette

        self.title("STUN 服务器管理")
        self.configure(bg=palette.bg)
        self._size_dialog(master, DIALOG_SIZE, DIALOG_MIN)
        self.transient(master)
        self.grab_set()

        self._builtin = {f"{h}:{port}".lower() for h, port in DEFAULT_SERVERS}
        self._list_font = font(Typography.mono)

        head = tk.Frame(self, bg=palette.bg)
        head.pack(fill="x", padx=px(self.scale, 18), pady=(px(self.scale, 14), px(self.scale, 5)))
        tk.Label(
            head, text="服务器列表", font=font(Typography.heading, bold=True),
            fg=palette.text, bg=palette.bg,
        ).pack(side="left")
        self.lbl_count = tk.Label(
            head, text="", font=font(Typography.note), fg=palette.text_dim, bg=palette.bg
        )
        self.lbl_count.pack(side="right")

        tk.Label(
            self, text="双击可切换启用 / 禁用。检测时优先使用响应快的节点。",
            font=font(Typography.label), fg=palette.text_faint, bg=palette.bg, anchor="w",
        ).pack(fill="x", padx=px(self.scale, 18))

        wrap = tk.Frame(
            self, bg=palette.surface, highlightbackground=palette.border,
            highlightthickness=1, bd=0,
        )
        wrap.pack(fill="both", expand=True, padx=px(self.scale, 18), pady=px(self.scale, 9))
        self.listbox = tk.Listbox(
            wrap,
            font=self._list_font,
            bg=palette.surface,
            fg=palette.text,
            selectbackground=palette.accent,
            selectforeground=palette.accent_text,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            activestyle="none",
            exportselection=False,
        )
        self.listbox.pack(
            side="left", fill="both", expand=True,
            padx=(px(self.scale, 9), 0), pady=px(self.scale, 9),
        )
        scrollbar = tk.Scrollbar(
            wrap, command=self.listbox.yview,
            width=px(self.scale, 10),
            bg=palette.surface_alt, troughcolor=palette.surface,
            activebackground=palette.border, relief="flat", bd=0, highlightthickness=0,
        )
        scrollbar.pack(side="right", fill="y", pady=px(self.scale, 9), padx=(0, px(self.scale, 5)))
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.bind("<Double-Button-1>", lambda _e: self._toggle())

        add_row = tk.Frame(self, bg=palette.bg)
        add_row.pack(fill="x", padx=px(self.scale, 18))
        tk.Label(
            add_row, text="添加", font=font(Typography.note), fg=palette.text_dim, bg=palette.bg
        ).pack(side="left")
        self.entry = tk.Entry(
            add_row,
            font=font(Typography.mono),
            bg=palette.surface,
            fg=palette.text,
            relief="flat",
            highlightbackground=palette.border,
            highlightthickness=1,
            insertbackground=palette.text,
        )
        self.entry.pack(side="left", fill="x", expand=True, padx=px(self.scale, 7),
                        ipady=px(self.scale, 3))
        self.entry.bind("<Return>", lambda _e: self._add())
        PillButton(
            add_row, palette, self.scale, "添加", self._add, pad_x=12.0, pad_y=4.5
        ).pack(side="left")

        tk.Label(
            self, text="支持 host、host:port、stun:host:port；可用逗号或空格一次添加多个。",
            font=font(Typography.label), fg=palette.text_faint, bg=palette.bg, anchor="w",
        ).pack(fill="x", padx=px(self.scale, 18), pady=(px(self.scale, 4), 0))

        hairline(self, palette).pack(side="bottom", fill="x", pady=(px(self.scale, 8), 0))
        buttons = tk.Frame(self, bg=palette.bg)
        buttons.pack(side="bottom", fill="x", padx=px(self.scale, 18), pady=px(self.scale, 12))
        GhostButton(buttons, palette, self.scale, "启用 / 禁用", self._toggle).pack(side="left")
        GhostButton(buttons, palette, self.scale, "删除选中", self._delete).pack(
            side="left", padx=px(self.scale, 7)
        )
        GhostButton(buttons, palette, self.scale, "恢复默认列表", self._reset).pack(side="left")
        PillButton(buttons, palette, self.scale, "关闭", self._close).pack(side="right")

        self._refresh()

    # ---- 尺寸 ----

    def _size_dialog(self, master, design, minimum) -> None:
        """按缩放设定尺寸并居中到父窗口（并夹进工作区）。"""
        work = metrics.work_area()
        layout = metrics.resolve(work, self.scale, design=design, floor=minimum,
                                 compact_floor=(min(420, design[0]), min(360, design[1])))
        # 宽高直接取 resolve 的结果（它已经乘过 scale 并夹进工作区）；
        # 千万别再跟"未缩放的 design"取 min —— 那样 200% 下弹窗只有设计尺寸，
        # 内容会被挤成一半（v1.0 的 560x560 硬编码就是这个后果）。
        width = layout.width
        height = layout.height
        self.minsize(min(layout.min_w, width), min(layout.min_h, height))
        try:
            root_x = master.winfo_rootx()
            root_y = master.winfo_rooty()
            root_w = master.winfo_width()
            root_h = master.winfo_height()
        except tk.TclError:
            root_x = root_y = root_w = root_h = 0
        x = root_x + max(0, (root_w - width) // 2)
        y = root_y + max(0, (root_h - height) // 2)
        x = min(max(x, work[0]), max(work[0], work[0] + work[2] - width))
        y = min(max(y, work[1]), max(work[1], work[1] + work[3] - height))
        # geometry 的宽高是"内容尺寸"，与 resolve 的物理像素一致
        self.geometry(f"{width}x{height}{x:+d}{y:+d}")

    # ---- 数据 ----

    def _rows(self) -> list[tuple[str, bool, bool]]:
        """返回 ``[(key, enabled, is_builtin)]``：内置在前，自定义在后。"""
        disabled = {d.lower() for d in self.cfg.disabled}
        rows: list[tuple[str, bool, bool]] = []
        seen: set[str] = set()
        for host, port in DEFAULT_SERVERS:
            key = f"{host}:{port}"
            low = key.lower()
            if low in seen:
                continue
            seen.add(low)
            rows.append((key, low not in disabled, True))
        for raw in self.cfg.custom_servers:
            try:
                server = parse_server(raw)
            except ValueError:
                continue
            low = server.key.lower()
            if low in seen:
                continue
            seen.add(low)
            rows.append((server.key, low not in disabled, False))
        return rows

    def _refresh(self) -> None:
        palette = self.palette
        self.listbox.delete(0, "end")
        rows = self._rows()
        enabled = sum(1 for _, on, _ in rows if on)
        self.lbl_count.configure(text=f"共 {len(rows)} 个，启用 {enabled} 个")
        # 三列按像素对齐：标记列 -> 地址列 -> 类型列
        tag_column = px(self.scale, 250)
        for key, on, builtin in rows:
            mark = "●" if on else "○"
            tag = "内置" if builtin else "自定义"
            prefix = f"{mark}  "
            room = max(px(self.scale, 60),
                       tag_column - self._list_font.measure(prefix) - px(self.scale, 6))
            shown_key = ellipsis(key, self._list_font, room)
            line = prefix + pad_to_px(shown_key, self._list_font, room) + "  " + tag
            self.listbox.insert("end", line)
            if not on:
                self.listbox.itemconfig("end", fg=palette.text_faint)
            elif not builtin:
                self.listbox.itemconfig("end", fg=palette.accent)
        self._rows_cache = rows

    def _selected_key(self) -> str | None:
        selection = self.listbox.curselection()
        if not selection:
            return None
        index = int(selection[0])
        if index >= len(self._rows_cache):
            return None
        return self._rows_cache[index][0]

    def _add(self) -> None:
        raw = self.entry.get().strip()
        if not raw:
            return
        parts = [x for x in raw.replace(",", " ").replace(";", " ").split() if x]
        added = 0
        failed: list[str] = []
        for part in parts:
            try:
                self.cfg.add_server(part)
                added += 1
            except ValueError:
                failed.append(part)
        self.cfg.save()
        self.entry.delete(0, "end")
        self._refresh()
        if failed:
            messagebox.showwarning(
                "部分未添加",
                "以下地址无法解析为标准格式：\n" + "\n".join(failed),
                parent=self,
            )
        if added and self.on_change:
            self.on_change()

    def _toggle(self) -> None:
        key = self._selected_key()
        if not key:
            return
        low = key.lower()
        currently_on = low not in {d.lower() for d in self.cfg.disabled}
        self.cfg.set_enabled(key, not currently_on)
        self.cfg.save()
        self._refresh()
        if self.on_change:
            self.on_change()

    def _delete(self) -> None:
        key = self._selected_key()
        if not key:
            return
        if key.lower() in self._builtin:
            messagebox.showinfo(
                "提示", "内置服务器不支持删除，可用「启用 / 禁用」关闭。", parent=self
            )
            return
        self.cfg.remove_server(key)
        self.cfg.save()
        self._refresh()
        if self.on_change:
            self.on_change()

    def _reset(self) -> None:
        if not messagebox.askyesno(
            "恢复默认",
            "将清除自定义服务器并重新启用全部内置服务器，确定？",
            parent=self,
        ):
            return
        self.cfg.custom_servers = []
        self.cfg.disabled = []
        self.cfg.save()
        self._refresh()
        if self.on_change:
            self.on_change()

    def _close(self) -> None:
        self.grab_release()
        self.destroy()


class FirstRunDialog(tk.Toplevel):
    """首次运行的一次性说明。

    存在的理由很具体：这个工具会主动往外发 UDP 包、还会向网关发 SSDP 组播，
    杀软/防火墙弹窗拦截时用户第一反应是"这软件在干什么"。与其让人猜，不如
    第一次就把"做什么 / 不做什么"写清楚。
    """

    def __init__(self, master: tk.Misc, palette: Palette, scale: float, on_ack=None):
        super().__init__(master)
        self.palette = palette
        self.scale = max(1.0, float(scale))
        self.on_ack = on_ack
        self.title(f"关于 {brand.NAME} 的网络访问")
        self.configure(bg=palette.bg)
        self.transient(master)
        self._size_dialog(master)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._close)

        pad = px(self.scale, 18)
        tk.Label(
            self,
            text=f"{brand.NAME} 会做哪些网络访问",
            font=font(Typography.heading, bold=True),
            fg=palette.text,
            bg=palette.bg,
            anchor="w",
        ).pack(fill="x", padx=pad, pady=(px(self.scale, 14), px(self.scale, 4)))

        body_width = px(self.scale, FIRST_RUN_SIZE[0] - 56)
        for title, text in (
            ("会做的三类探测", ""),
            (
                "① STUN 服务器探测",
                "向公共 STUN 服务器发送一次 UDP Binding 请求，测量连通性与往返时延。",
            ),
            (
                "② NAT 行为探测",
                "复用同一个本地 UDP 套接字，向少数服务器换地址/换端口重发，判断映射与过滤行为。",
            ),
            (
                "③ 网关端口映射查询",
                "向本机默认网关发一次 UPnP SSDP 组播与 NAT-PMP / PCP 查询，只查询、不修改任何映射。",
            ),
            ("不会做的事", ""),
            ("不监听端口", "不开放任何入站服务，不驻留后台，退出即结束。"),
            ("不上传数据", "没有遥测、没有统计、没有账号；只在本地写 config.json 与你主动导出的报告。"),
        ):
            if not text:
                tk.Label(
                    self, text=title, font=font(Typography.label, bold=True),
                    fg=palette.text_faint, bg=palette.bg, anchor="w",
                ).pack(fill="x", padx=pad, pady=(px(self.scale, 8), px(self.scale, 2)))
                continue
            tk.Label(
                self,
                text=title,
                font=font(Typography.body, bold=True),
                fg=palette.text,
                bg=palette.bg,
                anchor="w",
            ).pack(fill="x", padx=pad, pady=(px(self.scale, 4), 0))
            tk.Label(
                self,
                text=text,
                font=font(Typography.note),
                fg=palette.text_dim,
                bg=palette.bg,
                anchor="w",
                justify="left",
                wraplength=body_width,
            ).pack(fill="x", padx=pad, pady=(px(self.scale, 1), 0))

        tk.Label(
            self,
            text="若防火墙询问，允许其访问网络即可；拒绝也不会损坏系统，只是无法检测。",
            font=font(Typography.label),
            fg=palette.text_faint,
            bg=palette.bg,
            anchor="w",
        ).pack(fill="x", padx=pad, pady=(px(self.scale, 10), 0))

        hairline(self, palette).pack(side="bottom", fill="x", pady=(px(self.scale, 8), 0))
        row = tk.Frame(self, bg=palette.bg)
        row.pack(side="bottom", fill="x", padx=pad, pady=px(self.scale, 10))
        PillButton(row, palette, self.scale, "我知道了", self._close, min_width=90).pack(side="right")

    def _size_dialog(self, master: tk.Misc) -> None:
        work = metrics.work_area()
        layout = metrics.resolve(
            work, self.scale, design=FIRST_RUN_SIZE, floor=(460, 380),
            compact_floor=(420, 340),
        )
        width = layout.width
        height = layout.height
        try:
            root_x, root_y = master.winfo_rootx(), master.winfo_rooty()
            root_w, root_h = master.winfo_width(), master.winfo_height()
        except tk.TclError:
            root_x = root_y = root_w = root_h = 0
        x = root_x + max(0, (root_w - width) // 2)
        y = root_y + max(0, (root_h - height) // 3)
        x = min(max(x, work[0]), max(work[0], work[0] + work[2] - width))
        y = min(max(y, work[1]), max(work[1], work[1] + work[3] - height))
        self.geometry(f"{width}x{height}{x:+d}{y:+d}")

    def _close(self) -> None:
        if self.on_ack:
            self.on_ack()
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


__all__ = ["FirstRunDialog", "ServerDialog"]
