# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""JPG 报告版式测试：两遍布局、不裁剪、分页、超采样、内容不丢。

这里守的是 v1.1 修掉的那批真实缺陷：

* 固定 1500px 画布 + ``crop(min(y, H))`` 会静默吃掉内容 —— 现在画布高度必须
  等于规划高度，且最坏用例明显超过 1500；
* ``notes[:4]`` 会把第 5 条及之后的警告丢掉 —— 现在全部渲染；
* 提示只取 ``nat.warnings`` 而不合并 ``gateway.notes`` —— 现在统一走
  :func:`natscope.report.collect_notes`；
* 渲染代码里散落字面量 hex 导致换主题漏改 —— 现在只允许从 Palette 取色。

运行：python -m unittest discover -s tests -v
"""

import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from natscope import report, viz
from natscope.gateway import Capability, GatewayReport
from natscope.nat import (
    CLASSIC_BLOCKED,
    CLASSIC_CONE_UNKNOWN,
    CLASSIC_FULL_CONE,
    CLASSIC_OPEN,
    CLASSIC_PORT_RESTRICTED,
    CLASSIC_RESTRICTED,
    CLASSIC_SYMMETRIC,
    CLASSIC_UNKNOWN,
    MappingSample,
    NatResult,
    TestStep,
)
from natscope.servers import ProbeResult, ProbeSummary, StunServer, TIER_BACKUP, TIER_UNAVAILABLE
from natscope.theme import DARK, LIGHT

LONG_WARNING = (
    "当前可用服务器均未响应 CHANGE-REQUEST，过滤行为与传统四类型无法直接测量，"
    "建议添加支持 RFC5780 的服务器后重试；否则本报告只能给出锥型的粗分类。"
)
def build_gateway() -> GatewayReport:
    gw = GatewayReport(gateway_ip="192.168.1.1")
    gw.upnp = Capability(
        "UPnP-IGD",
        available=True,
        state="ok",
        external_ip="203.0.113.7",
        detail="GetExternalIPAddress 成功，外网地址 203.0.113.7",
    )
    gw.natpmp = Capability("NAT-PMP", state="absent", detail="网关 5351 端口无响应")
    gw.pcp = Capability("PCP", state="limited", detail="SOAP 调用被拒绝（HTTP 500）")
    gw.notes = ["网关策略可能限制了 PCP，建议在路由器后台手动放行端口"]
    return gw


def build_probe() -> ProbeSummary:
    results = []
    for i in range(18):
        ok = i % 3 != 0
        res = ProbeResult(
            server=StunServer(f"stun{i}.example.com", 3478),
            ok=ok,
            rtt_ms=14.0 + i,
            ip=f"198.51.100.{i}",
            tier=TIER_BACKUP if ok else TIER_UNAVAILABLE,
        )
        results.append(res)
    return ProbeSummary(results)


def build_worst_nat() -> NatResult:
    """最坏用例：4 条长警告 + 长结论 + 12 个步骤 + 8 个样本。

    ``cone_unknown`` 的结论文案是全部类型里最长的（要把「为什么无法细分」
    讲清楚），矩阵的说明行也最长，因此它是最占高度的真实类型。
    """
    nat = NatResult(
        ok=True,
        local_ip="192.168.1.23",
        local_port=52143,
        public_ip="203.0.113.7",
        public_port=52143,
        classic_type=CLASSIC_CONE_UNKNOWN,
        mapping_behavior="eim",
        filtering_behavior="unknown",
        change_request_supported=False,
        rfc5780_server="stun.voip.blackberry.com:3478",
        primary_server="stun.l.google.com:19302",
        duration_ms=8421.5,
    )
    nat.warnings = [
        LONG_WARNING,
        "检测到多个公网出口 IP（203.0.113.7、198.51.100.9），本机可能处于多 WAN / "
        "负载均衡环境，映射端口的一致性以端口为准。",
        "不同 IP 的可用 STUN 服务器不足 2 个，映射行为结论可能不准确。",
        "CHANGE-REQUEST 测试未能得出结论，过滤行为标记为不可测。",
        "第五条警告：v1.0 的 notes[:4] 一定会把这条静默丢掉。",
    ]
    nat.samples = [
        MappingSample(
            f"203.0.113.{i}",
            3478 + i,
            "203.0.113.7",
            52143 + i * 7,
            f"stun{i}.example.com:3478",
        )
        for i in range(1, 9)
    ]
    names = [
        "Test I：获取映射地址",
        "Test II：Change IP+Port",
        "Test III：Change Port",
        "多目标映射采样",
    ]
    statuses = ["ok", "fail", "skip", "pending"]
    for i in range(12):
        nat.steps.append(
            TestStep(
                f"{names[i % len(names)]} #{i + 1}（这是一个很长的测试项名称，用来验证像素级省略号）",
                statuses[i % len(statuses)],
                "203.0.113.7:52143（本地 192.168.1.23:52143，RTT 42ms）" if i % 2 else "",
            )
        )
    return nat


def build_worst_ctx() -> report.ReportContext:
    return report.ReportContext(
        nat=build_worst_nat(),
        gateway=build_gateway(),
        probe=build_probe(),
        generated_at="2026-01-02 03:04:05",
        os_text="Windows 11 (build 22631)",
    )


class TestNoCrop(unittest.TestCase):
    """核心回归：画布高度 == 规划高度，且内容确实超过旧画布。"""

    def test_canvas_height_equals_plan(self):
        ctx = build_worst_ctx()
        plan = report.plan_report(ctx, LIGHT)
        images = report.render_report_pages(ctx, LIGHT)
        self.assertEqual(len(images), len(plan.pages))
        for img, planned in zip(images, plan.heights):
            self.assertEqual(img.height, planned)
            self.assertEqual(img.width, plan.width)

    def test_page_height_covers_all_blocks(self):
        ctx = build_worst_ctx()
        plan = report.plan_report(ctx, LIGHT)
        for page in plan.pages:
            content = sum(b.height for b in page.blocks) - page.blocks[-1].tail
            self.assertGreaterEqual(page.height, content, "页高度必须容得下所有块")
            self.assertLessEqual(page.body_h, page.height)

    def test_measured_height_matches_rendered_image(self):
        ctx = build_worst_ctx()
        heights = report.measure_report(ctx, LIGHT)
        images = report.render_report_pages(ctx, LIGHT)
        self.assertEqual(tuple(img.height for img in images), heights)

    def test_worst_case_is_taller_than_v1_canvas(self):
        """v1.0 的固定 1500px 画布装不下这个用例——本断言就是那条 bug 的墓碑。"""
        ctx = build_worst_ctx()
        heights = report.measure_report(ctx, LIGHT)
        self.assertGreater(sum(heights), 1500)

    def test_last_line_fits_inside_canvas(self):
        ctx = build_worst_ctx()
        plan = report.plan_report(ctx, LIGHT)
        page = plan.pages[0]
        footer_h = report._footer_height(plan.scale)
        bottom = sum(b.height for b in page.blocks) - page.blocks[-1].tail
        self.assertLessEqual(bottom + footer_h, page.height)


class TestThemesAgree(unittest.TestCase):
    def test_light_and_dark_have_identical_geometry(self):
        ctx = build_worst_ctx()
        light = report.measure_report(ctx, LIGHT)
        dark = report.measure_report(ctx, DARK)
        self.assertEqual(light, dark, "版式必须与配色解耦，换主题不应改变尺寸")
        for palette in (LIGHT, DARK):
            for img in report.render_report_pages(ctx, palette):
                self.assertEqual((img.width, img.height), (report.IMG_W, light[0]))

    def test_render_is_rgb(self):
        ctx = build_worst_ctx()
        img = report.render_report_image(ctx, DARK)
        self.assertEqual(img.mode, "RGB")


class TestSupersample(unittest.TestCase):
    def test_2x_doubles_width_and_keeps_ratio(self):
        ctx = build_worst_ctx()
        base = report.measure_report(ctx, LIGHT)
        wanted = report.measure_report(ctx, LIGHT, supersample=2)
        imgs = report.render_report_pages(ctx, LIGHT, supersample=2)
        self.assertEqual(imgs[0].width, report.IMG_W * 2)
        self.assertEqual(imgs[0].height, wanted[0])
        self.assertGreater(wanted[0], base[0], "2x 布局字号同比放大，高度必然更大")

    def test_custom_width(self):
        ctx = build_worst_ctx()
        imgs = report.render_report_pages(ctx, LIGHT, width=1200)
        self.assertEqual(imgs[0].width, 1200)


class TestNotesAreNotDropped(unittest.TestCase):
    def test_all_warnings_rendered(self):
        ctx = build_worst_ctx()
        plan = report.plan_report(ctx, LIGHT)
        joined = "".join(plan.texts)
        for note in ctx.nat.warnings:
            marker = note[:8]
            self.assertIn(marker, joined.replace("\n", ""), f"警告被丢掉：{note[:20]}")

    def test_gateway_notes_are_merged(self):
        ctx = build_worst_ctx()
        merged = report.collect_notes(ctx)
        self.assertIn(build_gateway().notes[0], merged, "JPG 必须和 TXT 一样合并 gateway.notes")
        plan = report.plan_report(ctx, LIGHT)
        self.assertIn("网关策略", "".join(plan.texts))

    def test_collect_notes_dedupes_and_skips_blank(self):
        nat = NatResult()
        nat.warnings = ["重复", "重复", "", "   "]
        gw = build_gateway()
        gw.notes = ["重复", "独有"]
        merged = report.collect_notes(report.ReportContext(nat=nat, gateway=gw))
        self.assertEqual(merged, ["重复", "独有"])

    def test_too_many_notes_points_to_txt(self):
        nat = NatResult()
        nat.warnings = [f"警告条目 {i}" for i in range(report.MAX_NOTE_ITEMS + 5)]
        plan = report.plan_report(report.ReportContext(nat=nat), LIGHT)
        joined = "".join(plan.texts)
        self.assertIn("另有 5 条提示见 TXT 报告", joined)


class TestPagination(unittest.TestCase):
    def test_tall_report_is_split(self):
        nat = NatResult(
            ok=True,
            local_ip="10.0.0.2",
            local_port=40000,
            public_ip="203.0.113.9",
            public_port=40000,
            classic_type=CLASSIC_SYMMETRIC,
            mapping_behavior="apdm",
            filtering_behavior="apdf",
            primary_server="stun.example.com:3478",
        )
        nat.warnings = [f"第 {i} 条提示：" + LONG_WARNING for i in range(report.MAX_NOTE_ITEMS)]
        nat.steps = [
            TestStep(f"步骤 {i}：一个足够长的名字用于占位", "ok", "细节" * 30)
            for i in range(120)
        ]
        ctx = report.ReportContext(nat=nat, gateway=build_gateway(), probe=build_probe())
        heights = report.measure_report(ctx, LIGHT)
        self.assertGreater(len(heights), 1, "超过 4000px 必须自动分页")
        plan = report.plan_report(ctx, LIGHT)
        limit = report.MAX_PAGE_H
        for page in plan.pages:
            self.assertLessEqual(page.height, limit)
        images = report.render_report_pages(ctx, LIGHT)
        self.assertEqual([img.height for img in images], list(heights))

    def test_save_image_names_pages(self):
        nat = NatResult(local_ip="10.0.0.2", classic_type=CLASSIC_SYMMETRIC)
        nat.warnings = [f"第 {i} 条提示：" + LONG_WARNING for i in range(report.MAX_NOTE_ITEMS)]
        nat.steps = [TestStep(f"步骤 {i}：名字长一点" * 3, "ok", "细节" * 30) for i in range(120)]
        ctx = report.ReportContext(nat=nat)
        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, "报告.jpg")
            written = report.save_image(ctx, target, LIGHT, quality=70)
            self.assertGreater(len(written), 1)
            for i, path in enumerate(written, start=1):
                self.assertTrue(os.path.isfile(path), path)
                self.assertTrue(path.endswith(f"_{i}.jpg"), path)
                self.assertGreater(os.path.getsize(path), 1024)

    def test_single_page_keeps_given_path(self):
        ctx = build_worst_ctx()
        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, "NatScope报告.jpg")
            written = report.save_image(ctx, target, LIGHT, quality=80)
            self.assertEqual(written, [target])
            self.assertTrue(os.path.isfile(target))


class TestCandidateRendering(unittest.TestCase):
    """候选态在图上必须只有一种表达：统一描边 + 单个区间标记。"""

    def _block(self, classic: str, kind: str):
        nat = NatResult(
            local_ip="10.0.0.2",
            local_port=40000,
            public_ip="203.0.113.9",
            public_port=40000,
            classic_type=classic,
        )
        plan = report.plan_report(report.ReportContext(nat=nat), LIGHT)
        return plan, next(b for b in plan.pages[0].blocks if b.kind == kind)

    def test_candidate_cells_share_one_outline_color(self):
        plan, block = self._block(CLASSIC_CONE_UNKNOWN, "matrix")
        cells = [
            s
            for s in block.shapes
            if s.kind == "rrect" and (s.box[2] - s.box[0]) < plan.width - 200
        ]
        self.assertEqual(len(cells), 9, "矩阵应恰好 9 个格子")
        outlines = [s.outline for s in cells]
        self.assertEqual(outlines.count(LIGHT.warn), 3, "三个候选格必须是同一个候选色")
        self.assertEqual(len(set(outlines)), 2, f"只应有候选色+排除色：{set(outlines)}")
        self.assertNotIn(
            LIGHT.ok, outlines, "候选格不能出现「绿色=更好」的类型色，那会读出错误信息"
        )

    def test_candidate_cells_have_no_fill_tint(self):
        _plan, block = self._block(CLASSIC_CONE_UNKNOWN, "matrix")
        related = [
            s
            for s in block.shapes
            if s.kind == "rrect" and s.outline == LIGHT.warn
        ]
        for shape in related:
            self.assertEqual(shape.fill, LIGHT.surface, "候选格应为无填充，只留统一描边")

    def test_spectrum_draws_single_span_marker(self):
        plan, block = self._block(CLASSIC_CONE_UNKNOWN, "spectrum")
        triangles = [s for s in block.shapes if s.kind == "poly"]
        self.assertEqual(len(triangles), 1, "候选区间只应有一个三角标记，不是三个")
        self.assertIn("锥型（三选一，无法细分）", "".join(plan.texts))

    def test_span_marker_sits_at_interval_center(self):
        _plan, block = self._block(CLASSIC_CONE_UNKNOWN, "spectrum")
        tri = next(s for s in block.shapes if s.kind == "poly")
        tri_cx = sum(p[0] for p in tri.points) / len(tri.points)
        middle = next(line for line in block.lines if line.text == "受限锥型")
        self.assertAlmostEqual(tri_cx, middle.x, delta=2)

    def test_definite_type_keeps_single_segment_marker(self):
        _plan, block = self._block(CLASSIC_FULL_CONE, "spectrum")
        self.assertEqual(len([s for s in block.shapes if s.kind == "poly"]), 1)
        self.assertNotIn("三选一", "".join(l.text for l in block.lines))

    def test_unknown_has_no_marker(self):
        _plan, block = self._block(CLASSIC_UNKNOWN, "spectrum")
        self.assertEqual([s for s in block.shapes if s.kind == "poly"], [])

    def test_active_cell_still_uses_type_color(self):
        plan, block = self._block(CLASSIC_FULL_CONE, "matrix")
        cells = [
            s
            for s in block.shapes
            if s.kind == "rrect" and (s.box[2] - s.box[0]) < plan.width - 200
        ]
        self.assertIn(LIGHT.ok, [s.outline for s in cells], "测得的那一格仍应呈现类型色")


class TestEveryTypeRenders(unittest.TestCase):
    """8 种传统类型都能出图，且都不会因为某个块高度算错而崩。"""

    def test_all_classic_types(self):
        cases = (
            CLASSIC_OPEN,
            CLASSIC_FULL_CONE,
            CLASSIC_RESTRICTED,
            CLASSIC_PORT_RESTRICTED,
            CLASSIC_SYMMETRIC,
            CLASSIC_CONE_UNKNOWN,
            CLASSIC_BLOCKED,
            CLASSIC_UNKNOWN,
        )
        for classic in cases:
            with self.subTest(classic=classic):
                nat = NatResult(
                    local_ip="192.168.1.2",
                    local_port=40000,
                    public_ip="203.0.113.9",
                    public_port=40000,
                    classic_type=classic,
                )
                ctx = report.ReportContext(nat=nat)
                plan = report.plan_report(ctx, LIGHT)
                img = report.render_report_image(ctx, LIGHT)
                self.assertEqual((img.width, img.height), (report.IMG_W, plan.heights[0]))
                self.assertGreater(img.height, 600, "整份报告不可能这么矮")


class TestContentCoverage(unittest.TestCase):
    """报告里该出现的信息一条都不能少（旧版只画了结论和服务器统计）。"""

    def test_visual_blocks_present(self):
        ctx = build_worst_ctx()
        joined = "".join(report.plan_report(ctx, LIGHT).texts)
        for expected in (
            "类型谱 Spectrum",
            "行为矩阵 Behavior Matrix",
            "场景可用性 Scenarios",
            "映射样本 Mapping Samples",
            "网关端口映射能力 Gateway",
            "测试明细",
            "结论说明",
            "网络信息",
            "提示与警告",
        ):
            self.assertIn(expected, joined, f"缺少图形块：{expected}")

    def test_matrix_cells_and_spectrum_labels_are_drawn(self):
        ctx = build_worst_ctx()
        joined = "".join(report.plan_report(ctx, LIGHT).texts)
        mat = viz.build_matrix(ctx.nat)
        for line in mat.cells:
            for cell in line:
                self.assertIn(cell.title, joined)
        for seg in viz.build_spectrum(ctx.nat):
            self.assertIn(seg.label, joined)

    def test_txt_report_contains_matrix_and_scenarios(self):
        text = report.build_report_text(build_worst_ctx())
        self.assertIn("【三、行为矩阵】", text)
        self.assertIn("【四、场景可用性】", text)
        self.assertIn("完全锥型", text)
        self.assertIn("第五条警告", text)

    def test_txt_and_jpg_share_notes(self):
        ctx = build_worst_ctx()
        txt = report.build_report_text(ctx)
        for note in report.collect_notes(ctx):
            self.assertIn(note[:10], txt)


class TestNoHexLiterals(unittest.TestCase):
    """配色只能来自 Palette；渲染代码里出现字面量 hex 就是换主题时的定时炸弹。"""

    def test_report_source_has_no_hex_colors(self):
        path = os.path.join(ROOT, "natscope", "report.py")
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        hits = re.findall(r'["\']#[0-9A-Fa-f]{3,8}["\']', source)
        self.assertEqual(hits, [], f"report.py 里不允许写死颜色：{hits}")

    def test_viz_source_has_no_hex_colors(self):
        path = os.path.join(ROOT, "natscope", "viz.py")
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        hits = re.findall(r'["\']#[0-9A-Fa-f]{3,8}["\']', source)
        self.assertEqual(hits, [], f"viz.py 只给语义 tone，不该出现具体颜色：{hits}")


class TestTextFitting(unittest.TestCase):
    """像素级省略与换行：中英混排不能靠字符数截断。"""

    def test_ellipsize_respects_pixel_budget(self):
        m = report._Metrics(1.0)
        text = "Test II：Change IP+Port（这是一个很长的名字）"
        for budget in (60, 120, 200, 400):
            got = m.ellipsize("body", text, budget)
            self.assertLessEqual(m.width("body", got), budget)
            if budget < m.width("body", text):
                self.assertTrue(got.endswith("…"))

    def test_short_text_is_untouched(self):
        m = report._Metrics(1.0)
        self.assertEqual(m.ellipsize("body", "短", 200), "短")

    def test_wrap_never_exceeds_budget(self):
        m = report._Metrics(1.0)
        text = "P2P 类型应用可用性最佳：P2P 游戏、BT 下载、远程访问等场景表现最优秀。"
        lines = m.wrap("body", text, 300)
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(m.width("body", line), 300 + 4)

    def test_wrap_keeps_every_character(self):
        m = report._Metrics(1.0)
        text = "中文与 English 混排，,还有标点。"
        lines = m.wrap("body", text, 80)
        # 断行处的空格会被丢掉（不该出现在行首），其余字符一个都不能少
        self.assertEqual("".join(lines).replace(" ", ""), text.replace(" ", ""))

    def test_wrap_does_not_split_ascii_words(self):
        m = report._Metrics(1.0)
        text = "本次没有可用的 STUN 服务器响应 CHANGE-REQUEST，过滤行为无法测量"
        lines = m.wrap("body", text, 260)
        self.assertTrue(
            any("CHANGE-REQUEST" in line for line in lines),
            f"术语被拦腰截断：{lines}",
        )
        for line in lines:
            self.assertFalse(line.startswith(("-", " ")), f"行首出现空白/断词：{line!r}")

    def test_explicit_newline_is_respected(self):
        m = report._Metrics(1.0)
        self.assertEqual(m.wrap("body", "第一行\n第二行", 400), ["第一行", "第二行"])


if __name__ == "__main__":
    unittest.main()
