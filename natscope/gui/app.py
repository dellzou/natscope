# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""NatScope 主窗口。"""

from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .. import __version__
from ..config import THEME_AUTO, THEME_DARK, THEME_LIGHT, Config
from ..gateway import GatewayReport, probe_gateway
from ..nat import NatDetector, NatResult
from ..report import ReportContext, default_filename, save_image, save_text
from ..servers import ProbeSummary, probe_servers
from ..theme import Palette, get_palette, system_theme
from .dialogs import ServerDialog

FONT = "Microsoft YaHei UI"


def resource_path(rel: str) -> str:
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    return os.path.join(base, rel)


def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class App(tk.Tk):
    def __init__(self, config: Config | None = None):
        super().__init__()
        enable_dpi_awareness()
        self.config_data = config or Config.load()
        self.theme_mode = self.config_data.theme
        self.palette: Palette = get_palette(self.theme_mode)

        self.title(f"NatScope — NAT 类型检测  v{__version__}")

        # 高 DPI：先把 tk 的字体缩放对齐到真实 DPI，再按同一比例放大窗口，
        # 否则 200% 缩放下窗口只有一半大小，控件全被挤扁
        try:
            self.tk.call("tk", "scaling", self.winfo_fpixels("1i") / 72.0)
        except Exception:
            pass
        self._scale = max(1.0, self.winfo_fpixels("1i") / 96.0)

        self.minsize(int(860 * self._scale), int(660 * self._scale))
        self._center()

        try:
            ico = resource_path("assets/icon.ico")
            if os.path.exists(ico):
                self.iconbitmap(ico)
        except Exception:
            pass

        self._q: queue.Queue = queue.Queue()
        self._running = False
        self._result: NatResult | None = None
        self._gateway: GatewayReport | None = None
        self._probe: ProbeSummary | None = None
        self._log_lines: list[str] = []

        self._init_ttk()
        self._build()
        self.after(120, self._drain)
        self.after(400, self._start)  # 启动即自动开始一轮，省去一次点击

    # ------------------------------------------------------------------ 基础

    def _center(self) -> None:
        self.update_idletasks()
        s = getattr(self, "_scale", 1.0)
        w, h = int(980 * s), int(760 * s)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(w, sw - 60), min(h, sh - 90)
        self.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2 - 10)}")

    def _init_ttk(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        p = self.palette
        style.configure(
            "Vertical.TScrollbar",
            background=p.surface_alt,
            troughcolor=p.bg,
            bordercolor=p.bg,
            arrowcolor=p.text_dim,
            darkcolor=p.surface_alt,
            lightcolor=p.surface_alt,
            relief="flat",
        )
        style.map("Vertical.TScrollbar", background=[("active", p.border)])

    # ------------------------------------------------------------------ 构建

    def _build(self) -> None:
        p = self.palette
        self.configure(bg=p.bg)
        self._build_top()
        self._build_bottom()
        self._build_body()

    def _build_top(self) -> None:
        p = self.palette
        top = tk.Frame(self, bg=p.surface, highlightbackground=p.border, highlightthickness=0)
        top.pack(side="top", fill="x")
        tk.Frame(self, bg=p.border, height=1).pack(side="top", fill="x")

        row = tk.Frame(top, bg=p.surface)
        row.pack(fill="x", padx=22, pady=(13, 13))

        tk.Label(row, text="NatScope", font=(FONT, 16, "bold"), fg=p.text, bg=p.surface).pack(side="left")
        tk.Label(row, text="NAT 类型检测 · RFC 3489 / RFC 5780", font=(FONT, 9),
                 fg=p.text_faint, bg=p.surface).pack(side="left", padx=(12, 0), pady=(5, 0))

        self.btn_servers = self._ghost_button(row, "STUN 服务器", self._open_servers)
        self.btn_servers.pack(side="right")
        self.btn_theme = self._ghost_button(row, self._theme_text(), self._cycle_theme)
        self.btn_theme.pack(side="right", padx=(0, 8))

    def _build_bottom(self) -> None:
        p = self.palette
        self._progress_track = tk.Frame(self, bg=p.track, height=3)
        self._progress_track.pack(side="bottom", fill="x")
        self._progress_fill = tk.Frame(self._progress_track, bg=p.accent)
        self._progress_fill.place(x=0, y=0, relheight=1, relwidth=0)

        tk.Frame(self, bg=p.border, height=1).pack(side="bottom", fill="x")
        bar = tk.Frame(self, bg=p.surface)
        bar.pack(side="bottom", fill="x")
        row = tk.Frame(bar, bg=p.surface)
        row.pack(fill="x", padx=22, pady=12)

        self.btn_start = self._primary_button(row, "开始检测", self._start)
        self.btn_start.pack(side="left")
        self.btn_txt = self._ghost_button(row, "导出 TXT", lambda: self._export("txt"))
        self.btn_txt.pack(side="left", padx=(10, 0))
        self.btn_jpg = self._ghost_button(row, "导出 JPG", lambda: self._export("jpg"))
        self.btn_jpg.pack(side="left", padx=(8, 0))

        self.lbl_status = tk.Label(row, text="准备就绪", font=(FONT, 9), fg=p.text_dim, bg=p.surface)
        self.lbl_status.pack(side="right")

    def _build_body(self) -> None:
        p = self.palette
        body = tk.Frame(self, bg=p.bg)
        body.pack(side="top", fill="both", expand=True)

        # 结论卡
        card = tk.Frame(body, bg=p.surface, highlightbackground=p.border, highlightthickness=1)
        card.pack(fill="x", padx=22, pady=(16, 10))
        self._accent = tk.Frame(card, bg=p.text_faint, width=5)
        self._accent.pack(side="left", fill="y")
        inner = tk.Frame(card, bg=p.surface)
        inner.pack(side="left", fill="both", expand=True, padx=20, pady=14)
        self.lbl_type = tk.Label(inner, text="尚未检测", font=(FONT, 22, "bold"),
                                 fg=p.text, bg=p.surface, anchor="w")
        self.lbl_type.pack(fill="x")
        self.lbl_sub = tk.Label(inner, text="点击「开始检测」", font=(FONT, 9),
                                fg=p.text_dim, bg=p.surface, anchor="w")
        self.lbl_sub.pack(fill="x", pady=(4, 0))

        # 三小卡
        trio = tk.Frame(body, bg=p.bg)
        trio.pack(fill="x", padx=22)
        for i in range(3):
            trio.columnconfigure(i, weight=1, uniform="t")
        self._mini = {}
        specs = [("mapping", "映射行为 Mapping"), ("filtering", "过滤行为 Filtering"), ("gateway", "端口映射 Gateway")]
        for i, (key, label) in enumerate(specs):
            c = tk.Frame(trio, bg=p.surface, highlightbackground=p.border, highlightthickness=1)
            c.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0))
            tk.Label(c, text=label, font=(FONT, 8), fg=p.text_faint, bg=p.surface, anchor="w").pack(
                fill="x", padx=16, pady=(11, 0))
            v = tk.Label(c, text="—", font=(FONT, 11, "bold"), fg=p.text, bg=p.surface, anchor="w")
            v.pack(fill="x", padx=16, pady=(3, 12))
            self._mini[key] = v

        # 网络信息
        info = tk.Frame(body, bg=p.surface, highlightbackground=p.border, highlightthickness=1)
        info.pack(fill="x", padx=22, pady=10)
        grid = tk.Frame(info, bg=p.surface)
        grid.pack(fill="x", padx=18, pady=11)
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)
        self._info = {}
        pairs = [("local", "本机地址"), ("public", "公网映射"), ("gw", "默认网关"), ("server", "主 STUN 服务器")]
        for i, (key, label) in enumerate(pairs):
            r, c = divmod(i, 2)
            tk.Label(grid, text=label, font=(FONT, 8), fg=p.text_faint, bg=p.surface,
                     anchor="w", width=12).grid(row=r, column=c * 2, sticky="w", pady=3)
            v = tk.Label(grid, text="—", font=("Consolas", 10), fg=p.text, bg=p.surface, anchor="w")
            v.grid(row=r, column=c * 2 + 1, sticky="w", pady=3, padx=(0, 24))
            self._info[key] = v

        # 日志
        logcard = tk.Frame(body, bg=p.surface, highlightbackground=p.border, highlightthickness=1)
        logcard.pack(fill="both", expand=True, padx=22, pady=(0, 16))
        head = tk.Frame(logcard, bg=p.surface)
        head.pack(fill="x", padx=16, pady=(10, 0))
        tk.Label(head, text="检测日志", font=(FONT, 8), fg=p.text_faint, bg=p.surface, anchor="w").pack(side="left")

        wrap = tk.Frame(logcard, bg=p.surface)
        wrap.pack(fill="both", expand=True, padx=12, pady=(6, 12))
        self.log = tk.Text(wrap, height=6, bg=p.surface, fg=p.text_dim, font=("Consolas", 9),
                           relief="flat", wrap="word", state="disabled", borderwidth=0,
                           highlightthickness=0, insertbackground=p.text,
                           selectbackground=p.accent, padx=4, pady=2)
        self.log.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.log.yview, style="Vertical.TScrollbar")
        sb.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=sb.set)

    # ------------------------------------------------------------------ 控件工厂

    def _primary_button(self, parent, text, cmd):
        p = self.palette
        b = tk.Button(parent, text=text, command=cmd, font=(FONT, 10, "bold"),
                      bg=p.accent, fg=p.accent_text, activebackground=p.accent,
                      activeforeground=p.accent_text, relief="flat", bd=0,
                      padx=20, pady=6, cursor="hand2")
        b.bind("<Enter>", lambda e: b.configure(bg=p.accent_text, fg=p.accent))
        b.bind("<Leave>", lambda e: b.configure(bg=p.accent, fg=p.accent_text))
        return b

    def _ghost_button(self, parent, text, cmd):
        p = self.palette
        b = tk.Button(parent, text=text, command=cmd, font=(FONT, 9),
                      bg=p.surface_alt, fg=p.text, activebackground=p.border,
                      activeforeground=p.text, relief="flat", bd=0,
                      padx=12, pady=5, cursor="hand2",
                      highlightbackground=p.border, highlightthickness=1)
        b.bind("<Enter>", lambda e: b.configure(bg=p.border))
        b.bind("<Leave>", lambda e: b.configure(bg=p.surface_alt))
        return b

    # ------------------------------------------------------------------ 主题

    def _theme_text(self) -> str:
        return {THEME_AUTO: "主题：自动", THEME_LIGHT: "主题：浅色", THEME_DARK: "主题：深色"}[self.theme_mode]

    def _cycle_theme(self) -> None:
        order = [THEME_AUTO, THEME_LIGHT, THEME_DARK]
        self.theme_mode = order[(order.index(self.theme_mode) + 1) % 3]
        self.config_data.theme = self.theme_mode
        self.config_data.save()
        self._apply_theme()

    def _apply_theme(self) -> None:
        self.palette = get_palette(self.theme_mode)
        self._init_ttk()
        saved_log = self.log.get("1.0", "end-1c") if hasattr(self, "log") else ""
        for child in self.winfo_children():
            child.destroy()
        self._build()
        if saved_log:
            self._set_log(saved_log)
        if self._result:
            self._render()

    # ------------------------------------------------------------------ 日志与状态

    def _append_log(self, text: str) -> None:
        self._log_lines.append(text)
        if len(self._log_lines) > 800:
            self._log_lines = self._log_lines[-800:]
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("1.0", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _status(self, text: str) -> None:
        self.lbl_status.configure(text=text)

    def _progress(self, ratio: float) -> None:
        ratio = max(0.0, min(1.0, ratio))
        self._progress_fill.place_configure(relwidth=ratio)

    # ------------------------------------------------------------------ 检测流程

    def _post(self, item) -> None:
        self._q.put(item)

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "status":
                    self._status(payload)
                elif kind == "progress":
                    self._progress(payload)
                elif kind == "done":
                    nat, gw, probe = payload
                    self._result, self._gateway, self._probe = nat, gw, probe
                    self._running = False
                    self.btn_start.configure(text="开始检测", state="normal")
                    self._progress(1.0)
                    self._render()
                elif kind == "error":
                    self._running = False
                    self.btn_start.configure(text="开始检测", state="normal")
                    self._status("检测失败")
                    messagebox.showerror("检测失败", payload)
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def _start(self) -> None:
        if self._running:
            return
        self._running = True
        self.btn_start.configure(text="检测中…", state="disabled")
        self._stale_result()
        self._set_log("")
        self._log_lines = []
        self._progress(0.02)
        self._status("正在探测 STUN 服务器…")
        threading.Thread(target=self._worker, daemon=True).start()

    def _stale_result(self) -> None:
        p = self.palette
        self.lbl_type.configure(text="检测中…", fg=p.text_dim)
        self.lbl_sub.configure(text="正在依次执行 STUN 探测与 NAT 行为分析")
        self._accent.configure(bg=p.text_faint)
        for v in self._mini.values():
            v.configure(text="…", fg=p.text_faint)
        for v in self._info.values():
            v.configure(text="…")

    def _worker(self) -> None:
        try:
            cfg = self.config_data
            servers = cfg.all_servers()
            self._post(("log", f"共 {len(servers)} 个 STUN 服务器，开始并发探测…"))

            def progress(done, total, result):
                ratio = 0.05 + 0.45 * (done / max(1, total))
                self._post(("progress", ratio))
                if done == total:
                    self._post(("status", "服务器探测完成，开始 NAT 检测…"))

            results = probe_servers(servers, timeout=1.2, retry_timeout=2.0,
                                    workers=32, progress=progress)
            summary = ProbeSummary(results)
            self._post(("log",
                        f"探测结果：优选 {len(summary.preferred)} / 备选 {len(summary.backup)} / "
                        f"不可用 {len(summary.unavailable)}"))
            for r in summary.usable[:8]:
                self._post(("log", f"  {r.tier_label}  {r.server.label}  {r.rtt_ms:.0f} ms"))
            if not summary.usable:
                self._post(("error", "所有 STUN 服务器均不可用。请检查网络，或改用自定义服务器。"))
                return

            self._post(("progress", 0.55))
            self._post(("status", "正在分析 NAT 行为…"))
            detector = NatDetector(timeout=cfg.timeout, log=lambda m: self._post(("log", m)))
            nat = detector.run(results)
            if not nat.ok:
                self._post(("log", f"检测未完成：{nat.error}"))

            self._post(("progress", 0.85))
            self._post(("status", "正在检测网关端口映射能力…"))
            gw = probe_gateway(nat.local_ip)
            self._post(("log", f"网关 {gw.gateway_ip or '未知'}："
                               + "，".join(f"{c.name} {c.state_label}" for c in gw.capabilities)))
            self._post(("done", (nat, gw, summary)))
        except Exception as exc:  # 兜底：任何异常都不允许把窗口带崩
            self._post(("log", f"异常：{type(exc).__name__}: {exc}"))
            self._post(("error", f"检测过程出现异常：\n{type(exc).__name__}: {exc}"))

    # ------------------------------------------------------------------ 渲染

    def _render(self) -> None:
        nat, gw = self._result, self._gateway
        if not nat:
            return
        p = self.palette
        color = _type_color(p, nat.classic_type)
        self.lbl_type.configure(text=nat.classic_label, fg=color)
        self.lbl_sub.configure(
            text=f"{nat.classic_en} · 映射 {nat.mapping_label} · 过滤 {nat.filtering_label}"
            if nat.classic_en else nat.mapping_label
        )
        self._accent.configure(bg=color)

        self._mini["mapping"].configure(text=nat.mapping_label.split("（")[0], fg=p.text)
        self._mini["filtering"].configure(text=nat.filtering_label.split("（")[0], fg=p.text)
        if gw:
            names = [c.name.replace("UPnP-IGD", "UPnP") for c in gw.capabilities if c.available]
            if names:
                self._mini["gateway"].configure(text=" / ".join(names), fg=p.ok)
            elif any(c.state == "limited" for c in gw.capabilities):
                self._mini["gateway"].configure(text="存在但受限", fg=p.warn)
            else:
                self._mini["gateway"].configure(text="未发现", fg=p.text_dim)
        else:
            self._mini["gateway"].configure(text="未检测", fg=p.text_dim)

        self._info["local"].configure(text=f"{nat.local_ip}:{nat.local_port}")
        self._info["public"].configure(text=f"{nat.public_ip}:{nat.public_port}")
        self._info["gw"].configure(text=(gw.gateway_ip if gw and gw.gateway_ip else "—"))
        self._info["server"].configure(text=nat.primary_server or "—")

        self._status(f"检测完成 · 耗时 {nat.duration_ms / 1000:.1f}s")
        self._append_log("")
        self._append_log("— 结论 —")
        self._append_log(nat.classic_label)
        self._append_log(nat.summary)
        for w in nat.warnings:
            self._append_log(f"提示：{w}")

    # ------------------------------------------------------------------ 导出

    def _export(self, kind: str) -> None:
        if not self._result:
            messagebox.showinfo("提示", "请先执行一次检测。")
            return
        ext = "txt" if kind == "txt" else "jpg"
        init_dir = self.config_data.last_dir or os.path.join(os.path.expanduser("~"), "Desktop")
        if not os.path.isdir(init_dir):
            init_dir = os.path.expanduser("~")
        path = filedialog.asksaveasfilename(
            parent=self,
            title=f"导出 {ext.upper()} 报告",
            initialdir=init_dir,
            initialfile=default_filename("NatScope报告", ext),
            defaultextension=f".{ext}",
            filetypes=[("文本文件" if ext == "txt" else "JPEG 图片", f"*.{ext}"), ("所有文件", "*.*")],
        )
        if not path:
            return
        ctx = ReportContext(nat=self._result, gateway=self._gateway, probe=self._probe)
        try:
            if ext == "txt":
                save_text(ctx, path)
            else:
                save_image(ctx, path, self.palette)
        except Exception as exc:
            messagebox.showerror("导出失败", f"{type(exc).__name__}: {exc}")
            return
        self.config_data.last_dir = os.path.dirname(path)
        self.config_data.save()
        self._status(f"已导出 {os.path.basename(path)}")

    # ------------------------------------------------------------------ 服务器管理

    def _open_servers(self) -> None:
        ServerDialog(self, self.config_data, on_change=self._on_servers_changed)

    def _on_servers_changed(self) -> None:
        self._status("服务器列表已更新，重新检测生效")


def _type_color(p: Palette, classic_type: str) -> str:
    if classic_type in ("open", "full_cone"):
        return p.ok
    if classic_type in ("restricted", "port_restricted", "cone_unknown"):
        return p.warn
    if classic_type in ("symmetric", "blocked"):
        return p.err
    return p.text_dim


def run() -> None:
    app = App()
    app.mainloop()
