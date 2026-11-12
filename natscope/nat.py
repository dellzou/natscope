"""NAT 类型检测核心。

同时给出两套结论：
  1. RFC 3489 传统四类型（Test I / II / III）
  2. RFC 5780 现代行为模型（映射行为 + 过滤行为）

实现要点（踩过的坑都在这）：
  * 所有测试复用同一个本地 UDP socket，否则映射端口不可比。
  * CHANGE-REQUEST 必须校验**回包源地址是否真的变了**。大量公共 STUN 服务器会
    忽略该属性、仍从原地址回包，若不校验会把"端口受限"误判成"完全锥型"。
  * 对称型必须优先判定（换目的 IP 后映射端口变），不能先套锥型判据。
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field

from . import stun
from .servers import ProbeResult, StunServer

# ---- 传统四类型 ----
CLASSIC_BLOCKED = "blocked"
CLASSIC_OPEN = "open"
CLASSIC_FULL_CONE = "full_cone"
CLASSIC_RESTRICTED = "restricted"
CLASSIC_PORT_RESTRICTED = "port_restricted"
CLASSIC_CONE_UNKNOWN = "cone_unknown"
CLASSIC_SYMMETRIC = "symmetric"
CLASSIC_UNKNOWN = "unknown"

CLASSIC_LABEL = {
    CLASSIC_BLOCKED: "UDP 被阻断",
    CLASSIC_OPEN: "开放互联网（无 NAT）",
    CLASSIC_FULL_CONE: "完全锥型 NAT（Full Cone）",
    CLASSIC_RESTRICTED: "受限锥型 NAT（Restricted Cone）",
    CLASSIC_PORT_RESTRICTED: "端口受限锥型 NAT（Port Restricted Cone）",
    CLASSIC_CONE_UNKNOWN: "锥型 NAT（过滤行为不可测，无法细分）",
    CLASSIC_SYMMETRIC: "对称型 NAT（Symmetric）",
    CLASSIC_UNKNOWN: "无法判定",
}

CLASSIC_EN = {
    CLASSIC_BLOCKED: "Blocked",
    CLASSIC_OPEN: "Open Internet",
    CLASSIC_FULL_CONE: "Full Cone",
    CLASSIC_RESTRICTED: "Restricted Cone",
    CLASSIC_PORT_RESTRICTED: "Port Restricted Cone",
    CLASSIC_CONE_UNKNOWN: "Cone (unclassified)",
    CLASSIC_SYMMETRIC: "Symmetric",
    CLASSIC_UNKNOWN: "Unknown",
}

# ---- 行为模型 ----
MAP_EIM = "eim"
MAP_ADM = "adm"
MAP_APDM = "apdm"
MAP_UNKNOWN = "unknown"

MAP_LABEL = {
    MAP_EIM: "端点无关映射（EIM）",
    MAP_ADM: "地址相关映射（ADM）",
    MAP_APDM: "地址与端口相关映射（APDM）",
    MAP_UNKNOWN: "不可测",
}

FILT_EIF = "eif"
FILT_ADF = "adf"
FILT_APDF = "apdf"
FILT_UNKNOWN = "unknown"

FILT_LABEL = {
    FILT_EIF: "端点无关过滤（EIF）",
    FILT_ADF: "地址相关过滤（ADF）",
    FILT_APDF: "地址与端口相关过滤（APDF）",
    FILT_UNKNOWN: "不可测",
}

# 结论总结文案
SUMMARY = {
    CLASSIC_OPEN: (
        "P2P 可用性：最佳（无需穿透）。本机直接持有公网地址，"
        "P2P 游戏、BT 下载、远程访问、自建服务均可直接互联，无需 STUN / 端口映射。"
    ),
    CLASSIC_FULL_CONE: (
        "P2P 类型应用可用性最佳：P2P 游戏、BT 下载、远程访问等场景表现最优秀。"
        "任何外部主机只要知道你的映射地址即可主动连入，穿透成功率接近 100%，"
        "无需端口映射也能稳定建立直连。"
    ),
    CLASSIC_RESTRICTED: (
        "P2P 可用性：良好。主动发起过连接的对方可以回连，"
        "P2P 游戏、BT、语音通话可正常建立直连；"
        "但未通信过的陌生主机无法主动连入，纯被动式远程访问需要端口映射辅助。"
    ),
    CLASSIC_PORT_RESTRICTED: (
        "P2P 可用性：一般。只有你主动发过包的对端 IP **且** 端口一致时才能回连，"
        "常见 P2P 游戏与 BT 仍可用（依赖双方同时发包），"
        "但部分联机游戏会显示 NAT 类型严格 / 中等，建议开启 UPnP 或手动映射端口。"
    ),
    CLASSIC_SYMMETRIC: (
        "P2P 可用性：较差。每个目的地址分配独立映射端口，对端无法复用同一条通路，"
        "P2P 游戏易出现联机失败、BT 难以获取远端连接、部分语音/视频通话需走中继。"
        "建议：启用 UPnP/PCP 端口映射、使用中继服务，或改用支持端口预测的穿透方案。"
    ),
    CLASSIC_CONE_UNKNOWN: (
        "已确认属于锥型 NAT：映射端口不随目的地址变化，同一端口可复用，"
        "这对 P2P 是利好。但本次没有可用的 STUN 服务器响应 CHANGE-REQUEST，"
        "过滤行为无法测量，因此不能进一步区分完全锥型 / 受限锥型 / 端口受限锥型。"
        "通常此类型下 P2P 游戏、BT 与远程访问多数场景可正常建立连接。"
    ),
    CLASSIC_BLOCKED: (
        "UDP 出站被阻断：所有 STUN 服务器均无响应。"
        "P2P 应用基本不可用，请检查防火墙策略、是否处于强制代理/VPN 环境，"
        "或网络是否仅放行 TCP。"
    ),
    CLASSIC_UNKNOWN: (
        "结论不完整：可用 STUN 服务器不足或均不支持 CHANGE-REQUEST，"
        "仅能给出映射行为，过滤行为与传统四类型无法确定。请添加更多可用服务器后重试。"
    ),
}


@dataclass
class MappingSample:
    dst_ip: str
    dst_port: int
    mapped_ip: str
    mapped_port: int
    server: str

    def __str__(self) -> str:
        return f"{self.dst_ip}:{self.dst_port} -> {self.mapped_ip}:{self.mapped_port}"


@dataclass
class TestStep:
    """单个测试项的执行记录，便于在界面上逐条展示。"""

    name: str
    status: str = "pending"  # pending / ok / fail / skip
    detail: str = ""

    @property
    def icon(self) -> str:
        # 用微软雅黑确定支持的字符：√ × -（U+2714 等符号在中文字体里常缺字形）
        return {"ok": "√", "fail": "×", "skip": "-"}.get(self.status, "·")


@dataclass
class NatResult:
    ok: bool = False
    error: str = ""

    local_ip: str = ""
    local_port: int = 0
    public_ip: str = ""
    public_port: int = 0

    classic_type: str = CLASSIC_UNKNOWN
    mapping_behavior: str = MAP_UNKNOWN
    filtering_behavior: str = FILT_UNKNOWN

    change_request_supported: bool = False
    rfc5780_server: str = ""
    primary_server: str = ""

    samples: list[MappingSample] = field(default_factory=list)
    steps: list[TestStep] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    finished_at: str = ""

    @property
    def classic_label(self) -> str:
        return CLASSIC_LABEL.get(self.classic_type, self.classic_type)

    @property
    def classic_en(self) -> str:
        return CLASSIC_EN.get(self.classic_type, "")

    @property
    def mapping_label(self) -> str:
        return MAP_LABEL.get(self.mapping_behavior, self.mapping_behavior)

    @property
    def filtering_label(self) -> str:
        return FILT_LABEL.get(self.filtering_behavior, self.filtering_behavior)

    @property
    def summary(self) -> str:
        return SUMMARY.get(self.classic_type, SUMMARY[CLASSIC_UNKNOWN])

    @property
    def behind_nat(self) -> bool:
        return self.classic_type not in (CLASSIC_OPEN, CLASSIC_BLOCKED)

    def add_step(self, name: str, status: str, detail: str = "") -> TestStep:
        s = TestStep(name=name, status=status, detail=detail)
        self.steps.append(s)
        return s


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def local_outbound_ip() -> tuple[str, int]:
    """取本机用于访问外网的源 IP（不真的发包）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip, 0


def _mapping_mode(samples: list[MappingSample]) -> str:
    """根据样本推断映射行为。

    判据：
      * 映射端口全部相同              → EIM
      * 存在"只改目的端口"且端口跟着变 → APDM
      * 只存在"只改目的 IP"且端口变    → ADM
      * 变量无法分离（IP 与端口都变）  → 保守归为 APDM
    """
    if len(samples) < 2:
        return MAP_UNKNOWN

    if len({s.mapped_port for s in samples}) == 1:
        return MAP_EIM

    ip_only = False
    port_only = False
    for i in range(len(samples)):
        for j in range(i + 1, len(samples)):
            a, b = samples[i], samples[j]
            if a.mapped_port == b.mapped_port:
                continue
            if a.dst_ip != b.dst_ip and a.dst_port == b.dst_port:
                ip_only = True
            if a.dst_ip == b.dst_ip and a.dst_port != b.dst_port:
                port_only = True

    if port_only:
        return MAP_APDM
    if ip_only:
        return MAP_ADM
    return MAP_APDM


def _classic_from_behavior(mapping: str, filtering: str) -> str:
    if mapping in (MAP_ADM, MAP_APDM):
        return CLASSIC_SYMMETRIC
    if mapping == MAP_EIM:
        if filtering == FILT_EIF:
            return CLASSIC_FULL_CONE
        if filtering == FILT_ADF:
            return CLASSIC_RESTRICTED
        if filtering == FILT_APDF:
            return CLASSIC_PORT_RESTRICTED
        return CLASSIC_CONE_UNKNOWN
    return CLASSIC_UNKNOWN


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


class NatDetector:
    def __init__(self, timeout: float = 1.5, log=None):
        self.timeout = timeout
        self._log = log or (lambda msg: None)

    def log(self, msg: str) -> None:
        self._log(msg)

    def run(
        self,
        servers: list[ProbeResult],
        max_samples: int = 6,
    ) -> NatResult:
        """执行完整检测。servers 建议传入已探测并按优先级排序的结果。"""
        t_start = time.perf_counter()
        res = NatResult()
        res.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")

        usable = [s for s in servers if s.ok]
        if not usable:
            res.error = "没有可用的 STUN 服务器，请检查网络或更换服务器列表"
            res.classic_type = CLASSIC_BLOCKED
            res.add_step("STUN 服务器可用性", "fail", "0 个可用")
            res.duration_ms = (time.perf_counter() - t_start) * 1000
            return res

        # 优先用支持 RFC 5780 的服务器做主服务器，否则用最快的
        usable.sort(key=lambda s: (0 if s.supports_rfc5780 else 1, s.rtt_ms))
        primary = usable[0]
        res.primary_server = primary.server.label

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("", 0))
            _, local_port = sock.getsockname()[:2]
            # bind 到 "" 时 getsockname 返回 0.0.0.0，需另取出口 IP
            local_ip = local_outbound_ip()[0]
            res.local_ip, res.local_port = local_ip, local_port
            self.log(f"本地出口 {local_ip}:{local_port}，主服务器 {primary.server.label}")

            # ---------- Test I ----------
            r1 = self._binding(sock, primary, 1.0)
            if not r1.ok or not r1.mapped:
                res.add_step("Test I：获取映射地址", "fail", r1.error or "无响应")
                res.error = f"主服务器 {primary.server.label} 无有效响应：{r1.error or '超时'}"
                # 换一个服务器再试一次
                for alt in usable[1:4]:
                    r1 = self._binding(sock, alt, 1.2)
                    if r1.ok and r1.mapped:
                        primary = alt
                        res.primary_server = alt.server.label
                        break
                if not (r1.ok and r1.mapped):
                    res.classic_type = CLASSIC_BLOCKED
                    res.add_step("Test I：重试备选服务器", "fail", "全部无响应")
                    res.duration_ms = (time.perf_counter() - t_start) * 1000
                    return res
                res.add_step("Test I：获取映射地址", "ok", f"改用 {primary.server.label}")

            mapped = r1.mapped
            res.public_ip, res.public_port = mapped
            self.log(f"Test I 通过，公网映射 {mapped[0]}:{mapped[1]}（{r1.rtt_ms:.0f} ms）")

            if mapped[0] == local_ip and mapped[1] == local_port:
                res.classic_type = CLASSIC_OPEN
                res.mapping_behavior = MAP_EIM
                res.filtering_behavior = FILT_UNKNOWN
                res.add_step("Test I：获取映射地址", "ok", "映射地址与本地一致 → 无 NAT")
                res.ok = True
                res.duration_ms = (time.perf_counter() - t_start) * 1000
                self.log("映射地址与本地一致，判定为开放互联网")
                return res

            res.add_step(
                "Test I：获取映射地址",
                "ok",
                f"{mapped[0]}:{mapped[1]}（本地 {local_ip}:{local_port}）",
            )
            res.samples.append(MappingSample(primary.ip, primary.server.port,
                                             mapped[0], mapped[1], primary.server.label))

            # ---------- 多服务器映射采样（对称型 / 映射行为） ----------
            others = [s for s in usable[1:] if s.ip != primary.ip][: max_samples - 1]
            # OTHER-ADDRESS 可能是同 IP 不同端口，用于分离"改端口"与"改 IP"
            alt_ports = self._other_address_probes(sock, primary)
            sample_targets = list(alt_ports) + others

            seen_dst = {(primary.ip, primary.server.port)}
            for s in sample_targets:
                dst = (s.ip, s.server.port)
                if dst in seen_dst:
                    continue
                seen_dst.add(dst)
                r = self._binding(sock, s, 1.2)
                if r.ok and r.mapped:
                    res.samples.append(
                        MappingSample(dst[0], dst[1], r.mapped[0], r.mapped[1], s.server.label)
                    )
                    self.log(f"样本 {dst[0]}:{dst[1]} → {r.mapped[0]}:{r.mapped[1]}")
                else:
                    self.log(f"样本 {dst[0]}:{dst[1]} 失败：{r.error or '超时'}")

            mapped_ips = {s.mapped_ip for s in res.samples}
            if len(mapped_ips) > 1:
                res.warnings.append(
                    "检测到多个公网出口 IP（" + "、".join(sorted(mapped_ips)) +
                    "），本机可能处于多 WAN / 负载均衡环境，映射端口的一致性以端口为准"
                )

            distinct = {s.mapped_port for s in res.samples}
            if len(res.samples) >= 2:
                detail = "；".join(str(s) for s in res.samples[:6])
                status = "ok" if len(distinct) == 1 else "ok"
                res.add_step(
                    "多目标映射采样",
                    status,
                    f"{len(res.samples)} 个样本，{len(distinct)} 种映射端口",
                )
                res.notes.append(detail)
            else:
                res.add_step("多目标映射采样", "skip", "可用服务器不足，无法比对映射端口")
                res.warnings.append("不同 IP 的可用 STUN 服务器不足 2 个，映射行为结论可能不准确")

            res.mapping_behavior = _mapping_mode(res.samples)

            # ---------- CHANGE-REQUEST：过滤行为 + Test II / III ----------
            cr_server = self._pick_change_request_server(sock, usable, res)
            if cr_server is None:
                res.add_step("Test II：Change IP+Port", "skip", "无服务器支持 CHANGE-REQUEST")
                res.add_step("Test III：Change Port", "skip", "无服务器支持 CHANGE-REQUEST")
                res.filtering_behavior = FILT_UNKNOWN
                res.warnings.append(
                    "当前可用服务器均未响应 CHANGE-REQUEST，过滤行为与传统四类型无法直接测量"
                )
            else:
                res.change_request_supported = True
                res.rfc5780_server = cr_server.server.label
                self._filter_tests(sock, cr_server, res)

            # ---------- 综合判定 ----------
            self._decide(res)
            res.ok = True
            res.duration_ms = (time.perf_counter() - t_start) * 1000
            self.log(f"判定完成：{res.classic_label}")
            return res
        except OSError as exc:
            res.error = f"网络操作失败：{exc}"
            res.add_step("网络初始化", "fail", str(exc))
            res.duration_ms = (time.perf_counter() - t_start) * 1000
            return res
        finally:
            sock.close()

    # ---- 内部 ----

    def _binding(self, sock, srv: ProbeResult, timeout: float):
        dst = (srv.ip, srv.server.port)
        return stun.binding_transaction(sock, dst, timeout=timeout)

    def _other_address_probes(self, sock, primary: ProbeResult) -> list[ProbeResult]:
        """用主服务器的 OTHER-ADDRESS 造一个"同 IP 不同端口"的采样目标。

        用于区分 ADM 与 APDM：只改目的端口时映射端口是否跟着变。
        """
        out: list[ProbeResult] = []
        r = self._binding(sock, primary, 1.2)
        other = r.other if r.ok else None
        if not other or other == (primary.ip, primary.server.port):
            return out
        # OTHER-ADDRESS 可能是同 IP 不同端口（可测"只改端口"），
        # 也可能是另一个 IP（可测"只改 IP"），两种情况都作为额外样本
        out.append(
            ProbeResult(
                server=StunServer(other[0], other[1]),
                ok=True,
                ip=other[0],
                rtt_ms=0.0,
            )
        )
        return out

    def _pick_change_request_server(self, sock, usable, res: NatResult) -> ProbeResult | None:
        """找一个真正实现了 CHANGE-REQUEST 的服务器。

        探测阶段已标记支持的优先使用；若一个都没有，再对最快的几台做一次实测，
        以 420（Unknown Attribute）或缺少 OTHER-ADDRESS 作为不支持依据。
        """
        known = [s for s in usable if s.supports_rfc5780]
        if known:
            return min(known, key=lambda s: s.rtt_ms)

        for s in sorted(usable, key=lambda x: x.rtt_ms)[:5]:
            r = self._binding(sock, s, 1.0)
            if not r.ok:
                continue
            if r.message and r.message.error_code == 420:
                continue
            if r.other:
                return s
        return None

    def _filter_tests(self, sock, srv: ProbeResult, res: NatResult) -> None:
        """Test II / III：校验回包源地址确实改变，否则该测试无效。"""
        dst_ip, dst_port = srv.ip, srv.server.port

        # Test II：change IP + change Port
        r2 = stun.binding_transaction(
            sock, (dst_ip, dst_port), change_ip=True, change_port=True, timeout=1.5
        )
        ii_valid = r2.ok and r2.source is not None and r2.source[0] != dst_ip
        if r2.ok and not ii_valid:
            res.add_step(
                "Test II：Change IP+Port",
                "skip",
                "服务器忽略 CHANGE-REQUEST（回包源地址未变），测试无效",
            )
        elif ii_valid:
            res.add_step("Test II：Change IP+Port", "ok",
                         f"收到来自 {r2.source[0]}:{r2.source[1]} 的响应")
        else:
            res.add_step("Test II：Change IP+Port", "fail", r2.error or "无响应")

        # Test III：只改端口
        r3 = stun.binding_transaction(
            sock, (dst_ip, dst_port), change_ip=False, change_port=True, timeout=1.5
        )
        iii_valid = r3.ok and r3.source is not None and r3.source[1] != dst_port
        if r3.ok and not iii_valid:
            res.add_step(
                "Test III：Change Port",
                "skip",
                "服务器忽略 CHANGE-REQUEST（回包端口未变），测试无效",
            )
        elif iii_valid:
            res.add_step("Test III：Change Port", "ok",
                         f"收到来自 {r3.source[0]}:{r3.source[1]} 的响应")
        else:
            res.add_step("Test III：Change Port", "fail", r3.error or "无响应")

        if ii_valid:
            res.filtering_behavior = FILT_EIF
        elif iii_valid:
            res.filtering_behavior = FILT_ADF
        elif r2.error == "timeout" and r3.error == "timeout":
            res.filtering_behavior = FILT_APDF
        else:
            res.filtering_behavior = FILT_UNKNOWN

        if not ii_valid and not iii_valid and res.filtering_behavior == FILT_UNKNOWN:
            res.warnings.append("CHANGE-REQUEST 测试未能得出结论，过滤行为标记为不可测")

    def _decide(self, res: NatResult) -> None:
        """综合两套模型给传统四类型。"""
        map_b = res.mapping_behavior
        filt_b = res.filtering_behavior

        # 1) 映射端口随目的变化 → 对称型（优先，避免误判锥型）
        if map_b in (MAP_ADM, MAP_APDM):
            res.classic_type = CLASSIC_SYMMETRIC
            res.add_step("判定：对称型", "ok", f"映射端口随目的变化（{MAP_LABEL[map_b]}）")
            return

        # 2) 明确无 NAT
        if res.public_ip == res.local_ip and res.public_port == res.local_port:
            res.classic_type = CLASSIC_OPEN
            return

        if map_b == MAP_EIM:
            if filt_b == FILT_EIF:
                res.classic_type = CLASSIC_FULL_CONE
                res.add_step("判定：完全锥型", "ok", "EIM + EIF")
            elif filt_b == FILT_ADF:
                res.classic_type = CLASSIC_RESTRICTED
                res.add_step("判定：受限锥型", "ok", "EIM + ADF")
            elif filt_b == FILT_APDF:
                res.classic_type = CLASSIC_PORT_RESTRICTED
                res.add_step("判定：端口受限锥型", "ok", "EIM + APDF")
            else:
                res.classic_type = CLASSIC_CONE_UNKNOWN
                res.add_step(
                    "判定：锥型（未细分）",
                    "skip",
                    "EIM 已确认，过滤行为不可测 → 三种锥型无法区分",
                )
                res.warnings.append(
                    "已确认端点无关映射（EIM），但过滤行为不可测，"
                    "只能确定是锥型（完全/受限/端口受限之一）"
                )
            return

        res.classic_type = CLASSIC_UNKNOWN
        res.add_step("判定：映射行为不可测", "skip", "可用服务器不足")
