# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""STUN 服务器管理弹窗。"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox

from ..config import Config
from ..servers import DEFAULT_SERVERS, parse_server
from ..theme import get_palette

FONT = "Microsoft YaHei UI"


class ServerDialog(tk.Toplevel):
    def __init__(self, master, config: Config, on_change=None):
        super().__init__(master)
        self.cfg = config
        self.on_change = on_change
        self.palette = get_palette(config.theme)
        p = self.palette

        self.title("STUN 服务器管理")
        self.configure(bg=p.bg)
        self.geometry("560x560")
        self.minsize(480, 460)
        self.transient(master)
        self.grab_set()
        self._center(master)

        self._builtin = {f"{h}:{port}".lower() for h, port in DEFAULT_SERVERS}

        head = tk.Frame(self, bg=p.bg)
        head.pack(fill="x", padx=20, pady=(16, 6))
        tk.Label(head, text="服务器列表", font=(FONT, 12, "bold"), fg=p.text, bg=p.bg).pack(side="left")
        self.lbl_count = tk.Label(head, text="", font=(FONT, 9), fg=p.text_dim, bg=p.bg)
        self.lbl_count.pack(side="right")

        tk.Label(self, text="双击可切换启用 / 禁用。检测时优先使用响应快的节点。",
                 font=(FONT, 8), fg=p.text_faint, bg=p.bg, anchor="w").pack(fill="x", padx=20)

        wrap = tk.Frame(self, bg=p.surface, highlightbackground=p.border, highlightthickness=1)
        wrap.pack(fill="both", expand=True, padx=20, pady=10)
        self.listbox = tk.Listbox(
            wrap, font=("Consolas", 9), bg=p.surface, fg=p.text,
            selectbackground=p.accent, selectforeground=p.accent_text,
            relief="flat", borderwidth=0, highlightthickness=0,
            activestyle="none", exportselection=False,
        )
        self.listbox.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        sb = tk.Scrollbar(wrap, command=self.listbox.yview, width=12,
                          bg=p.surface_alt, troughcolor=p.surface, relief="flat",
                          borderwidth=0, highlightthickness=0, activebackground=p.border)
        sb.pack(side="right", fill="y", pady=10, padx=(0, 6))
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.bind("<Double-Button-1>", lambda e: self._toggle())

        addrow = tk.Frame(self, bg=p.bg)
        addrow.pack(fill="x", padx=20)
        tk.Label(addrow, text="添加", font=(FONT, 9), fg=p.text_dim, bg=p.bg).pack(side="left")
        self.entry = tk.Entry(addrow, font=("Consolas", 9), bg=p.surface, fg=p.text,
                              relief="flat", highlightbackground=p.border, highlightthickness=1,
                              insertbackground=p.text)
        self.entry.pack(side="left", fill="x", expand=True, padx=8, ipady=4)
        self.entry.bind("<Return>", lambda e: self._add())
        self._button(addrow, "添加", self._add, primary=True).pack(side="left")

        tk.Label(self, text="支持 host、host:port、stun:host:port；可用逗号或空格一次添加多个。",
                 font=(FONT, 8), fg=p.text_faint, bg=p.bg, anchor="w").pack(fill="x", padx=20, pady=(4, 0))

        btns = tk.Frame(self, bg=p.bg)
        btns.pack(fill="x", padx=20, pady=14)
        self._button(btns, "启用 / 禁用", self._toggle).pack(side="left")
        self._button(btns, "删除选中", self._delete).pack(side="left", padx=8)
        self._button(btns, "恢复默认列表", self._reset).pack(side="left")
        self._button(btns, "关闭", self._close, primary=True).pack(side="right")

        self._refresh()

    # ---- 辅助 ----

    def _center(self, master) -> None:
        self.update_idletasks()
        try:
            x = master.winfo_rootx() + (master.winfo_width() - 560) // 2
            y = master.winfo_rooty() + (master.winfo_height() - 560) // 2
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        except tk.TclError:
            pass

    def _button(self, parent, text, cmd, primary=False):
        p = self.palette
        if primary:
            b = tk.Button(parent, text=text, command=cmd, font=(FONT, 9, "bold"),
                          bg=p.accent, fg=p.accent_text, activebackground=p.accent,
                          activeforeground=p.accent_text, relief="flat", bd=0,
                          padx=16, pady=5, cursor="hand2")
        else:
            b = tk.Button(parent, text=text, command=cmd, font=(FONT, 9),
                          bg=p.surface_alt, fg=p.text, activebackground=p.border,
                          activeforeground=p.text, relief="flat", bd=0,
                          padx=12, pady=5, cursor="hand2",
                          highlightbackground=p.border, highlightthickness=1)
        return b

    # ---- 数据 ----

    def _rows(self) -> list[tuple[str, bool, bool]]:
        """返回 [(key, enabled, is_builtin)]，顺序：内置在前，自定义在后。"""
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
                s = parse_server(raw)
            except ValueError:
                continue
            low = s.key.lower()
            if low in seen:
                continue
            seen.add(low)
            rows.append((s.key, low not in disabled, False))
        return rows

    def _refresh(self) -> None:
        p = self.palette
        self.listbox.delete(0, "end")
        rows = self._rows()
        enabled = sum(1 for _, ok, _ in rows if ok)
        self.lbl_count.configure(text=f"共 {len(rows)} 个，启用 {enabled} 个")
        for key, on, builtin in rows:
            mark = "●" if on else "○"
            tag = "内置" if builtin else "自定义"
            self.listbox.insert("end", f" {mark}  {key:<34} {tag}")
            if not on:
                self.listbox.itemconfig("end", fg=p.text_faint)
            elif not builtin:
                self.listbox.itemconfig("end", fg=p.accent)
        self._rows_cache = rows

    def _selected_key(self) -> str | None:
        sel = self.listbox.curselection()
        if not sel:
            return None
        idx = sel[0]
        if idx >= len(self._rows_cache):
            return None
        return self._rows_cache[idx][0]

    def _add(self) -> None:
        raw = self.entry.get().strip()
        if not raw:
            return
        parts = [x for x in raw.replace(",", " ").replace(";", " ").split() if x]
        added, failed = 0, []
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
            messagebox.showwarning("部分未添加",
                                   "以下地址无法解析为标准格式：\n" + "\n".join(failed), parent=self)
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
            messagebox.showinfo("提示", "内置服务器不支持删除，可用「启用 / 禁用」关闭。", parent=self)
            return
        self.cfg.remove_server(key)
        self.cfg.save()
        self._refresh()
        if self.on_change:
            self.on_change()

    def _reset(self) -> None:
        if not messagebox.askyesno("恢复默认", "将清除自定义服务器并重新启用全部内置服务器，确定？",
                                   parent=self):
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
