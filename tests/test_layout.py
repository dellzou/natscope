# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""布局度量（``natscope.gui.metrics``）的纯逻辑测试。

锁的是 v1.0 那个**算术上必然发生**的 bug：``minsize(860*scale, 660*scale)``
在 1366×768 @125% 下要 825px 高，而工作区只有 733px，于是窗口底栏（含
「开始检测」按钮）被任务栏永久吞掉，用户**任何**拖拽都露不出来。

所以这里的第一组断言不是"尺寸好看"，而是硬不变式：

    min_w <= work_w - 32   且   min_h <= work_h - 32

20 组「分辨率 × 缩放」矩阵逐一验证；1366×768 @125% 额外要求降级到 ``compact``。

另外两条边界也在这里锁：

* ``clamp_geometry`` 必须拒绝**非法字符串**与**出界矩形**（副屏拔掉后窗口
  启动在看不见的地方，用户会以为程序没启动）；
* ``import natscope.gui.metrics`` 不得把 tkinter 拉进来（纯逻辑层）。

零 GUI：本文件不导入 tkinter，也不建任何窗口。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from natscope.gui import metrics

#: 待覆盖的显示器物理分辨率
DISPLAYS: tuple[tuple[int, int], ...] = (
    (1366, 768),
    (1600, 900),
    (1920, 1080),
    (2560, 1440),
    (3840, 2160),
)

#: 待覆盖的缩放比例（Windows 的 100% / 125% / 150% / 200%）
SCALES: tuple[float, ...] = (1.0, 1.25, 1.5, 2.0)

#: 任务栏高度（逻辑像素）：物理高度 = 40 × 缩放
TASKBAR_LOGICAL = 40

MODES = {metrics.MODE_COMFORTABLE, metrics.MODE_COMPACT, metrics.MODE_SCROLL}


def work_for(display: tuple[int, int], scale: float) -> tuple[int, int, int, int]:
    """某分辨率 + 缩放下主显示器的可用工作区（排除任务栏）。

    任务栏随缩放变高（Windows 的 40 逻辑像素），因此 125% 下是 50 物理像素。
    """
    width, height = display
    taskbar = int(round(TASKBAR_LOGICAL * scale))
    return (0, 0, width, max(1, height - taskbar))


class TestResolveInvariants(unittest.TestCase):
    """5 分辨率 × 4 缩放 = 20 组，逐组验证硬不变式。"""

    def test_twenty_combinations(self):
        seen_modes: dict[str, int] = {}
        for display in DISPLAYS:
            for scale in SCALES:
                label = f"{display[0]}×{display[1]} @{int(scale * 100)}%"
                work = work_for(display, scale)
                with self.subTest(显示=label):
                    layout = metrics.resolve(work, scale)

                    # ---- 硬不变式：最小尺寸绝不越过工作区 ----
                    self.assertLessEqual(
                        layout.min_w, work[2] - 32,
                        f"{label}: min_w={layout.min_w} 超过 work_w-32={work[2] - 32}，"
                        "窗口最小宽度大于可用宽度，右侧内容永远看不见",
                    )
                    self.assertLessEqual(
                        layout.min_h, work[3] - 32,
                        f"{label}: min_h={layout.min_h} 超过 work_h-32={work[3] - 32}，"
                        "窗口最小高度大于可用高度 —— 这正是 v1.0 底栏被任务栏吞掉的原因"
                        f"（工作区 {work[2]}×{work[3]}，min_h={layout.min_h}）",
                    )
                    # ---- 初始尺寸同样不能越界 ----
                    self.assertLessEqual(layout.width, work[2] - 32, label)
                    self.assertLessEqual(layout.height, work[3] - 32, label)
                    self.assertGreaterEqual(layout.width, layout.min_w, label)
                    self.assertGreaterEqual(layout.height, layout.min_h, label)

                    self.assertIn(layout.mode, MODES, label)
                    self.assertGreater(layout.width, 0, label)
                    self.assertGreater(layout.height, 0, label)
                    self.assertGreaterEqual(layout.pad, metrics.MIN_PAD, label)
                    self.assertAlmostEqual(layout.scale, scale, places=6, msg=label)

                    seen_modes[layout.mode] = seen_modes.get(layout.mode, 0) + 1

        # 三档都必须被覆盖到，否则这组矩阵没有真正"遍历降级路径"
        self.assertEqual(
            set(seen_modes), MODES,
            f"20 组组合没有覆盖完整的三级降级，实际只出现 {sorted(seen_modes)}",
        )

    def test_1366x768_at_125_percent_is_compact(self):
        """QA 实测的那一档：必须降级为 compact，且高度不超工作区。"""
        work = work_for((1366, 768), 1.25)
        layout = metrics.resolve(work, 1.25)
        detail = (
            f"工作区 {work[2]}×{work[3]}，得到 mode={layout.mode} "
            f"width={layout.width} height={layout.height} "
            f"min=({layout.min_w},{layout.min_h}) pad={layout.pad}"
        )
        self.assertEqual(
            layout.mode, metrics.MODE_COMPACT,
            "1366×768 @125% 必须走紧凑档（980×760 的舒适档在这里放不下）。" + detail,
        )
        self.assertLessEqual(layout.height, work[3], detail)
        self.assertLessEqual(layout.height, work[3] - 32, detail)
        self.assertLessEqual(layout.min_h, work[3] - 32, detail)
        self.assertGreaterEqual(layout.min_h, 400, detail)

    def test_4k_at_200_percent_is_comfortable(self):
        """3840×2160 @200% 的等效逻辑尺寸充足，应回到舒适档。"""
        work = work_for((3840, 2160), 2.0)
        layout = metrics.resolve(work, 2.0)
        self.assertEqual(layout.mode, metrics.MODE_COMFORTABLE)
        self.assertEqual(layout.width, int(round(metrics.DESIGN[0] * 2.0)))
        self.assertEqual(layout.height, int(round(metrics.DESIGN[1] * 2.0)))

    def test_compact_is_tighter_than_comfortable(self):
        comfortable = metrics.resolve(work_for((3840, 2160), 2.0), 2.0)
        compact = metrics.resolve(work_for((1366, 768), 1.25), 1.25)
        self.assertGreater(comfortable.pad, compact.pad, "紧凑档的内边距必须更小")
        self.assertTrue(compact.compact, "紧凑档应默认折叠日志")
        self.assertFalse(comfortable.compact, "舒适档不应折叠日志")

    def test_scroll_mode_when_even_compact_does_not_fit(self):
        """1366×768 @200%：连紧凑下限都放不下，只能整页滚动。"""
        work = work_for((1366, 768), 2.0)
        layout = metrics.resolve(work, 2.0)
        self.assertEqual(layout.mode, metrics.MODE_SCROLL)
        self.assertTrue(layout.scrolls)
        self.assertLessEqual(layout.min_h, work[3] - 32)

    def test_absurdly_small_work_area_still_respects_invariant(self):
        """极端小工作区（400×300）不许让 min 越界，也不许抛异常。"""
        layout = metrics.resolve((0, 0, 400, 300), 1.0)
        self.assertLessEqual(layout.min_w, 400 - 32)
        self.assertLessEqual(layout.min_h, 300 - 32)
        self.assertGreater(layout.width, 0)
        self.assertEqual(layout.mode, metrics.MODE_SCROLL)

    def test_bad_scale_is_sanitised(self):
        for bad in (0, -1.0, float("nan"), None, "1.5"):
            with self.subTest(scale=bad):
                layout = metrics.resolve(work_for((1920, 1080), 1.0), bad)  # type: ignore[arg-type]
                self.assertGreaterEqual(layout.scale, 1.0)
                self.assertLessEqual(layout.scale, 3.0)

    def test_work_area_is_collected(self):
        """``work_area()`` 在真实平台上必须返回 4 个正数（不得抛异常）。"""
        area = metrics.work_area()
        self.assertEqual(len(area), 4)
        self.assertGreater(area[2], 0)
        self.assertGreater(area[3], 0)


class TestClampGeometry(unittest.TestCase):
    """几何串校验：非法返回 None，合法顺手修复。"""

    WORK = (0, 0, 1920, 1040)

    def test_valid_geometry_is_kept(self):
        self.assertEqual(metrics.clamp_geometry("980x720+120+80", self.WORK), "980x720+120+80")

    def test_size_only_geometry_is_accepted(self):
        self.assertEqual(metrics.clamp_geometry("980x720", self.WORK), "980x720")

    def test_garbage_returns_none(self):
        for bad in ("", "abc", "980", "980x", "x720", "980x720+", "980x720+10",
                    "980x720+a+b", "#$%", None, 980, "980x720+100+50+7"):
            with self.subTest(geo=bad):
                self.assertIsNone(metrics.clamp_geometry(bad, self.WORK))

    def test_too_small_or_too_large_returns_none(self):
        for bad in ("100x100+0+0", "9x720+0+0", "980x100+0+0", "99999x720+0+0"):
            with self.subTest(geo=bad):
                self.assertIsNone(metrics.clamp_geometry(bad, self.WORK))

    def test_offscreen_rectangle_returns_none(self):
        # 窗口整个在屏幕右下角之外：启动后用户看不到它
        for bad in ("980x720+5000+5000", "980x720+-3000+100", "980x720+100-2000"):
            with self.subTest(geo=bad):
                self.assertIsNone(metrics.clamp_geometry(bad, self.WORK))

    def test_partially_visible_is_repaired_into_view(self):
        """露出足够（>=40%）时接受，并把窗口平移回完整可见的位置。"""
        # 980x720 @+1000+500：与工作区相交 920x540 ≈ 70%，但右下角出界
        repaired = metrics.clamp_geometry("980x720+1000+500", self.WORK)
        self.assertIsNotNone(repaired, "相交约 70%，应当被接受并修复")
        self.assertEqual(repaired, "980x720+940+320")

    def test_threshold_is_forty_percent(self):
        """40% 是阈值：41% 接受并修复，39% 丢弃（team-lead 定的口径）。"""
        # 980x720 在 y=745 时可见 980x295 ≈ 41%
        self.assertIsNotNone(metrics.clamp_geometry("980x720+100+745", self.WORK))
        # y=760 时可见 980x280 ≈ 39%
        self.assertIsNone(metrics.clamp_geometry("980x720+100+760", self.WORK))

    def test_window_straddling_two_monitors_is_accepted(self):
        """跨屏摆放（一半在这屏、一半在那屏）是正常用法，必须能用。

        接受之后可能被平移到**某一块**屏内完全可见——这比"丢回默认居中"好：
        用户至少还能看到它、拖回来。
        """
        rects = [(0, 0, 1920, 1040), (1920, 0, 1920, 1040)]
        repaired = metrics.clamp_geometry("980x720+1430+100", rects)
        self.assertIsNotNone(repaired, "跨屏摆放不应被判为不可用")
        match = re.match(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)$", repaired or "")
        self.assertIsNotNone(match, repaired)
        w, h, x, y = (int(g) for g in match.groups())  # type: ignore[union-attr]
        inside = any(
            rx <= x and x + w <= rx + rw and ry <= y and y + h <= ry + rh
            for rx, ry, rw, rh in rects
        )
        self.assertTrue(inside, f"修复结果 {repaired} 必须完整落在某块屏内")

    def test_window_split_across_three_monitors(self):
        """极端跨屏（各占 30%，合计 60%）：按并集面积算，仍应接受。"""
        three = [(0, 0, 1920, 1040), (1920, 0, 1920, 1040), (3840, 0, 1920, 1040)]
        self.assertIsNotNone(metrics.clamp_geometry("4000x720+1500+100", three))

    def test_barely_visible_is_rejected(self):
        """只露出约 20%：视为不合法（标题栏可能在屏幕外，拖不回来）。"""
        self.assertIsNone(metrics.clamp_geometry("980x720+1500+700", self.WORK))

    def test_oversized_window_is_shrunk_to_work_area(self):
        self.assertIsNone(metrics.clamp_geometry("4000x3000+0+0", 0))  # work 非法
        # 比屏幕还大但左上角在屏内：可用，缩到工作区大小
        self.assertEqual(
            metrics.clamp_geometry("4000x3000+0+0", self.WORK), "1920x1040+0+0"
        )

    def test_second_monitor_is_honoured(self):
        rects = [(0, 0, 1920, 1040), (1920, 0, 1920, 1040)]
        self.assertEqual(
            metrics.clamp_geometry("1200x800+2400+100", rects), "1200x800+2400+100"
        )

    def test_centered_output_round_trips(self):
        work = self.WORK
        layout = metrics.resolve(work, 1.0)
        geometry = metrics.centered(layout, work)
        self.assertEqual(metrics.clamp_geometry(geometry, work), geometry)


class TestImportIsolation(unittest.TestCase):
    """``metrics`` 是纯逻辑层，导入期绝不能碰 tkinter。"""

    def test_import_does_not_pull_tkinter(self):
        code = (
            "import sys\n"
            "from natscope.gui import metrics\n"
            "print('tkinter' in sys.modules)\n"
            "print(metrics.resolve((0, 0, 1366, 733), 1.25).mode)\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
        out = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        ).stdout.splitlines()
        self.assertEqual(out[0].strip(), "False", "import natscope.gui.metrics 不应加载 tkinter")
        self.assertEqual(out[1].strip(), "compact")


if __name__ == "__main__":
    unittest.main(verbosity=2)
