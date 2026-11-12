# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""命令行入口：无 GUI 也能完成检测与导出。

用法：
    python -m natscope.cli
    python -m natscope.cli --txt out.txt --jpg out.jpg --no-gateway
"""

from __future__ import annotations

import argparse
import sys
import time

from . import __version__
from .config import Config
from .gateway import probe_gateway
from .nat import NatDetector
from .report import ReportContext, default_filename, save_image, save_text
from .servers import ProbeSummary, probe_servers
from .theme import get_palette


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="natscope",
        description="NatScope — NAT 类型检测（RFC 3489 四类型 + RFC 5780 行为模型）",
    )
    ap.add_argument("--txt", nargs="?", const="", default=None, help="导出 TXT 报告，可指定路径")
    ap.add_argument("--jpg", nargs="?", const="", default=None, help="导出 JPG 报告，可指定路径")
    ap.add_argument("--no-gateway", action="store_true", help="跳过 UPnP/NAT-PMP/PCP 探测")
    ap.add_argument("--server", action="append", default=[], help="追加自定义 STUN 服务器，可多次")
    ap.add_argument("--timeout", type=float, default=1.5, help="STUN 请求超时（秒）")
    ap.add_argument("--quiet", action="store_true", help="只输出结论")
    ap.add_argument("--version", action="version", version=f"NatScope {__version__}")
    args = ap.parse_args(argv)

    cfg = Config.load()
    for s in args.server:
        try:
            cfg.add_server(s)
        except ValueError as exc:
            print(f"忽略无效服务器 {s!r}: {exc}", file=sys.stderr)

    servers = cfg.all_servers()
    if not args.quiet:
        print(f"正在探测 {len(servers)} 个 STUN 服务器…")
    t0 = time.time()
    results = probe_servers(servers, timeout=min(args.timeout, 1.2), retry_timeout=args.timeout)
    summary = ProbeSummary(results)
    if not args.quiet:
        print(
            f"探测完成 {time.time() - t0:.1f}s：优选 {len(summary.preferred)} / "
            f"备选 {len(summary.backup)} / 不可用 {len(summary.unavailable)}"
        )
        for r in summary.usable[:5]:
            print(f"  {r.tier_label} {r.server.label} {r.rtt_ms:.0f}ms")

    detector = NatDetector(timeout=args.timeout, log=None if args.quiet else print)
    nat = detector.run(results)

    gw = None if args.no_gateway else probe_gateway(nat.local_ip)

    if args.quiet:
        print(f"{nat.classic_label}")
        print(f"映射: {nat.mapping_label} / 过滤: {nat.filtering_label}")
    else:
        print()
        print("=" * 60)
        print(f"传统类型 : {nat.classic_label}")
        print(f"映射行为 : {nat.mapping_label}")
        print(f"过滤行为 : {nat.filtering_label}")
        print(f"本机     : {nat.local_ip}:{nat.local_port} → 公网 {nat.public_ip}:{nat.public_port}")
        if gw:
            print(f"网关     : {gw.gateway_ip}")
            for cap in gw.capabilities:
                print(f"  {cap.name}: {cap.state_label} {cap.external_ip} {cap.detail}")
        print("-" * 60)
        print(nat.summary)
        print("=" * 60)

    ctx = ReportContext(nat=nat, gateway=gw, probe=summary)
    if args.txt is not None:
        path = args.txt or default_filename("NatScope报告", "txt")
        save_text(ctx, path)
        print(f"TXT 已保存: {path}")
    if args.jpg is not None:
        path = args.jpg or default_filename("NatScope报告", "jpg")
        save_image(ctx, path, get_palette(cfg.theme))
        print(f"JPG 已保存: {path}")

    return 0 if nat.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
