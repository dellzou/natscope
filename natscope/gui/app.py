# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""NatScope 主窗口。

布局（从上到下）
----------------
顶栏（品牌标记 + 主题/服务器按钮）→ 结论卡（大字 + verdict 副标题 + 行为矩阵
缩略）→ 三小卡（映射/过滤/网关）→ 网络信息 → 结果可视化区（矩阵 / 类型谱 /
样本 / 场景 / 步骤）→ 日志（可折叠）→ 底栏（开始检测 / 导出 / 状态）。

关键结构约定
------------
* **底栏先 pack**（``side="bottom"``），主体后 pack（``expand=True``）。
  pack 的分配顺序保证了"无论窗口多小，底栏永远先拿到自己的高度"——
  这条与 :mod:`natscope.gui.metrics` 的 ``min_h <= work_h - 32`` 一起，
  从两个方向堵住 v1.0 "底栏被任务栏吞掉"的问题。
* 尺寸、最小尺寸、日志默认是否折叠全部来自 ``metrics.resolve()`` 的
  :class:`~natscope.gui.metrics.Layout`，代码里不再出现 ``980``/``860``
  这类魔数，也不再自己算屏幕居中。
* 主题切换只**重建 + 恢复显示**，不重放任何日志（见 ``_apply_theme``）。
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

from .. import __version__, brand, viz
from ..config import THEME_AUTO, THEME_DARK, THEME_LIGHT, Config
from ..gateway import GatewayReport, probe_gateway
from ..nat import NatDetector, NatResult
from ..report import ReportContext, default_filename, save_image, save_text
from ..servers import ProbeSummary, probe_servers
from ..theme import Palette, Typography, get_palette, type_color
from . import metrics
from .dialogs import FirstRunDialog, ServerDialog
from .result_view import MatrixView, ResultView
from .widgets import (
    FONT_FAMILY,
    Card,
    GhostButton,
    InfoGrid,
    LogoMark,
    PillButton,
    ScrollFrame,
    font,
    hairline,
    px,
    reset_font_cache,
)

#: 兼容旧引用（v1.0 的 app.FONT）
FONT = FONT_FAMILY


def resource_path(rel: str) -> str:
    """打包后从 ``sys._MEIPASS`` 取资源，源码运行时从仓库根取。"""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(
        os.path.dirname(os.path.dirname(__file__))
    )
    return os.path.join(base, rel)


def enable_dpi_awareness() -> None:
    """声明 DPI 感知（必须在建窗口前调用，否则界面被系统拉伸发虚）。"""
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
    """主窗口。``autostart=False`` 时不自动开测（截图脚本与测试用）。"""

    def __init__(self, config: Config | None = None, *, autostart: bool = True):
        # DPI 感知必须在**任何窗口创建之前**声明（main.py 也会先调一次；
        # 这里再调一遍是为了覆盖"直接 new App()"的用法，例如截图脚本与测试）。
        enable_dpi_awareness()
        super().__init__()
        reset_font_cache()  # 新根窗口：旧 Font 对象已失效

        self.config_data = config or Config.load()
        self.theme_mode = self.config_data.theme
        self.palette: Palette = get_palette(self.theme_mode)

        self.title(f"{brand.NAME} — {brand.TAGLINE}  v{__version__}")

        # 缩放比例：先对齐 tk 的字体缩放，再记下 1 英寸的物理像素数
        self._scale = self._measure_scale()
        self._work = metrics.work_area()
        self._layout = metrics.resolve(self._work, self._scale)
        self._apply_window_geometry()

        try:
            icon = resource_path("assets/icon.ico")
            if os.path.exists(icon):
                self.iconbitmap(icon)
        except Exception:
            pass

        self._q: queue.Queue = queue.Queue()
        self._running = False
        self._closing = False
        self._result: NatResult | None = None
        self._gateway: GatewayReport | None = None
        self._probe: ProbeSummary | None = None
        self._log_lines: list[str] = []
        self._progress_ratio = 0.0
        self._status_text = "准备就绪"
        self._dpi_pending: float | None = None
        # 紧凑/滚动档默认折叠日志：结论与矩阵先看见，日志要看再展开
        self._log_open = not self._layout.compact and not self.config_data.compact

        self._build()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # 记住定时器句柄：关窗口时统一取消，否则 destroy 之后 Tcl 还会去执行
        # 已删除的命令，终端里留下一串 invalid command name "..._drain"
        self._boot_ids: list[str] = [self.after(600, self._maybe_first_run)]
        self._drain_id = self.after(120, self._drain)
        self._dpi_id = self.after(1500, self._poll_dpi)
        if autostart:
            # 启动即自动开始一轮，省去一次点击
            self._boot_ids.append(self.after(400, self._start))

    # ------------------------------------------------------------------ 基础

    def _measure_scale(self) -> float:
        """返回 ``当前 DPI / 96``（100% 缩放 = 1.0），并把 tk 的字体缩放对齐。"""
        try:
            self.tk.call("tk", "scaling", self.winfo_fpixels("1i") / 72.0)
        except Exception:
            pass
        try:
            return max(1.0, self.winfo_fpixels("1i") / 96.0)
        except Exception:
            return 1.0

    def _apply_window_geometry(self) -> None:
        """套用布局：``minsize`` 与初始几何（优先恢复上次保存的位置）。"""
        self.minsize(self._layout.min_w, self._layout.min_h)
        saved = metrics.clamp_geometry(self.config_data.window_geometry, self._work)
        try:
            self.geometry(saved or metrics.centered(self._layout, self._work))
        except tk.TclError:  # 几何串异常时退回居中
            self.geometry(metrics.centered(self._layout, self._work))

    # ------------------------------------------------------------------ 构建

    def _build(self) -> None:
        self.configure(bg=self.palette.bg)
        self._build_top()
        self._build_bottom()  # 先占底栏：pack 顺序保证它永不被人挤掉
        self._build_body()

    def _build_top(self) -> None:
        palette = self.palette
        pad = self._layout.pad
        top = tk.Frame(self, bg=palette.surface)
        top.pack(side="top", fill="x")
        hairline(top, palette).pack(side="bottom", fill="x")

        row = tk.Frame(top, bg=palette.surface)
        row.pack(fill="x", padx=pad, pady=(px(self._scale, 10), px(self._scale, 10)))

        LogoMark(
            row, palette, self._scale, size=26, background=palette.surface
        ).pack(side="left")
        tk.Label(
            row,
            text=brand.NAME,
            font=font(Typography.title, bold=True),
            fg=palette.text,
            bg=palette.surface,
        ).pack(side="left", padx=(px(self._scale, 10), 0))
        tk.Label(
            row,
            text=f"{brand.TAGLINE} · {brand.SUBTITLE}",
            font=font(Typography.note),
            fg=palette.text_faint,
            bg=palette.surface,
        ).pack(side="left", padx=(px(self._scale, 10), 0), pady=(px(self._scale, 5), 0))

        self.btn_servers = GhostButton(
            row, palette, self._scale, "STUN 服务器", self._open_servers
        )
        self.btn_servers.pack(side="right")
        self.btn_theme = GhostButton(
            row, palette, self._scale, self._theme_text(), self._cycle_theme
        )
        self.btn_theme.pack(side="right", padx=(0, px(self._scale, 8)))

    def _build_bottom(self) -> None:
        palette = self.palette
        pad = self._layout.pad

        self._progress_track = tk.Frame(
            self, bg=palette.track, height=max(2, px(self._scale, 3))
        )
        self._progress_track.pack(side="bottom", fill="x")
        self._progress_fill = tk.Frame(self._progress_track, bg=palette.accent)
        self._progress_fill.place(x=0, y=0, relheight=1.0, relwidth=0.0)

        hairline(self, palette).pack(side="bottom", fill="x")
        bar = tk.Frame(self, bg=palette.surface)
        bar.pack(side="bottom", fill="x")
        row = tk.Frame(bar, bg=palette.surface)
        row.pack(fill="x", padx=pad, pady=px(self._scale, 9))

        self.btn_start = PillButton(
            row, palette, self._scale, "开始检测", self._start, min_width=98
        )
        self.btn_start.pack(side="left")
        self.btn_txt = GhostButton(
            row, palette, self._scale, "导出 TXT", lambda: self._export("txt")
        )
        self.btn_txt.pack(side="left", padx=(px(self._scale, 10), 0))
        self.btn_jpg = GhostButton(
            row, palette, self._scale, "导出 JPG", lambda: self._export("jpg")
        )
        self.btn_jpg.pack(side="left", padx=(px(self._scale, 8), 0))

        self.lbl_status = tk.Label(
            row,
            text=self._status_text,
            font=font(Typography.note),
            fg=palette.text_dim,
            bg=palette.surface,
            anchor="e",
        )
        self.lbl_status.pack(side="right")

    def _build_body(self) -> None:
        palette = self.palette
        # 主体**始终**放在滚动容器里：内容比窗口高时可以滚到，绝不裁掉任何一块。
        # 只有一层滚动（页面级），结果区不再自己套 Canvas——两层嵌套滚动会让
        # 滚轮在"内层到头后是否传给外层"上行为不确定，而且内层视口会把矩阵
        # 切在中间，看起来像画错了。
        host = ScrollFrame(
            self, palette, self._scale,
            background=palette.bg,
            height=max(320.0, self._layout.height / self._scale * 0.8),
        )
        host.pack(fill="both", expand=True)
        self._host: ScrollFrame = host
        body = host.inner
        self._body = body

        self._build_conclusion(body)
        self._build_trio(body)
        self._build_info(body)
        self._build_results(body)
        self._build_log(body)

    def _build_conclusion(self, body: tk.Frame) -> None:
        palette = self.palette
        pad = self._layout.pad
        card = Card(body, palette, self._scale, accent=palette.text_faint)
        card.pack(fill="x", padx=pad, pady=(px(self._scale, 12), px(self._scale, 8)))
        self._card_conclusion = card

        inner = tk.Frame(card.body, bg=palette.surface)
        inner.pack(fill="both", expand=True, padx=px(self._scale, 14), pady=px(self._scale, 10))
        inner.columnconfigure(0, weight=1)

        text_col = tk.Frame(inner, bg=palette.surface)
        text_col.grid(row=0, column=0, sticky="we")
        self.lbl_type = tk.Label(
            text_col,
            text="尚未检测",
            font=font(Typography.display, bold=True),
            fg=palette.text,
            bg=palette.surface,
            anchor="w",
        )
        self.lbl_type.pack(fill="x")
        self.lbl_sub = tk.Label(
            text_col,
            text="点击「开始检测」",
            font=font(Typography.note),
            fg=palette.text_dim,
            bg=palette.surface,
            anchor="w",
            justify="left",
        )
        self.lbl_sub.pack(fill="x", pady=(px(self._scale, 3), 0))

        thumb_px = px(self._scale, 62)
        self.thumb = MatrixView(
            inner, palette, self._scale, thumb=True, height=62, surface=palette.surface
        )
        self.thumb.configure(width=thumb_px)
        self.thumb.grid(row=0, column=1, sticky="e", padx=(px(self._scale, 12), 0))

    def _build_trio(self, body: tk.Frame) -> None:
        palette = self.palette
        pad = self._layout.pad
        trio = tk.Frame(body, bg=palette.bg)
        trio.pack(fill="x", padx=pad)
        for index in range(3):
            trio.columnconfigure(index, weight=1, uniform="trio")
        self._mini: dict[str, tk.Label] = {}
        specs = (
            ("mapping", "映射行为 Mapping"),
            ("filtering", "过滤行为 Filtering"),
            ("gateway", "端口映射 Gateway"),
        )
        for index, (key, label) in enumerate(specs):
            card = Card(trio, palette, self._scale)
            card.grid(
                row=0, column=index, sticky="nsew",
                padx=(0 if index == 0 else px(self._scale, 8), 0),
            )
            tk.Label(
                card.body,
                text=label,
                font=font(Typography.label),
                fg=palette.text_faint,
                bg=palette.surface,
                anchor="w",
            ).pack(fill="x", padx=px(self._scale, 12), pady=(px(self._scale, 8), 0))
            value = tk.Label(
                card.body,
                text="—",
                font=font(Typography.value, bold=True),
                fg=palette.text,
                bg=palette.surface,
                anchor="w",
            )
            value.pack(fill="x", padx=px(self._scale, 12), pady=(px(self._scale, 2), px(self._scale, 9)))
            self._mini[key] = value

    def _build_info(self, body: tk.Frame) -> None:
        palette = self.palette
        pad = self._layout.pad
        card = Card(body, palette, self._scale)
        card.pack(fill="x", padx=pad, pady=px(self._scale, 8))
        self._info = InfoGrid(card.body, palette, self._scale, columns=2)
        self._info.pack(fill="x", padx=px(self._scale, 12), pady=px(self._scale, 8))
        for key, label in (
            ("local", "本机地址"),
            ("public", "公网映射"),
            ("gw", "默认网关"),
            ("server", "主 STUN 服务器"),
        ):
            self._info.add(key, label)

    def _build_results(self, body: tk.Frame) -> None:
        palette = self.palette
        pad = self._layout.pad
        card = Card(body, palette, self._scale)
        card.pack(fill="x", padx=pad, pady=(0, px(self._scale, 8)))
        head = tk.Frame(card.body, bg=palette.surface)
        head.pack(fill="x", padx=px(self._scale, 12), pady=(px(self._scale, 8), 0))
        tk.Label(
            head,
            text="结果可视化",
            font=font(Typography.label, bold=True),
            fg=palette.text_faint,
            bg=palette.surface,
        ).pack(side="left")
        self.result_view = ResultView(card.body, palette, self._scale, surface=palette.surface)
        self.result_view.pack(
            fill="x",
            padx=px(self._scale, 12), pady=(px(self._scale, 4), px(self._scale, 10)),
        )

    def _build_log(self, body: tk.Frame) -> None:
        palette = self.palette
        pad = self._layout.pad
        card = Card(body, palette, self._scale)
        card.pack(fill="x", padx=pad, pady=(0, px(self._scale, 12)))
        head = tk.Frame(card.body, bg=palette.surface)
        head.pack(fill="x", padx=px(self._scale, 12), pady=px(self._scale, 7))
        tk.Label(
            head,
            text="检测日志",
            font=font(Typography.label),
            fg=palette.text_faint,
            bg=palette.surface,
        ).pack(side="left")
        self.btn_log = GhostButton(
            head, palette, self._scale,
            "折叠" if self._log_open else "展开",
            self._toggle_log, pad_x=9.0, pad_y=3.0,
        )
        self.btn_log.pack(side="right")

        self._log_wrap = tk.Frame(card.body, bg=palette.surface)
        line_h = max(1, font(Typography.mono, family="Consolas").metrics("linespace"))
        lines = max(3, int(px(self._scale, 44) / line_h))
        self.log = tk.Text(
            self._log_wrap,
            height=lines,
            bg=palette.surface,
            fg=palette.text_dim,
            font=font(Typography.mono, family="Consolas"),
            relief="flat",
            wrap="word",
            state="disabled",
            borderwidth=0,
            highlightthickness=0,
            insertbackground=palette.text,
            selectbackground=palette.accent,
            padx=px(self._scale, 3),
            pady=px(self._scale, 2),
        )
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar = tk.Scrollbar(
            self._log_wrap,
            command=self.log.yview,
            width=px(self._scale, 9),
            bg=palette.surface_alt,
            troughcolor=palette.surface,
            activebackground=palette.border,
            relief="flat",
            bd=0,
            highlightthickness=0,
        )
        scrollbar.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=scrollbar.set)
        if self._log_open:
            self._log_wrap.pack(
                fill="both", expand=True,
                padx=px(self._scale, 10), pady=(0, px(self._scale, 9)),
            )

    def _toggle_log(self) -> None:
        self._log_open = not self._log_open
        if self._log_open:
            self._log_wrap.pack(
                fill="both", expand=True,
                padx=px(self._scale, 10), pady=(0, px(self._scale, 9)),
            )
        else:
            self._log_wrap.pack_forget()
        if hasattr(self, "btn_log"):
            self.btn_log.configure(text="折叠" if self._log_open else "展开")

    # ------------------------------------------------------------------ 主题

    def _theme_text(self) -> str:
        return {
            THEME_AUTO: "主题：自动",
            THEME_LIGHT: "主题：浅色",
            THEME_DARK: "主题：深色",
        }[self.theme_mode]

    def _cycle_theme(self) -> None:
        order = [THEME_AUTO, THEME_LIGHT, THEME_DARK]
        self.theme_mode = order[(order.index(self.theme_mode) + 1) % 3]
        self.config_data.theme = self.theme_mode
        self._save_config()
        self._apply_theme()

    def _apply_theme(self) -> None:
        """换主题：重建控件并**恢复显示**，不重放日志。

        [P1-4] v1.0 在重建后调 ``_render()``，而 ``_render()`` 里有一句
        ``_append_log("— 结论 —")``，于是每切一次主题，日志里就多一段结论
        （实测 1 → 2 → 3 → 4）。现在重建后只调 :meth:`_render_display`，
        它只改标签与图形，一个字都不往日志里写。

        [P1-5] 检测进行中切主题，按钮必须保持 ``disabled`` + "检测中…"，
        进度条保持当前值——否则按钮变回可点、进度归零，用户会以为检测停了。
        """
        self.palette = get_palette(self.theme_mode)
        log_text = "\n".join(self._log_lines)
        running = self._running
        progress = self._progress_ratio
        status = self._status_text
        log_open = self._log_open

        for child in self.winfo_children():
            child.destroy()
        self._log_open = log_open
        self._build()
        self._set_log(log_text)

        if self._result is not None:
            self._render_display()
        elif running:
            self._stale_result()
        self._progress(progress)
        self._status(status)
        if running:
            self.btn_start.configure(text="检测中…", state="disabled")
        else:
            self.btn_start.configure(text="开始检测", state="normal")

    # ------------------------------------------------------------------ 持久化

    def _save_config(self, notify_when_failed: bool = True) -> bool:
        """保存配置；失败时（只读目录/磁盘满/杀软锁文件）明确告诉用户。"""
        ok = self.config_data.save()
        if not ok and notify_when_failed:
            messagebox.showwarning(
                "配置未能保存",
                "设置无法写入磁盘（目录只读、磁盘已满或被安全软件锁定）。\n"
                "本次修改只在当前会话生效。",
                parent=self,
            )
        return ok

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
        if text:
            self.log.insert("1.0", text if text.endswith("\n") else text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _status(self, text: str) -> None:
        self._status_text = str(text)
        self.lbl_status.configure(text=self._status_text)

    def _progress(self, ratio: float) -> None:
        ratio = max(0.0, min(1.0, float(ratio)))
        self._progress_ratio = ratio
        self._progress_fill.place_configure(relwidth=ratio)

    # ------------------------------------------------------------------ 检测流程

    def _post(self, item) -> None:
        self._q.put(item)

    def _drain(self) -> None:
        """消费后台线程的消息。

        [P1-6] ``self.after(...)`` 必须在 ``finally`` 里：v1.0 只要有一条消息
        处理时抛异常，这一行就永远执行不到，队列从此无人消费——界面卡在
        "检测中…"，而且没有任何报错（异常被 Tk 的回调兜底吞掉）。
        现在单条消息失败只记一行日志，消费循环照常续命。
        """
        try:
            while True:
                try:
                    kind, payload = self._q.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle(kind, payload)
                except Exception as exc:  # noqa: BLE001 - 界面更新失败不能停循环
                    self._append_log(f"界面更新异常：{type(exc).__name__}: {exc}")
        finally:
            if not self._closing:
                try:
                    self._drain_id = self.after(120, self._drain)
                except tk.TclError:  # 窗口正在销毁
                    pass

    def _handle(self, kind: str, payload) -> None:
        if kind == "log":
            self._append_log(payload)
        elif kind == "status":
            self._status(payload)
        elif kind == "progress":
            self._progress(payload)
        elif kind == "done":
            nat, gw, probe = payload
            self._result, self._gateway, self._probe = nat, gw, probe
            self._finish()
            self._render()
        elif kind == "error":
            self._finish()
            self._status("检测失败")
            messagebox.showerror("检测失败", payload, parent=self)

    def _finish(self) -> None:
        """一轮检测结束：恢复按钮、补齐进度，并应用挂起的 DPI 变更。"""
        self._running = False
        if hasattr(self, "btn_start"):
            self.btn_start.configure(text="开始检测", state="normal")
        self._progress(1.0)
        if self._dpi_pending is not None:
            pending = self._dpi_pending
            self._dpi_pending = None
            self._reflow(pending)

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
        if self._host is not None:
            self._host.scroll_to_top()
        threading.Thread(target=self._worker, daemon=True).start()

    def _stale_result(self) -> None:
        """进入检测中：清掉上一轮的展示内容，避免新旧结论混在一起。"""
        palette = self.palette
        self.lbl_type.configure(text="检测中…", fg=palette.text_dim)
        self.lbl_sub.configure(text="正在依次执行 STUN 探测与 NAT 行为分析")
        self._card_conclusion.set_accent(palette.text_faint)
        for value in self._mini.values():
            value.configure(text="…", fg=palette.text_faint)
        self._info.set_many({key: "…" for key in self._info.values()})
        self.thumb.set_matrix(None)
        self.result_view.render(None, None)

    def _worker(self) -> None:
        try:
            config = self.config_data
            servers = config.all_servers()
            self._post(("log", f"共 {len(servers)} 个 STUN 服务器，开始并发探测…"))

            def progress(done, total, _result):
                self._post(("progress", 0.05 + 0.45 * (done / max(1, total))))
                if done == total:
                    self._post(("status", "服务器探测完成，开始 NAT 检测…"))

            results = probe_servers(
                servers, timeout=1.2, retry_timeout=2.0, workers=32, progress=progress
            )
            summary = ProbeSummary(results)
            self._post(
                ("log",
                 f"探测结果：优选 {len(summary.preferred)} / 备选 {len(summary.backup)} / "
                 f"不可用 {len(summary.unavailable)}")
            )
            limit = 40 if config.log_level == "DEBUG" else 8
            for result in summary.usable[:limit]:
                self._post(
                    ("log", f"  {result.tier_label}  {result.server.label}  {result.rtt_ms:.0f} ms")
                )
            if not summary.usable:
                self._post(("error", "所有 STUN 服务器均不可用。请检查网络，或改用自定义服务器。"))
                return

            self._post(("progress", 0.55))
            self._post(("status", "正在分析 NAT 行为…"))
            detector = NatDetector(timeout=config.timeout, log=lambda m: self._post(("log", m)))
            nat = detector.run(results)
            if not nat.ok:
                self._post(("log", f"检测未完成：{nat.error}"))

            self._post(("progress", 0.85))
            gateway: GatewayReport | None = None
            if config.gateway_enabled:
                self._post(("status", "正在检测网关端口映射能力…"))
                try:
                    gateway = probe_gateway(nat.local_ip)
                    self._post(
                        ("log",
                         f"网关 {gateway.gateway_ip or '未知'}："
                         + "，".join(f"{c.name} {c.state_label}" for c in gateway.capabilities))
                    )
                except Exception as exc:  # noqa: BLE001
                    # [P0-3 调用处兜底] 网关探测只是附加信息：它失败不能让已经
                    # 算完的 NAT 结论被整体丢弃（v1.0 就是这样，用户看到的是
                    # "检测过程出现异常"而不是"NAT 结论 + 网关未知"）。
                    gateway = None
                    self._post(
                        ("log",
                         f"网关探测失败（不影响 NAT 结论）：{type(exc).__name__}: {exc}")
                    )
            else:
                self._post(("log", "已禁用网关探测（gateway_enabled=false）"))

            self._post(("done", (nat, gateway, summary)))
        except Exception as exc:  # noqa: BLE001 - 兜底：任何异常都不允许把窗口带崩
            self._post(("log", f"异常：{type(exc).__name__}: {exc}"))
            self._post(("error", f"检测过程出现异常：\n{type(exc).__name__}: {exc}"))

    # ------------------------------------------------------------------ 渲染

    def _render(self) -> None:
        """一轮检测完成：先更新显示，再往日志里写结论。"""
        self._render_display()
        self._log_conclusion()

    def _render_display(self) -> None:
        """只更新界面，不写日志（主题切换也走这里，见 ``_apply_theme``）。"""
        nat = self._result
        if nat is None:
            return
        palette = self.palette
        title, subtitle, _tone = viz.verdict(nat)
        # 类型色全项目唯一实现来自 theme.type_color（v1.0 在 app.py 与
        # report.py 里各有一份副本，两份都改才一致——现在只有一份）
        color = type_color(palette, nat.classic_type)

        self.lbl_type.configure(text=title, fg=color)
        mapping = nat.mapping_label.split("（")[0]
        filtering = nat.filtering_label.split("（")[0]
        if nat.classic_en:
            detail = f"{subtitle} · 映射 {mapping} · 过滤 {filtering}"
        else:
            detail = f"{subtitle} · 映射 {mapping}"
        self.lbl_sub.configure(text=detail)
        self._card_conclusion.set_accent(color)
        self.thumb.set_matrix(viz.build_matrix(nat))

        self._mini["mapping"].configure(text=mapping or "—", fg=palette.text)
        self._mini["filtering"].configure(text=filtering or "—", fg=palette.text)
        gateway = self._gateway
        if gateway is None:
            self._mini["gateway"].configure(text="未检测", fg=palette.text_dim)
        else:
            names = [c.name.replace("UPnP-IGD", "UPnP") for c in gateway.capabilities if c.available]
            if names:
                self._mini["gateway"].configure(text=" / ".join(names), fg=palette.ok)
            elif any(c.state == "limited" for c in gateway.capabilities):
                self._mini["gateway"].configure(text="存在但受限", fg=palette.warn)
            else:
                self._mini["gateway"].configure(text="未发现", fg=palette.text_dim)

        self._info.set_many(
            {
                "local": f"{nat.local_ip}:{nat.local_port}",
                "public": f"{nat.public_ip}:{nat.public_port}",
                "gw": (gateway.gateway_ip if gateway and gateway.gateway_ip else "—"),
                "server": nat.primary_server or "—",
            }
        )
        self.result_view.render(nat, gateway)
        self._status(f"检测完成 · 耗时 {nat.duration_ms / 1000:.1f}s")

    def _log_conclusion(self) -> None:
        nat = self._result
        if nat is None:
            return
        self._append_log("")
        self._append_log("— 结论 —")
        self._append_log(nat.classic_label)
        self._append_log(nat.summary)
        for warning in nat.warnings:
            self._append_log(f"提示：{warning}")

    # ------------------------------------------------------------------ 导出

    def _export(self, kind: str) -> None:
        if not self._result:
            messagebox.showinfo("提示", "请先执行一次检测。", parent=self)
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
            filetypes=[
                ("文本文件" if ext == "txt" else "JPEG 图片", f"*.{ext}"),
                ("所有文件", "*.*"),
            ],
        )
        if not path:
            return
        ctx = ReportContext(nat=self._result, gateway=self._gateway, probe=self._probe)
        try:
            if ext == "txt":
                save_text(ctx, path)
            else:
                save_image(ctx, path, self.palette)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导出失败", f"{type(exc).__name__}: {exc}", parent=self)
            return
        self.config_data.last_dir = os.path.dirname(path)
        self._save_config(notify_when_failed=False)
        self._status(f"已导出 {os.path.basename(path)}")

    # ------------------------------------------------------------------ 服务器管理

    def _open_servers(self) -> None:
        ServerDialog(
            self, self.config_data, self.palette, self._scale,
            on_change=self._on_servers_changed,
        )

    def _on_servers_changed(self) -> None:
        self._status("服务器列表已更新，重新检测生效")

    # ------------------------------------------------------------------ 首次运行 / DPI

    def _maybe_first_run(self) -> None:
        if self.config_data.first_run_ack:
            return
        FirstRunDialog(self, self.palette, self._scale, on_ack=self._ack_first_run)

    def _ack_first_run(self) -> None:
        self.config_data.first_run_ack = True
        self._save_config(notify_when_failed=False)

    def _poll_dpi(self) -> None:
        """轮询 DPI：变化超过 5% 才重排（拖动窗口跨屏时系统会改 DPI）。"""
        if self._closing:
            return
        try:
            scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        except tk.TclError:
            return
        if self._scale > 0 and abs(scale - self._scale) / self._scale > 0.05:
            if self._running:
                self._dpi_pending = scale  # 检测中只记标记，结束后再生效
            else:
                self._reflow(scale)
        try:
            self._dpi_id = self.after(1500, self._poll_dpi)
        except tk.TclError:
            pass

    def _reflow(self, scale: float) -> None:
        """按新的缩放比例重排整个界面。"""
        self._scale = float(scale)
        self._dpi_pending = None
        try:
            self.tk.call("tk", "scaling", self.winfo_fpixels("1i") / 72.0)
        except Exception:
            pass
        self._work = metrics.work_area()
        self._layout = metrics.resolve(self._work, self._scale)
        self.minsize(self._layout.min_w, self._layout.min_h)
        try:
            root_x, root_y = int(self.winfo_rootx()), int(self.winfo_rooty())
        except tk.TclError:
            root_x, root_y = 0, 0
        try:
            self.geometry(
                f"{self._layout.width}x{self._layout.height}{root_x:+d}{root_y:+d}"
            )
        except tk.TclError:
            pass
        self._apply_theme()

    def _cancel_timers(self) -> None:
        """取消所有挂起的 ``after``（销毁前必须做，否则留一串 Tcl 报错）。"""
        handles = [self._drain_id, self._dpi_id, *getattr(self, "_boot_ids", [])]
        for handle in handles:
            if not handle:
                continue
            try:
                self.after_cancel(handle)
            except (tk.TclError, ValueError):
                pass
        self._drain_id = ""
        self._dpi_id = ""
        self._boot_ids = []

    def _on_close(self) -> None:
        self._closing = True
        self._cancel_timers()
        try:
            geometry = self.geometry()
            clamped = metrics.clamp_geometry(geometry, self._work)
            if clamped:
                self.config_data.window_geometry = clamped
        except tk.TclError:
            pass
        self._save_config()
        try:
            self.destroy()
        except tk.TclError:
            pass


def run() -> None:
    """启动主窗口。"""
    app = App()
    app.mainloop()


__all__ = ["App", "enable_dpi_awareness", "resource_path", "run"]
