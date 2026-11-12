# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""生成 ``assets/icon.ico``（9 档）与 ``assets/icon.png``（512×512）。

用法::

    python tools/make_icons.py            # 输出到 assets/
    python tools/make_icons.py --out-dir D:/tmp/icons

为什么不是简单地从 1024 缩到底：曲线与斜线（圆环、45° 连杆）在高倍缩小
时会塌成灰糊。这里每一档都先按 **4× 目标尺寸** 光栅化，再用 LANCZOS
降到目标尺寸，细笔画才留得住。16/20/24/32/40/48/64/128/256 九档覆盖了
Windows 100% / 125% / 150% / 200% / 250% 的任务栏、标题栏、Alt-Tab 与
文件属性对话框；缺 20 与 40 时 125% / 250% 下会被系统拉伸而发虚。
"""

from __future__ import annotations

import argparse
import io
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

#: ico 需要包含的全部尺寸
ICO_SIZES: tuple[int, ...] = (16, 20, 24, 32, 40, 48, 64, 128, 256)
#: 每档尺寸的超采样倍数
SUPERSAMPLE = 4
#: PNG 主图边长（README、关于对话框、商店素材都用它）
PNG_SIZE = 512
#: 32bpp 位图的 planes / bitcount，写进 ICONDIRENTRY
PLANES = 1
BIT_COUNT = 32


def build_frame(size: int) -> "object":
    """按某一档尺寸渲染图标：先 4× 光栅化，再 LANCZOS 缩小。"""
    from PIL import Image

    from natscope import brand

    big = int(size) * SUPERSAMPLE
    mark = brand.render_mark(big, brand.pick_variant(size))
    if big == size:
        return mark
    return mark.resize((int(size), int(size)), Image.LANCZOS)


def _png_bytes(image: "object") -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def write_ico(frames: dict[int, bytes], path: str) -> None:
    """手写 ICONDIR / ICONDIRENTRY，把每档 PNG 帧原样塞进 ico。

    不使用 Pillow 的 ICO 写出器，是因为它会在缺帧时自己重采样——那样
    精心准备的 4× 渲染成果会被二次缩放糊掉。这里每个字节都来自上一步
    的成品帧。
    """
    sizes = sorted(frames)
    if not sizes:
        raise ValueError("frames 不能为空")
    if max(sizes) > 256:
        raise ValueError("ico 帧最大边长 256")
    count = len(sizes)
    header = struct.pack("<HHH", 0, 1, count)  # reserved / type=1(icon) / count
    offset = 6 + 16 * count
    entries = b""
    blobs = b""
    for size in sizes:
        data = frames[size]
        dim = 0 if size >= 256 else size  # 256 在目录里记为 0
        entries += struct.pack(
            "<BBBBHHII",
            dim,
            dim,
            0,  # 调色板颜色数，0 = 真彩
            0,  # reserved
            PLANES,
            BIT_COUNT,
            len(data),
            offset,
        )
        offset += len(data)
        blobs += data
    with open(path, "wb") as f:
        f.write(header + entries + blobs)


def build_png(path: str) -> None:
    """写 512×512 主图（4× 光栅化后再缩小，边缘更干净）。"""
    from PIL import Image

    from natscope import brand

    big = PNG_SIZE * 2
    mark = brand.render_mark(big, "full")
    mark = mark.resize((PNG_SIZE, PNG_SIZE), Image.LANCZOS)
    mark.save(path, format="PNG", optimize=True)


def verify(ico_path: str, png_path: str) -> None:
    """自检产物：尺寸档齐全、PNG 边长正确、每帧都能解码。"""
    from PIL import Image

    with Image.open(ico_path) as im:
        sizes = {tuple(s) for s in im.info.get("sizes", ())}
        for size in ICO_SIZES:
            want = (size, size)
            if want not in sizes:
                raise AssertionError(f"ico 缺少 {size}×{size} 档，实际：{sorted(sizes)}")
        for size in ICO_SIZES:
            im.size = (size, size)  # ICO 插件支持按尺寸切帧
            im.load()
    with Image.open(png_path) as im:
        if im.size != (PNG_SIZE, PNG_SIZE):
            raise AssertionError(f"png 边长应为 {PNG_SIZE}，实际 {im.size}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 NatScope 图标资产")
    parser.add_argument(
        "--out-dir",
        default=os.path.join(ROOT, "assets"),
        help="输出目录，默认 assets/",
    )
    args = parser.parse_args(argv)
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    frames: dict[int, bytes] = {}
    for size in ICO_SIZES:
        frames[size] = _png_bytes(build_frame(size))
        print(f"  渲染 {size:>3}×{size:<3} ({len(frames[size]):>6} bytes)")

    ico_path = os.path.join(out_dir, "icon.ico")
    png_path = os.path.join(out_dir, "icon.png")
    write_ico(frames, ico_path)
    build_png(png_path)
    verify(ico_path, png_path)

    print(f"ico  -> {ico_path}  ({len(ICO_SIZES)} 档: "
          f"{', '.join(str(s) for s in ICO_SIZES)})")
    print(f"png  -> {png_path}  ({PNG_SIZE}×{PNG_SIZE})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
