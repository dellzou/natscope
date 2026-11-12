# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""视图模型（``natscope.viz``）的数据契约测试。

三条底线：

1. **零 GUI 依赖**：``import natscope.viz`` 不得把 tkinter / PIL 拉进来，
   否则 CLI、单元测试和打包脚本都会被拖慢，甚至在没有 GUI 的环境里直接崩；
2. **语义正确**：3×3 行为矩阵 8 种传统类型 × 3 种过滤行为都要落在该落的格子
   上，锥型不可细分时必须是「整行候选 + 有说明」，不能假装精确；
3. **最小输入不崩**：只有一个 ``NatResult()`` 时所有函数都要能出图/出数据。

运行：python -m unittest discover -s tests -v
"""

import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from natscope import viz
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
    FILT_ADF,
    FILT_APDF,
    FILT_EIF,
    MAP_ADM,
    MAP_APDM,
    MAP_EIM,
    MappingSample,
    NatResult,
)
from natscope.theme import DARK, LIGHT, type_color

ALL_CLASSIC = (
    CLASSIC_OPEN,
    CLASSIC_FULL_CONE,
    CLASSIC_RESTRICTED,
    CLASSIC_PORT_RESTRICTED,
    CLASSIC_SYMMETRIC,
    CLASSIC_CONE_UNKNOWN,
    CLASSIC_BLOCKED,
    CLASSIC_UNKNOWN,
)
ALL_FILTERING = (FILT_EIF, FILT_ADF, FILT_APDF)

STATES = {viz.STATE_ACTIVE, viz.STATE_RELATED, viz.STATE_DIM, viz.STATE_NA}
TONES = {viz.TONE_OK, viz.TONE_WARN, viz.TONE_BAD, viz.TONE_NA}


class TestImportIsolation(unittest.TestCase):
    """viz 是纯数据层，导入期绝不能碰 tkinter / PIL。"""

    def test_import_does_not_pull_tkinter_or_pil(self):
        code = (
            "import sys\n"
            "import natscope.viz as v\n"
            "print('PIL' in sys.modules)\n"
            "print('tkinter' in sys.modules)\n"
            "print(len(v.build_matrix(v.NatResult()).cells))\n"
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
        self.assertEqual(out[0].strip(), "False", "import natscope.viz 不应加载 PIL")
        self.assertEqual(out[1].strip(), "False", "import natscope.viz 不应加载 tkinter")
        self.assertEqual(out[2].strip(), "3")

    def test_source_has_no_gui_imports(self):
        path = os.path.join(ROOT, "natscope", "viz.py")
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        for banned in ("import tkinter", "from tkinter", "import PIL", "from PIL"):
            self.assertNotIn(banned, source, f"viz.py 里不允许出现 {banned!r}")


class TestMatrixStructure(unittest.TestCase):
    def test_grid_is_3x3_and_well_formed(self):
        mat = viz.build_matrix(NatResult(classic_type=CLASSIC_FULL_CONE))
        self.assertEqual(mat.rows, (MAP_EIM, MAP_ADM, MAP_APDM))
        self.assertEqual(mat.cols, (FILT_EIF, FILT_ADF, FILT_APDF))
        self.assertEqual(len(mat.cells), 3)
        for row_code, line in zip(mat.rows, mat.cells):
            self.assertEqual(len(line), 3)
            for col_code, cell in zip(mat.cols, line):
                self.assertEqual(cell.row, row_code)
                self.assertEqual(cell.col, col_code)
                self.assertTrue(cell.title)
                self.assertTrue(cell.sub)
                self.assertIn(cell.state, STATES)
                self.assertIn(cell.tone, TONES)
        self.assertIsInstance(mat.note, str)

    def test_every_classic_type_times_every_filtering(self):
        for classic in ALL_CLASSIC:
            for filtering in ALL_FILTERING:
                with self.subTest(classic=classic, filtering=filtering):
                    nat = NatResult(classic_type=classic, filtering_behavior=filtering)
                    mat = viz.build_matrix(nat)
                    self.assertEqual(len(mat.cells), 3)
                    for line in mat.cells:
                        self.assertEqual(len(line), 3)
                    # 结论只能落在「一格」或「一整行」上，绝不能两行都高亮
                    active_rows = {
                        c.row for line in mat.cells for c in line if c.state == viz.STATE_ACTIVE
                    }
                    related_rows = {
                        c.row for line in mat.cells for c in line if c.state == viz.STATE_RELATED
                    }
                    self.assertLessEqual(len(active_rows), 2)
                    self.assertFalse(active_rows & related_rows)

    def test_cone_semantics_of_the_three_cells(self):
        cases = {
            FILT_EIF: ("完全锥型", "Full Cone"),
            FILT_ADF: ("受限锥型", "Restricted Cone"),
            FILT_APDF: ("端口受限锥型", "Port Restricted Cone"),
        }
        for filtering, (title, sub) in cases.items():
            with self.subTest(filtering=filtering):
                mat = viz.build_matrix(
                    NatResult(classic_type=CLASSIC_FULL_CONE, filtering_behavior=filtering)
                )
                cell = mat.get(MAP_EIM, filtering)
                self.assertEqual(cell.title, title)
                self.assertEqual(cell.sub, sub)
                self.assertEqual(cell.state, viz.STATE_ACTIVE)
                # 同行的另外两格必须被实测排除（灰显），不能标成「可能」
                for other in ALL_FILTERING:
                    if other == filtering:
                        continue
                    self.assertEqual(mat.get(MAP_EIM, other).state, viz.STATE_DIM)

    def test_symmetric_rows_are_all_symmetric(self):
        for row in (MAP_ADM, MAP_APDM):
            for col in ALL_FILTERING:
                cell = viz.build_matrix(
                    NatResult(classic_type=CLASSIC_SYMMETRIC, mapping_behavior=row,
                              filtering_behavior=col)
                ).get(row, col)
                self.assertEqual(cell.title, "对称型")
                self.assertEqual(cell.sub, "Symmetric")
                self.assertEqual(cell.tone, viz.TONE_BAD)

    def test_symmetric_with_known_filtering_marks_one_column(self):
        mat = viz.build_matrix(
            NatResult(classic_type=CLASSIC_SYMMETRIC, filtering_behavior=FILT_ADF)
        )
        active = mat.highlighted()
        self.assertEqual({c.col for c in active}, {FILT_ADF})
        self.assertEqual({c.row for c in active}, {MAP_ADM, MAP_APDM})
        self.assertTrue(all(c.state == viz.STATE_ACTIVE for c in active))

    def test_cone_unknown_marks_whole_row_and_explains(self):
        mat = viz.build_matrix(
            NatResult(classic_type=CLASSIC_CONE_UNKNOWN, mapping_behavior=MAP_EIM)
        )
        for col in ALL_FILTERING:
            self.assertEqual(mat.get(MAP_EIM, col).state, viz.STATE_RELATED)
        for row in (MAP_ADM, MAP_APDM):
            for col in ALL_FILTERING:
                self.assertEqual(mat.get(row, col).state, viz.STATE_DIM)
        self.assertIn("锥型（未细分）", mat.note)

    def test_open_and_blocked_are_all_not_applicable(self):
        for classic in (CLASSIC_OPEN, CLASSIC_BLOCKED):
            with self.subTest(classic=classic):
                mat = viz.build_matrix(NatResult(classic_type=classic))
                for line in mat.cells:
                    for cell in line:
                        self.assertEqual(cell.state, viz.STATE_NA)
                self.assertTrue(mat.note, "全 na 时必须说明原因，否则图上一片空白")

    def test_unknown_mapping_leaves_matrix_empty(self):
        mat = viz.build_matrix(NatResult())
        for line in mat.cells:
            for cell in line:
                self.assertEqual(cell.state, viz.STATE_DIM)
        self.assertTrue(mat.note)


class TestSpectrum(unittest.TestCase):
    def test_order_is_best_to_worst(self):
        segs = viz.build_spectrum(NatResult(classic_type=CLASSIC_FULL_CONE))
        self.assertEqual(
            [s.key for s in segs],
            [
                CLASSIC_OPEN,
                CLASSIC_FULL_CONE,
                CLASSIC_RESTRICTED,
                CLASSIC_PORT_RESTRICTED,
                CLASSIC_SYMMETRIC,
                CLASSIC_BLOCKED,
            ],
        )
        scores = [s.score for s in segs]
        self.assertEqual(scores, sorted(scores, reverse=True), "类型谱必须从好到差排列")

    def test_exactly_one_current_for_definite_types(self):
        for classic in (CLASSIC_OPEN, CLASSIC_FULL_CONE, CLASSIC_RESTRICTED,
                        CLASSIC_PORT_RESTRICTED, CLASSIC_SYMMETRIC, CLASSIC_BLOCKED):
            with self.subTest(classic=classic):
                segs = viz.build_spectrum(NatResult(classic_type=classic))
                current = [s for s in segs if s.state == "current"]
                self.assertEqual(len(current), 1)
                self.assertEqual(current[0].key, classic)

    def test_cone_unknown_marks_three_candidates(self):
        segs = viz.build_spectrum(NatResult(classic_type=CLASSIC_CONE_UNKNOWN))
        cands = [s for s in segs if s.state == "candidate"]
        self.assertEqual(
            [s.key for s in cands],
            [CLASSIC_FULL_CONE, CLASSIC_RESTRICTED, CLASSIC_PORT_RESTRICTED],
        )
        self.assertFalse([s for s in segs if s.state == "current"])

    def test_unknown_has_no_highlight(self):
        segs = viz.build_spectrum(NatResult())
        self.assertTrue(all(s.state == "dim" for s in segs))


class TestScenarios(unittest.TestCase):
    #: 与需求文档冻结的可用性表逐字对应
    TABLE = {
        CLASSIC_OPEN: ("ok", "ok", "ok", "ok"),
        CLASSIC_FULL_CONE: ("ok", "ok", "ok", "ok"),
        CLASSIC_RESTRICTED: ("ok", "ok", "warn", "ok"),
        CLASSIC_PORT_RESTRICTED: ("warn", "warn", "bad", "warn"),
        CLASSIC_SYMMETRIC: ("bad", "warn", "bad", "bad"),
        CLASSIC_CONE_UNKNOWN: ("ok", "ok", "warn", "ok"),
        CLASSIC_BLOCKED: ("bad", "bad", "bad", "bad"),
        CLASSIC_UNKNOWN: ("na", "na", "na", "na"),
    }

    def test_availability_table(self):
        for classic, expected in self.TABLE.items():
            with self.subTest(classic=classic):
                got = tuple(s.level for s in viz.build_scenarios(NatResult(classic_type=classic)))
                self.assertEqual(got, expected)

    def test_scenario_fields(self):
        items = viz.build_scenarios(NatResult(classic_type=CLASSIC_FULL_CONE))
        self.assertEqual([s.key for s in items], ["game", "bittorrent", "remote", "voice"])
        for sc in items:
            self.assertTrue(sc.label)
            self.assertTrue(sc.sub)
            self.assertTrue(sc.hint)
            self.assertIn(sc.tone, TONES)


class TestSamplesAndBadges(unittest.TestCase):
    def test_no_samples_is_empty_tuple(self):
        self.assertEqual(viz.build_samples(NatResult()), ())

    def test_stable_and_drifting_ports(self):
        nat = NatResult()
        nat.samples = [
            MappingSample("1.1.1.1", 3478, "203.0.113.7", 52143, "a:3478"),
            MappingSample("2.2.2.2", 3478, "203.0.113.7", 52143, "b:3478"),
            MappingSample("3.3.3.3", 3478, "203.0.113.7", 52150, "c:3478"),
        ]
        chips = viz.build_samples(nat)
        self.assertEqual(len(chips), 3)
        self.assertEqual([c.index for c in chips], [1, 2, 3])
        self.assertEqual([c.stable for c in chips], [True, True, False])
        self.assertEqual(chips[0].tone, viz.TONE_OK)
        self.assertEqual(chips[2].tone, viz.TONE_WARN)
        self.assertEqual(chips[0].dst, "1.1.1.1:3478")
        self.assertEqual(chips[0].mapped, "203.0.113.7:52143")
        self.assertIn("→", chips[0].text)

    def test_badges_without_gateway(self):
        badges = viz.build_badges(None)
        self.assertEqual(len(badges), 1)
        self.assertEqual(badges[0].value, "未检测")
        self.assertEqual(badges[0].tone, viz.TONE_NA)

    def test_badges_from_capabilities(self):
        gw = GatewayReport(gateway_ip="192.168.1.1")
        gw.upnp = Capability("UPnP-IGD", available=True, state="ok", detail="ok")
        gw.natpmp = Capability("NAT-PMP", state="absent", detail="no")
        gw.pcp = Capability("PCP", state="limited", detail="被拒绝")
        badges = viz.build_badges(gw)
        self.assertEqual([b.label for b in badges], ["UPnP", "NAT-PMP", "PCP"])
        self.assertEqual([b.tone for b in badges],
                         [viz.TONE_OK, viz.TONE_NA, viz.TONE_WARN])
        self.assertEqual([b.value for b in badges], ["可用", "未发现", "存在但受限"])


class TestVerdict(unittest.TestCase):
    def test_verdict_shape(self):
        for classic in ALL_CLASSIC:
            with self.subTest(classic=classic):
                title, sub, tone = viz.verdict(NatResult(classic_type=classic))
                self.assertTrue(title)
                self.assertTrue(sub)
                self.assertIn(tone, TONES)
                self.assertEqual(tone, viz.type_tone(classic))

    def test_unknown_type_falls_back(self):
        title, sub, tone = viz.verdict(NatResult(classic_type="who-knows"))
        self.assertEqual((title, sub, tone), ("无法判定", "Unknown · 可用服务器不足", viz.TONE_NA))

    def test_tone_agrees_with_theme_type_color(self):
        """viz 的 tone 与 theme.type_color 必须指向同一族的颜色。"""
        families = {
            viz.TONE_OK: "ok",
            viz.TONE_WARN: "warn",
            viz.TONE_BAD: "err",
            viz.TONE_NA: "text_dim",
        }
        for palette in (LIGHT, DARK):
            for classic in ALL_CLASSIC:
                with self.subTest(palette=palette.name, classic=classic):
                    expected = getattr(palette, families[viz.type_tone(classic)])
                    self.assertEqual(type_color(palette, classic), expected)


class TestMinimalInput(unittest.TestCase):
    """只有一个 NatResult()（甚至只有一个 IP）时也必须能出完整模型。"""

    def test_bare_nat_result(self):
        nat = NatResult()
        self.assertEqual(len(viz.build_matrix(nat).cells), 3)
        self.assertEqual(len(viz.build_spectrum(nat)), 6)
        self.assertEqual(viz.build_samples(nat), ())
        self.assertEqual(len(viz.build_badges(None)), 1)
        self.assertEqual(len(viz.build_scenarios(nat)), 4)
        self.assertEqual(len(viz.verdict(nat)), 3)

    def test_only_addresses(self):
        nat = NatResult(local_ip="192.168.1.2", public_ip="203.0.113.4")
        mat = viz.build_matrix(nat)
        self.assertTrue(mat.note)
        self.assertEqual(len(viz.build_spectrum(nat)), 6)
        for sc in viz.build_scenarios(nat):
            self.assertEqual(sc.level, "na")


if __name__ == "__main__":
    unittest.main()
