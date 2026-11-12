# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""许可合规、品牌契约、配置格式与配色令牌的"地基"测试。

覆盖 T01 冻结下来的四组不变量：

1. 全仓无 MIT 残留，LICENSE 已换成 PolyForm Noncommercial 并带 Required Notice；
2. 每个 .py 文件头部两行 SPDX 标识，且标识符逐字为
   ``PolyForm-Noncommercial-1.0.0``；
3. ``import natscope.brand`` 不加载 tkinter / PIL，图形几何可渲染且不出界；
4. ``natscope.config`` 的新字段读写往返、旧 schema 文件可加载、
   ``save()`` 失败时返回 False；``natscope.theme`` 令牌达到对比度要求。

运行：python -m unittest discover -s tests -v
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

#: 不参与扫描的目录：构建产物、临时文件、虚拟环境、编辑器状态
SKIP_DIRS = {
    ".git",
    ".tmp",
    ".workbuddy",
    ".venv",
    "venv",
    "build",
    "dist",
    "__pycache__",
    ".pytest_cache",
    ".idea",
}

#: 可能内嵌许可文本的文本类后缀
TEXT_SUFFIXES = (".py", ".md", ".toml", ".txt", ".yml", ".yaml", ".json", ".spec", ".cfg")

#: 第三方声明文件是**唯一**允许出现第三方许可全文的地方
NOTICE_FILE = os.path.join("docs", "THIRD-PARTY-NOTICES.md")

LICENSE_ID = "PolyForm-Noncommercial-1.0.0"


def iter_files(suffixes=TEXT_SUFFIXES):
    """遍历仓库内的文本文件，跳过构建产物与临时目录。"""
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.lower().endswith(suffixes):
                yield os.path.join(dirpath, name)


def iter_residue_targets():
    """扫描残留时要覆盖的文件：所有文本文件 + 仓库根的 LICENSE。"""
    yield from iter_files()
    license_path = os.path.join(ROOT, "LICENSE")
    if os.path.isfile(license_path):
        yield license_path


def read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def rel(path):
    return os.path.relpath(path, ROOT).replace("\\", "/")


def contrast_ratio(fg, bg):
    """WCAG 2.1 相对亮度对比度，输入 ``#RRGGBB``。"""

    def channel(value):
        value = value / 255.0
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    def luminance(color):
        color = color.lstrip("#")
        r, g, b = (int(color[i : i + 2], 16) for i in (0, 2, 4))
        return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)

    hi, lo = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class TestNoLicenseResidue(unittest.TestCase):
    """改协议最容易漏掉的地方：文档、徽章、CI 里还留着旧协议字样。"""

    #: 拼起来而不是写整句，否则本文件自己就会被下一条测试判定为残留
    FORBIDDEN = ("MIT " + "License", "Permission is hereby granted" + ", free of charge")

    def test_no_mit_residue_anywhere(self):
        offenders = []
        for path in iter_residue_targets():
            if rel(path) == NOTICE_FILE.replace("\\", "/"):
                continue
            text = read_text(path)
            for phrase in self.FORBIDDEN:
                if phrase in text:
                    offenders.append(f"{rel(path)}: {phrase}")
        self.assertEqual(offenders, [], f"仍存在 MIT 残留（全文）：{offenders}")

    def test_notice_file_is_the_only_place(self):
        # 兜底：确保上一条的豁免逻辑真的生效（声明文件存在且被豁免）
        notice = os.path.join(ROOT, NOTICE_FILE)
        self.assertTrue(os.path.isfile(notice), f"缺少 {NOTICE_FILE}")


class TestLicenseFile(unittest.TestCase):
    def setUp(self):
        self.text = read_text(os.path.join(ROOT, "LICENSE"))

    def test_polyform_header(self):
        self.assertIn("PolyForm Noncommercial License 1.0.0", self.text)
        self.assertIn("polyformproject.org/licenses/noncommercial/1.0.0", self.text)

    def test_required_notice(self):
        self.assertIn("Required Notice: Copyright 2026 ZouNing", self.text)
        self.assertIn("https://github.com/dellzou", self.text)

    def test_required_notice_is_at_the_top(self):
        head = self.text.splitlines()[:3]
        joined = "\n".join(head)
        self.assertIn("Required Notice:", joined, "Required Notice 必须放在正文之前")

    def test_key_sections_present(self):
        # PolyForm 全文一字不改地保留：抽查 4 个章节标题
        for heading in (
            "## Acceptance",
            "## Notices",
            "## Noncommercial Purposes",
            "## No Liability",
        ):
            self.assertIn(heading, self.text)

    def test_old_mit_text_gone(self):
        for phrase in TestNoLicenseResidue.FORBIDDEN:
            self.assertNotIn(phrase, self.text)


class TestSpdxHeaders(unittest.TestCase):
    """每个 Python 源文件前 3 行内必须有 SPDX 版权行与许可标识行。"""

    def test_every_py_has_spdx_header(self):
        missing = []
        wrong_id = []
        for path in iter_files(suffixes=(".py",)):
            head = "\n".join(read_text(path).splitlines()[:3])
            if "SPDX-FileCopyrightText:" not in head:
                missing.append(rel(path))
                continue
            if f"SPDX-License-Identifier: {LICENSE_ID}" not in head:
                wrong_id.append(rel(path))
        self.assertEqual(missing, [], f"缺少 SPDX 版权头：{missing}")
        self.assertEqual(wrong_id, [], f"许可标识符不匹配（区分大小写与连字符）：{wrong_id}")

    def test_package_declares_version_and_license(self):
        import natscope

        self.assertEqual(natscope.__version__, "1.1.0")
        self.assertEqual(natscope.__license__, LICENSE_ID)
        self.assertIn("__license__", natscope.__all__)


class TestBrandImport(unittest.TestCase):
    """品牌模块必须能在没有 GUI/PIL 的环境里被导入（CLI、打包脚本都要用）。"""

    def test_import_does_not_pull_tkinter_or_pil(self):
        code = (
            "import sys\n"
            "import natscope.brand as b\n"
            "print('PIL' in sys.modules)\n"
            "print('tkinter' in sys.modules)\n"
            "print(b.NAME, b.__name__)\n"
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
        self.assertEqual(out[0].strip(), "False", "import natscope.brand 不应加载 PIL")
        self.assertEqual(out[1].strip(), "False", "import natscope.brand 不应加载 tkinter")
        self.assertIn("NatScope", out[2])

    def test_brand_constants(self):
        from natscope import brand

        self.assertEqual(brand.NAME, "NatScope")
        self.assertEqual(brand.TAGLINE, "NAT 类型检测")
        self.assertEqual(brand.SUBTITLE, "RFC 3489 / RFC 5780")
        self.assertEqual(brand.LICENSE_ID, LICENSE_ID)
        self.assertEqual(len(brand.BRAND_DARK), 3)
        self.assertEqual(len(brand.BRAND_ACCENT), 3)
        self.assertEqual(
            brand.hex_color(brand.BRAND_DARK),
            "#{:02X}{:02X}{:02X}".format(*brand.BRAND_DARK),
        )


class TestMarkGeometry(unittest.TestCase):
    """图标几何：归一化、居中、连杆与缺口共线、绿点不啃环、笔画出界。"""

    def setUp(self):
        from natscope import brand

        self.brand = brand

    def test_all_geometry_normalized(self):
        # 例外：_deg 是角度；_scale 是倍数因子，二者都不是 0..1 归一化长度
        for key, value in self.brand.MARK_GEOM.items():
            self.assertIsInstance(value, float, key)
            if key.endswith("_deg"):
                self.assertGreaterEqual(value, -180.0, key)
                self.assertLessEqual(value, 360.0, key)
                continue
            if key.endswith("_scale"):
                self.assertGreater(value, 0.0, key)
                continue
            self.assertGreater(value, 0.0, key)
            self.assertLess(value, 1.0, key)

    def test_layout_is_centered_and_in_bounds(self):
        for variant in ("full", "simple"):
            lay = self.brand.layout(variant)
            pts = [lay["ring_center"], lay["ball_center"], *lay["rod"]]
            r_out = lay["ring_outer_r"]
            ball_r = lay["ball_r"]
            xs = [p[0] for p in pts] + [
                lay["ring_center"][0] - r_out,
                lay["ring_center"][0] + r_out,
                lay["ball_center"][0] - ball_r,
                lay["ball_center"][0] + ball_r,
            ]
            ys = [p[1] for p in pts] + [
                lay["ring_center"][1] - r_out,
                lay["ring_center"][1] + r_out,
                lay["ball_center"][1] - ball_r,
                lay["ball_center"][1] + ball_r,
            ]
            self.assertGreater(min(xs), 0.05, variant)
            self.assertLess(max(xs), 0.95, variant)
            self.assertGreater(min(ys), 0.05, variant)
            self.assertLess(max(ys), 0.95, variant)
            self.assertAlmostEqual((min(xs) + max(xs)) / 2.0, 0.5, places=6)
            self.assertAlmostEqual((min(ys) + max(ys)) / 2.0, 0.5, places=6)

    def test_gap_rod_ball_are_collinear_at_45_degrees(self):
        lay = self.brand.layout("full")
        cx, cy = lay["ring_center"]
        bx, by = lay["ball_center"]
        self.assertAlmostEqual(bx - cx, cy - by, places=9)  # 45°
        self.assertGreater(bx, cx)
        rod = lay["rod"]
        self.assertEqual(len(rod), 2 * (self.brand.ROD_ARC_STEPS + 1))
        # 缺口角度跨度覆盖连杆底边：底边两端点正好落在 ±gap/2 上
        self.assertAlmostEqual(lay["arc_end_deg"] - lay["arc_start_deg"], 360.0 - lay["gap_deg"])

    def test_simple_variant_drops_rod_and_thickens_ring(self):
        full = self.brand.layout("full")
        simple = self.brand.layout("simple")
        self.assertTrue(full["rod"])
        self.assertEqual(simple["rod"], ())
        self.assertGreater(simple["ring_width"], full["ring_width"])
        self.assertGreater(simple["ball_r"], full["ball_r"])

    def test_ball_clears_ring(self):
        for variant in ("full", "simple"):
            lay = self.brand.layout(variant)
            cx, cy = lay["ring_center"]
            bx, by = lay["ball_center"]
            gap = ((bx - cx) ** 2 + (by - cy) ** 2) ** 0.5 - lay["ball_r"] - lay["ring_outer_r"]
            self.assertGreater(gap, 0.0, f"{variant} 绿点与圆环重叠")

    def test_variant_threshold(self):
        self.assertEqual(self.brand.pick_variant(16), "simple")
        self.assertEqual(self.brand.pick_variant(31), "simple")
        self.assertEqual(self.brand.pick_variant(32), "full")
        self.assertEqual(self.brand.pick_variant(256), "full")

    def test_render_stays_inside_rounded_tile(self):
        from PIL import ImageChops, ImageDraw, Image

        for variant, size in (("simple", 16), ("simple", 64), ("full", 48), ("full", 256)):
            img = self.brand.render_mark(size, variant)
            self.assertEqual(img.size, (size, size))
            mask = Image.new("L", (size, size), 0)
            radius = max(1, int(round(self.brand.MARK_GEOM["corner_radius"] * size)))
            ImageDraw.Draw(mask).rounded_rectangle(
                [0, 0, size - 1, size - 1], radius=min(radius, size // 2), fill=255
            )
            leaked = ImageChops.subtract(img.getchannel("A"), mask)
            self.assertIsNone(leaked.getbbox(), f"{variant}@{size} 有像素溢出圆角方底")

    def test_render_produces_expected_palette(self):
        img = self.brand.render_mark(64, "full").convert("RGB")
        colors = {c for _, c in img.getcolors(maxcolors=1 << 16)}
        target = self.brand.BRAND_ACCENT
        self.assertTrue(
            any(abs(c[0] - target[0]) < 8 and abs(c[1] - target[1]) < 8 and abs(c[2] - target[2]) < 8
                for c in colors),
            "图标里找不到品牌强调色",
        )


class TestConfigSchema(unittest.TestCase):
    def test_defaults(self):
        from natscope.config import CURRENT_SCHEMA, Config

        cfg = Config()
        self.assertEqual(cfg.schema, CURRENT_SCHEMA)
        self.assertEqual(cfg.window_geometry, "")
        self.assertFalse(cfg.compact)
        self.assertTrue(cfg.gateway_enabled)
        self.assertFalse(cfg.first_run_ack)
        self.assertEqual(cfg.log_level, "INFO")

    def test_roundtrip(self):
        from natscope.config import THEME_DARK, Config

        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            cfg = Config(
                theme=THEME_DARK,
                custom_servers=["stun.example.com:3479"],
                disabled=["stun.l.google.com:19302"],
                last_dir="D:/reports",
                timeout=2.5,
                window_geometry="980x720+120+80",
                compact=True,
                gateway_enabled=False,
                first_run_ack=True,
                log_level="DEBUG",
            )
            self.assertIs(cfg.save(path), True)
            back = Config.load(path)
            self.assertEqual(back.theme, THEME_DARK)
            self.assertEqual(back.custom_servers, ["stun.example.com:3479"])
            self.assertEqual(back.disabled, ["stun.l.google.com:19302"])
            self.assertEqual(back.last_dir, "D:/reports")
            self.assertAlmostEqual(back.timeout, 2.5)
            self.assertEqual(back.window_geometry, "980x720+120+80")
            self.assertTrue(back.compact)
            self.assertFalse(back.gateway_enabled)
            self.assertTrue(back.first_run_ack)
            self.assertEqual(back.log_level, "DEBUG")
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self.assertEqual(raw["schema"], 2)

    def test_loads_legacy_file_without_schema(self):
        from natscope.config import CURRENT_SCHEMA, THEME_LIGHT, Config

        legacy = {
            "theme": "light",
            "custom_servers": ["stun.example.com:3479"],
            "disabled": [],
            "last_dir": "C:/old",
            "timeout": 3.0,
        }
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(legacy, f)
            cfg = Config.load(path)
            self.assertEqual(cfg.theme, THEME_LIGHT)
            self.assertEqual(cfg.custom_servers, ["stun.example.com:3479"])
            self.assertAlmostEqual(cfg.timeout, 3.0)
            # 旧文件缺的字段必须走默认值，而不是变成 None
            self.assertEqual(cfg.schema, CURRENT_SCHEMA)
            self.assertEqual(cfg.window_geometry, "")
            self.assertFalse(cfg.compact)
            self.assertTrue(cfg.gateway_enabled)
            self.assertFalse(cfg.first_run_ack)
            self.assertEqual(cfg.log_level, "INFO")

    def test_unknown_and_bad_fields_are_ignored(self):
        from natscope.config import Config

        payload = {
            "schema": 99,
            "theme": "neon",
            "timeout": "fast",
            "compact": "yes",
            "log_level": "LOUD",
            "window_geometry": "x" * 500,
            "who_knows": [1, 2, 3],
        }
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            cfg = Config.load(path)
            self.assertEqual(cfg.theme, "auto")
            self.assertAlmostEqual(cfg.timeout, 1.5)
            self.assertFalse(cfg.compact)
            self.assertEqual(cfg.log_level, "INFO")
            self.assertEqual(cfg.window_geometry, "")

    def test_missing_file_is_not_an_error(self):
        from natscope.config import Config

        cfg = Config.load(os.path.join(ROOT, "definitely-not-here", "config.json"))
        self.assertEqual(cfg.theme, "auto")

    def test_save_returns_false_when_unwritable(self):
        from natscope.config import Config

        with tempfile.TemporaryDirectory() as td:
            blocker = os.path.join(td, "blocker")
            with open(blocker, "w", encoding="utf-8") as f:
                f.write("i am a file, not a directory")
            # 父路径是个文件 -> 建目录必失败 -> 必须返回 False 而不是静默吞掉
            ok = Config().save(os.path.join(blocker, "config.json"))
            self.assertIs(ok, False)


class TestThemeTokens(unittest.TestCase):
    def test_text_faint_meets_wcag_aa(self):
        from natscope.theme import DARK, LIGHT

        for palette in (LIGHT, DARK):
            for bg_name in ("bg", "surface"):
                ratio = contrast_ratio(palette.text_faint, getattr(palette, bg_name))
                self.assertGreaterEqual(
                    ratio, 4.5, f"{palette.name}.text_faint 对 {bg_name} 仅 {ratio:.2f}:1"
                )

    def test_border_is_visible_but_subtle(self):
        from natscope.theme import DARK, LIGHT

        light = contrast_ratio(LIGHT.border, LIGHT.bg)
        dark = contrast_ratio(DARK.border, DARK.bg)
        self.assertGreaterEqual(light, 1.2, f"浅色边框过淡：{light:.2f}:1")
        self.assertLessEqual(light, 1.7, f"浅色边框过重：{light:.2f}:1")
        self.assertGreaterEqual(dark, 1.3, f"深色边框过淡：{dark:.2f}:1")

    def test_new_optional_fields_have_defaults(self):
        from natscope.theme import LIGHT

        # 只传 v1.0 的 16 个字段也必须能构造出来
        bare = type(LIGHT)(
            name="x",
            bg="#FFFFFF",
            surface="#FFFFFF",
            surface_alt="#FFFFFF",
            border="#EEEEEE",
            text="#000000",
            text_dim="#333333",
            text_faint="#666666",
            accent="#0000FF",
            accent_text="#FFFFFF",
            ok="#008000",
            warn="#804000",
            err="#800000",
            header="#000000",
            header_text="#FFFFFF",
            track="#EEEEEE",
        )
        self.assertIsInstance(bare.shadow, str)
        self.assertIsInstance(bare.overlay, str)
        self.assertIsInstance(bare.grid, str)
        self.assertIsInstance(bare.radius, int)

    def test_typography_scale(self):
        from natscope.theme import Typography

        self.assertEqual(Typography.display, 22)
        self.assertEqual(Typography.title, 16)
        self.assertEqual(Typography.body, 10)
        self.assertEqual(Typography.label, 8)
        self.assertEqual(Typography.mono, 9)
        for key, value in Typography.SCALE.items():
            self.assertIsInstance(value, int, key)
            self.assertGreater(value, 0, key)
        self.assertEqual(Typography.SCALE["display"], Typography.display)

    def test_type_color_mapping(self):
        from natscope.theme import DARK, type_color

        self.assertEqual(type_color(DARK, "full_cone"), DARK.ok)
        self.assertEqual(type_color(DARK, "port_restricted"), DARK.warn)
        self.assertEqual(type_color(DARK, "symmetric"), DARK.err)
        self.assertEqual(type_color(DARK, "who-knows"), DARK.text_dim)


class TestBuildMetadata(unittest.TestCase):
    def test_spec_disables_upx(self):
        text = read_text(os.path.join(ROOT, "natscope.spec"))
        self.assertRegex(text, r"upx=False")
        self.assertNotRegex(text, r"upx=True")

    def test_icons_exist_and_cover_scaling_steps(self):
        from PIL import Image

        ico = os.path.join(ROOT, "assets", "icon.ico")
        png = os.path.join(ROOT, "assets", "icon.png")
        self.assertTrue(os.path.isfile(ico), "缺 assets/icon.ico")
        self.assertTrue(os.path.isfile(png), "缺 assets/icon.png")
        with Image.open(ico) as im:
            sizes = {tuple(s) for s in im.info.get("sizes", ())}
        for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
            self.assertIn((size, size), sizes, f"ico 缺 {size}×{size} 档")
        with Image.open(png) as im:
            self.assertEqual(im.size, (512, 512))


if __name__ == "__main__":
    unittest.main()
