# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""NAT 类型检测核心。

同时给出两套结论：
  1. RFC 3489 传统四类型（Test I / II / III）
  2. RFC 5780 现代行为模型（映射行为 + 过滤行为）

实现要点（踩过的坑都在这）：
  * 所有测试复用同一个本地 UDP socket，否则映射端口不可比。
  * CHANGE-REQUEST 必须校验**回包源地址是否真的变了**。大量公共 STUN 服务器会
    忽略该属性、仍从原地址回包，若不校验会把"端口受限"误判成"完全锥型"。
  * **CHANGE-REQUEST 必须排在一切"朝 OTHER-ADDRESS 发包"的动作之前**。先发过
    包 = NAT 里已经有了该 ip:port 的入站许可，Test II 的回包就会被放行，于是
    不放行陌生地址的 NAT 被自己的预热行为测成"完全锥型"。顺序见 run() 内注释。
  * 对称型必须优先判定（换目的 IP 后映射端口变），不能先套锥型判据。
"""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass, field

from . import stun
from .servers import ProbeResult, StunServer

# ---- 过滤行为测量参数（阶段 2） ----
#: 单包 CHANGE-REQUEST 超时（秒）。这是**下限**：无论预算多紧都不压缩它，
#: 否则"没等够"会被记成"被 NAT 过滤"，把地址受限型推成端口受限型。
FILTER_TIMEOUT_S = 1.0
#: 每个子测试最多发几包（服务器对连续 CHANGE-REQUEST 会限流，见 P2-17）
FILTER_ATTEMPTS = 3
#: 同一子测试两次重试之间的间隔（秒）
FILTER_RETRY_GAP_S = 0.15
#: Test II 与 Test III 之间的间隔（秒）：背靠背连发会被服务器丢弃
FILTER_GAP_S = 0.25
#: 目标耗时（秒）。**软预算**：用完只停止"继续重试"，绝不下调单包超时；
#: 已在途的包一律按 FILTER_TIMEOUT_S 等满。实测最坏 ≈4.5s，典型 0.6s。
FILTER_BUDGET_S = 3.0
#: 能力诊断最多试几台服务器
FILTER_CAP_CANDIDATES = 3
#: 能力诊断用的超时（秒）
FILTER_CAP_TIMEOUT_S = 0.8

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


#: 不能当作"公网采样目标"的网段。
#: 注意只列真正的私有/不可路由段——**不要**用 ``ipaddress.is_private``，
#: 它在 3.13 里把 192.0.2.0/24、198.51.100.0/24、203.0.113.0/24 这些文档
#: 保留段也算进去，会把测试夹具与某些真实服务器一起误杀。
_NON_ROUTABLE_NETS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",  # CGNAT
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "224.0.0.0/4",  # 组播
        "255.255.255.255/32",
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
)


def _is_routable(ip: str) -> bool:
    """该地址能否当作"公网采样目标"：排除私网 / 环回 / 链路本地 / CGNAT / 组播。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not any(addr in net for net in _NON_ROUTABLE_NETS if net.version == addr.version)


def _mapping_mode(samples: list[MappingSample]) -> str:
    """根据样本推断映射行为。

    判据：
      * 映射端口全部相同              → EIM
      * 存在"只改目的端口"且端口跟着变 → APDM
      * 只存在"只改目的 IP"且端口变    → ADM
      * 变量无法分离（IP 与端口都变）  → 保守归为 APDM

    **多 WAN 保护**：不同样本的公网出口 IP 不一致时（多 WAN / 负载均衡 / CGNAT
    轮换），比较"映射端口是否相同"没有意义——那是出口不同造成的，不是 NAT 映射
    行为。此时只保留样本数最多的那一个出口 IP 的样本再比对，避免把双线环境误判
    成对称型（实测本机就是这样：OTHER-ADDRESS 走另一条 WAN，映射端口必然不同）。
    """
    if len(samples) < 2:
        return MAP_UNKNOWN

    hosts = [s for s in samples if s.mapped_ip]
    if len({s.mapped_ip for s in hosts}) > 1:
        groups: dict[str, list[MappingSample]] = {}
        for s in hosts:
            groups.setdefault(s.mapped_ip, []).append(s)
        # 取样本最多的出口；并列时取含首个样本（主服务器）的那个
        keep_ip = max(groups, key=lambda ip: (len(groups[ip]), ip == samples[0].mapped_ip))
        samples = groups[keep_ip]
        if len(samples) < 2:
            return MAP_UNKNOWN

    # 多数端口一致即视为 EIM，容许**最多一个离群样本**。
    # 依据：离群值几乎总是测量伪影（例如该目的地址刚被 CHANGE-REQUEST 碰过，
    # NAT 给它重新分配了映射端口），而不是真实的"映射随目的变化"。真对称型
    # 下每个目的各持一个端口、凑不出多数，仍会走到下面的 ADM/APDM 判定。
    port_counts: dict[int, int] = {}
    for s in samples:
        port_counts[s.mapped_port] = port_counts.get(s.mapped_port, 0) + 1
    top_port, top_count = max(port_counts.items(), key=lambda kv: kv[1])
    if top_count >= 2 and top_count >= len(samples) - 1:
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
    """行为模型 → 传统四类型的**纯**映射（无副作用）。

    注意：生产路径走 ``NatDetector._decide()``，它会先判对称型、再判"无 NAT"、
    最后才套行为映射；本函数不处理"无 NAT"这类前置条件，两者在 open 场景下
    结论不同。保留它是因为单元测试需要一份无副作用、可穷举的映射表，改动前
    请先看 ``_decide()``。
    """
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
            switched_from = ""
            if not r1.ok or not r1.mapped:
                res.error = f"主服务器 {primary.server.label} 无有效响应：{r1.error or '超时'}"
                # 换一个服务器再试一次
                for alt in usable[1:4]:
                    r1 = self._binding(sock, alt, 1.2)
                    if r1.ok and r1.mapped:
                        switched_from = primary.server.label
                        primary = alt
                        res.primary_server = alt.server.label
                        break
                if not (r1.ok and r1.mapped):
                    res.classic_type = CLASSIC_BLOCKED
                    res.add_step("Test I：获取映射地址", "fail", "主服务器与备选服务器全部无响应")
                    res.duration_ms = (time.perf_counter() - t_start) * 1000
                    return res

            mapped = r1.mapped
            res.public_ip, res.public_port = mapped
            self.log(f"Test I 通过，公网映射 {mapped[0]}:{mapped[1]}（{r1.rtt_ms:.0f} ms）")

            # 无论是否中途换过服务器，Test I 只写一条步骤：同一步骤同时出现
            # × 和 √ 会让报告里的结论无从判断（审计 P1-10）。
            detail = f"{mapped[0]}:{mapped[1]}（本地 {local_ip}:{local_port}）"
            if switched_from:
                detail += f"；主服务器 {switched_from} 超时，已改用 {primary.server.label}"
            res.add_step("Test I：获取映射地址", "ok", detail)

            if mapped[0] == local_ip and mapped[1] == local_port:
                res.classic_type = CLASSIC_OPEN
                res.mapping_behavior = MAP_EIM
                res.filtering_behavior = FILT_UNKNOWN
                res.add_step("判定：无 NAT", "ok", "映射地址与本地一致")
                res.ok = True
                res.duration_ms = (time.perf_counter() - t_start) * 1000
                self.log("映射地址与本地一致，判定为开放互联网")
                return res

            res.samples.append(MappingSample(primary.ip, primary.server.port,
                                             mapped[0], mapped[1], primary.server.label))

            # ---------- 过滤行为测量（阶段 2：全新 socket，绝不预热） ----------
            #
            # 【顺序与通道都不能动】这段必须排在**任何向 OTHER-ADDRESS / CHANGED-
            # ADDRESS 发包的代码之前**，而且必须跑在**新建的 socket** 上。
            #
            # Test II 的判据是"回包源 IP 是否变成了另一个地址"，而服务器换源地址
            # 后发来的地址正是它宣告的那个 other 地址。只要我们事先朝那个 ip:port
            # 发过哪怕一个 UDP 包，NAT 就会为它建好入站许可（地址受限 / 端口受限
            # 型都会），Test II 的回包被放行 → 判成 EIF → "完全锥型 / P2P 最佳"。
            # 真机对照（stun.miwifi.com，各 5 次）：全新 socket 直接发 Change-IP
            # 5/5 超时；先向 OTHER-ADDRESS 发一个包再发同一请求 5/5 成功。
            cr_server = self._pick_change_request_server(usable)
            if cr_server is None:
                res.add_step("Test II：Change IP+Port", "skip", "无服务器声明支持 CHANGE-REQUEST")
                res.add_step("Test III：Change Port", "skip", "无服务器声明支持 CHANGE-REQUEST")
                res.filtering_behavior = FILT_UNKNOWN
                res.warnings.append(
                    "当前可用服务器均未声明 OTHER-ADDRESS / CHANGED-ADDRESS，"
                    "过滤行为与传统四类型无法直接测量"
                )
            else:
                res.change_request_supported = True
                res.rfc5780_server = cr_server.server.label
                self._measure_filtering(cr_server, usable, res)

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

            # 离群样本复核：某个目的的映射端口与多数不一致时，先重发一次该目的。
            # 这类离群值多为测量伪影（该地址刚被 CHANGE-REQUEST 碰过、NAT 给它
            # 重新分配了端口），直接当成对称型证据会把 EIM 误判成 APDM。
            self._recheck_outlier_samples(sock, res)

            distinct = {s.mapped_port for s in res.samples}
            if len(res.samples) >= 2:
                detail = "；".join(str(s) for s in res.samples[:6])
                # 端口不一致本身是"对称型"的信号，不该和成功一样打 √；
                # "warn" 在两个渲染器里都落到中性色 + "·"，不会误报成失败。
                status = "ok" if len(distinct) == 1 else "warn"
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
        if not _is_routable(other[0]):
            # 实测：stun.freeswitch.org 给 10.10.0.62:443、stun.sonetel.com 给
            # 172.31.9.136:3479（AWS 内网）。拿私网地址当采样目标只会白等 1.2s，
            # 还会把"回包源地址"的预期带偏。
            self.log(f"跳过非公网 OTHER-ADDRESS：{other[0]}:{other[1]}")
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

    def _pick_change_request_server(self, usable) -> ProbeResult | None:
        """挑一台**声明**支持 CHANGE-REQUEST 的服务器（不发任何包）。

        这里只看属性（``supports_rfc5780`` 由探测阶段填好），因此绝不会有
        "顺手把 OTHER-ADDRESS 预热掉"的副作用。真能力验证放在
        :meth:`_prove_change_request_capability`，只在需要区分 APDF 时触发。
        """
        known = [s for s in usable if s.supports_rfc5780]
        if not known:
            return None
        return min(known, key=lambda s: s.rtt_ms)

    def _recheck_outlier_samples(self, sock, res: NatResult) -> None:
        """对"映射端口与多数不一致"的单个离群样本，重发一次该目的。

        重测后与多数一致 → 用新样本替换（说明原值是伪影）；
        仍不一致 → 保留原样本，但记一条提示（此时才可能是真的 APDM）。

        只在存在**多数端口**（≥2 个样本一致）且离群样本唯一时才动手，
        正常 EIM 环境下一次额外的包都不会发。
        """
        if len(res.samples) < 3:
            return
        counts: dict[int, int] = {}
        for s in res.samples:
            counts[s.mapped_port] = counts.get(s.mapped_port, 0) + 1
        top_port, top_count = max(counts.items(), key=lambda kv: kv[1])
        if top_count < 2:
            return
        outliers = [s for s in res.samples if s.mapped_port != top_port]
        if len(outliers) != 1 or counts[outliers[0].mapped_port] != 1:
            return

        target = outliers[0]
        r = stun.binding_transaction(
            sock, (target.dst_ip, target.dst_port), timeout=1.2
        )
        if r.ok and r.mapped and r.mapped[1] == top_port:
            res.samples = [s for s in res.samples if s is not target]
            res.samples.append(
                MappingSample(
                    target.dst_ip, target.dst_port, r.mapped[0], r.mapped[1], target.label
                )
            )
            res.notes.append(
                f"离群样本 {target.dst_ip}:{target.dst_port} 复核后与多数端口一致"
                f"（{target.mapped_port} → {top_port}），原值计为测量伪影"
            )
            self.log(f"离群样本 {target.dst_ip}:{target.dst_port} 复核一致，已采用重测值")
        else:
            res.warnings.append(
                f"{target.dst_ip}:{target.dst_port} 的映射端口 {target.mapped_port} "
                f"与多数（{top_port}）不一致，复核后仍不一致，已保留"
            )

    # ---- 阶段 2：行为测量（全新 socket，绝不预热） ----

    def _measure_filtering(self, srv: ProbeResult, usable, res: NatResult) -> None:
        """在**新建的 socket** 上测 Test II / III。

        为什么必须是新 socket：过滤表是按"本地 socket（内部端口）"记的。若复用
        已经朝 OTHER-ADDRESS 发过包的 socket，Test II 的回包会被那张表的旧条目
        放行，得到假的"完全锥型"。新 socket = 新内部端口 = 干净的过滤表。
        """
        dst = (srv.ip, srv.server.port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("", 0))
            meas_port = sock.getsockname()[1]
            self.log(f"过滤测量通道 本地端口 {meas_port}，目标 {dst[0]}:{dst[1]}")
            # 必须先建映射：CHANGE-REQUEST 的回包要落到这条映射上
            base = stun.binding_transaction(sock, dst, timeout=1.0)
            if not base.ok:
                res.add_step(
                    "Test II：Change IP+Port", "skip",
                    f"新建测量通道后无法建立映射：{base.error or '超时'}",
                )
                res.add_step("Test III：Change Port", "skip", "同上")
                res.filtering_behavior = FILT_UNKNOWN
                res.warnings.append("过滤测量建链失败，过滤行为标记为不可测")
                return

            deadline = time.perf_counter() + FILTER_BUDGET_S
            r2 = self._change_request(sock, dst, True, True, deadline)
            ii_valid = bool(r2.ok and r2.source and r2.source[0] != dst[0])
            if r2.ok and not ii_valid:
                res.add_step(
                    "Test II：Change IP+Port", "skip",
                    "服务器忽略 CHANGE-REQUEST（回包源地址未变），测试无效",
                )
            elif ii_valid:
                res.add_step("Test II：Change IP+Port", "ok",
                             f"收到来自 {r2.source[0]}:{r2.source[1]} 的响应")
            else:
                res.add_step("Test II：Change IP+Port", "fail", r2.error or "无响应")

            # 服务器会对连续的 CHANGE-REQUEST 限流/丢包：实测"同 socket 连发 9 个"
            # 全部超时、"每次单发"24/26 台成功。背靠背发会让 Test III 无辜超时，
            # 把地址受限型推成端口受限型。
            time.sleep(FILTER_GAP_S)

            r3 = self._change_request(sock, dst, False, True, deadline)
            iii_valid = bool(r3.ok and r3.source and r3.source[1] != dst[1])
            if r3.ok and not iii_valid:
                res.add_step(
                    "Test III：Change Port", "skip",
                    "服务器忽略 CHANGE-REQUEST（回包端口未变），测试无效",
                )
            elif iii_valid:
                res.add_step("Test III：Change Port", "ok",
                             f"收到来自 {r3.source[0]}:{r3.source[1]} 的响应")
            else:
                res.add_step("Test III：Change Port", "fail", r3.error or "无响应")
        finally:
            sock.close()

        if ii_valid:
            res.filtering_behavior = FILT_EIF
            return
        if iii_valid:
            res.filtering_behavior = FILT_ADF
            return

        both_timeout = (
            r2.error_kind == stun.ErrorKind.TIMEOUT.value
            and r3.error_kind == stun.ErrorKind.TIMEOUT.value
        )
        if not both_timeout:
            res.filtering_behavior = FILT_UNKNOWN
            res.warnings.append("CHANGE-REQUEST 测试未能得出结论，过滤行为标记为不可测")
            return

        # 两个子测试都超时 —— 到底是 NAT 在过滤，还是服务器根本不实现
        # change_port？只有后者被排除后，才允许判 APDF（端口受限）。
        proven = self._prove_change_request_capability(usable, res)
        if proven:
            res.filtering_behavior = FILT_APDF
            res.notes.append(f"change_port 能力由 {proven} 正向证明，双超时 → APDF")
        else:
            res.filtering_behavior = FILT_UNKNOWN
            res.warnings.append(
                "Change-IP 与 Change-Port 均超时，但没有任何服务器能正向证明自己"
                "会响应 change_port —— 无法区分『NAT 过滤』与『服务器不实现』，"
                "过滤行为仅能确定『比 EIF 更严』"
            )

    def _change_request(self, sock, dst, change_ip: bool, change_port: bool, deadline: float):
        """独立重试一次 CHANGE-REQUEST：最多 FILTER_ATTEMPTS 次，只对超时重试。

        ``deadline`` 是**软预算**：过了它就不再补发重试，但每个真正发出去的包
        都按 ``FILTER_TIMEOUT_S`` 等满。绝不把 `min(timeout, remain)` 这种压缩写
        进来——那会让"预算不够"伪装成"被 NAT 过滤"，方向性错误。
        """
        last: stun.BindingResult | None = None
        for attempt in range(FILTER_ATTEMPTS):
            if last is not None and time.perf_counter() > deadline:
                break  # 软预算用完：不再重发，但已发的包都等满了
            last = stun.binding_transaction(
                sock, dst,
                change_ip=change_ip, change_port=change_port,
                timeout=FILTER_TIMEOUT_S,
                attempts=1,  # 重试放在外层：每次都是新的 transaction id
            )
            if last.ok or last.error_kind != stun.ErrorKind.TIMEOUT.value:
                break
            if attempt < FILTER_ATTEMPTS - 1:
                time.sleep(FILTER_RETRY_GAP_S)
        if last is None:  # 理论上不会发生（首包必定发出）
            last = stun.BindingResult(
                error="未执行：时间预算不足", error_kind=stun.ErrorKind.UNKNOWN.value
            )
        return last

    # ---- 阶段 1：能力诊断（允许预热，只用于解释超时） ----

    def _prove_change_request_capability(self, usable, res: NatResult) -> str:
        """正向证明「某台服务器真的会响应 change_port」，返回服务器标签或空串。

        这里**允许预热**：先向预期的回包源地址发普通 Binding，把该 ip:port 加进
        本机 NAT 白名单，再发 CHANGE-REQUEST。回答的是"服务器有没有这个能力"，
        不是"NAT 放不放行"。绝不能拿这条通道做行为测量，所以每次都新开 socket
        并在结束时关掉。
        """
        candidates = sorted(usable, key=lambda s: (0 if s.supports_rfc5780 else 1, s.rtt_ms))
        for srv in candidates[:FILTER_CAP_CANDIDATES]:
            ok, detail = self._probe_change_port_capability(srv)
            res.notes.append(f"能力诊断 {srv.server.label}：{detail}")
            self.log(f"能力诊断 {srv.server.label}：{detail}")
            if ok:
                return srv.server.label
        return ""

    def _probe_change_port_capability(self, srv: ProbeResult) -> tuple[bool, str]:
        """在一次性 socket 上验证服务器是否会响应 change_port。"""
        dst = (srv.ip, srv.server.port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("", 0))
            base = stun.binding_transaction(sock, dst, timeout=FILTER_CAP_TIMEOUT_S)
            other = base.other if base.ok else None
            if not base.ok:
                return False, f"普通 Binding 无响应（{base.error or '超时'}）"
            if not other:
                return False, "响应未带 OTHER-ADDRESS / CHANGED-ADDRESS"
            # 预热预期回包源：change_port 的回包来自 (dst_ip, other_port)，
            # 顺带把 other 的 IP 也放进白名单，覆盖 ADF / APDF 两种过滤策略
            stun.binding_transaction(sock, other, timeout=FILTER_CAP_TIMEOUT_S)
            stun.binding_transaction(sock, (dst[0], other[1]), timeout=FILTER_CAP_TIMEOUT_S)
            r = stun.binding_transaction(
                sock, dst, change_port=True, timeout=FILTER_CAP_TIMEOUT_S, attempts=2
            )
            if r.ok and r.source and r.source[1] != dst[1]:
                return True, f"change_port 有响应（源 {r.source[0]}:{r.source[1]}）"
            return False, f"change_port 无响应（{r.error or '超时'}）"
        finally:
            sock.close()

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

        # 3) 映射行为测不出来（可用服务器不足），但过滤行为已有结论：
        #    RFC 3489 的经典判据本来就是「Test I + Test II/III」，并不依赖多服务器
        #    映射比对。旧实现只在 map_b == EIM 时才使用 filtering_behavior，于是
        #    "Test II/III 明明都测出了结果，结论却是无法判定"，而 summary 文案还
        #    写着"仅能给出映射行为"——与事实自相矛盾。触发条件很普通：企业网 /
        #    严格防火墙只放行 1~2 台 STUN 服务器。
        #    这里回退到经典三分类，并把前提明确写进警告。
        if map_b == MAP_UNKNOWN and filt_b in (FILT_EIF, FILT_ADF, FILT_APDF):
            classic = {
                FILT_EIF: CLASSIC_FULL_CONE,
                FILT_ADF: CLASSIC_RESTRICTED,
                FILT_APDF: CLASSIC_PORT_RESTRICTED,
            }[filt_b]
            res.classic_type = classic
            res.add_step(
                "判定：经典 Test I/II/III",
                "ok",
                f"{CLASSIC_LABEL[classic]}（映射行为未测）",
            )
            res.warnings.append(
                "可用服务器不足，映射行为未能测量；结论仅基于 RFC 3489 经典 "
                "Test I/II/III 判据。若本机为对称型 NAT 且过滤行为宽松，"
                "锥型结论可能偏乐观，建议补充多台不同 IP 的 STUN 服务器"
            )
            return

        res.classic_type = CLASSIC_UNKNOWN
        res.add_step("判定：映射行为不可测", "skip", "可用服务器不足")
