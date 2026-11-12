# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""NatScope 测试基建：脚本化 STUN 服务器 + 多通道 NAT 过滤表。

**这个文件不是测试用例**（文件名不匹配 `test*.py`，`discover` 不会收集它），
只给 `test_regression_p0.py` / `test_regression_p1.py` 提供替身。

为什么需要它
------------
NAT 类型判定的关键性质是「**一条 NAT 通道（本地 socket / 内部端口）的过滤表项
只由这条通道上真实发出去的包建立**」。要在 CI 上稳定复现"采样预热污染过滤测试"
这一类缺陷，就必须把这条性质显式建模，而不是真的连 UDP。

本替身把每个本地 socket 当成一条独立通道：

* 通道 A 向 `OTHER-ADDRESS` 发过包 → 只打开 **A** 的白名单，B 依旧干净；
* 服务器的映射端口对所有通道取同一个值（真实 EIM NAT 下不同内部端口会映射到不同
  外部端口，这里刻意简化：映射行为检测只应在一个通道上采样，本套用例不考察该点，
  固定映射端口可以让"mapping == eim"的断言不受实现选择在哪个通道采样的影响）。

过滤模式
--------
* ``"eif"``  完全锥型：任意外部地址可入站
* ``"adf"``  地址受限锥型：只有本通道曾主动发包过的 IP 可入站
* ``"apdf"`` 端口受限锥型：只有本通道曾发包到的 ``IP:端口`` 可入站

⚠️ 隐含前提（改实现时先读这一段）
---------------------------------
本替身假设 **「一个本地 socket = 一个独立内部端口 = 一张独立 NAT 过滤表」**。
真实 NAT 的过滤表项按内部端点（IP:端口）建立，而 `socket.bind(("", 0))` 每次都会
分配到新的内部端口，所以这个假设在真机上成立，也是两阶段架构能成立的基础。

**但它是前提，不是保证**：一旦 `NatDetector` 引入 socket 复用、连接池或"bind 固定
端口"，两条"通道"在真机上就会共享同一张过滤表，而替身仍认为它们互不影响 ——
届时「阶段 1 预热污染阶段 2」这个 bug 会重新出现，而本套用例却依然是绿的。
出现 socket 复用倾向时，必须先把本替身的分组键从 `id(sock)` 改成**本地端口**
（`sock.getsockname()[1]`），否则这些用例提供的是错误的安全感。
"""

from __future__ import annotations

from typing import NamedTuple
from unittest.mock import patch

from natscope import stun
from natscope import nat as natmod
from natscope.nat import NatDetector
from natscope.servers import ProbeResult, StunServer

LOCAL_IP = "192.168.1.50"
MAPPED_IP = "203.0.113.7"

# v1.0.0 的 BindingResult 没有结构化错误类别，取值靠兼容
TIMEOUT_KIND = getattr(stun, "ERR_TIMEOUT", "timeout")


class Call(NamedTuple):
    """一次 `binding_transaction` 调用的记录。"""

    sock: object
    dest: tuple[str, int]
    change_ip: bool
    change_port: bool
    attempts: int

    @property
    def is_change_request(self) -> bool:
        return self.change_ip or self.change_port


class _Channel:
    """一条 NAT 通道（= 一个本地 socket = 一个内部端口 → 一张独立过滤表）。"""

    def __init__(self) -> None:
        self.allowed_ips: set[str] = set()
        self.allowed_endpoints: set[tuple[str, int]] = set()


class FakeStunNetwork:
    """可作 `stun.binding_transaction` 的 `side_effect`。

    参数
    ----
    primary / other
        主服务器地址与它宣告的 ``OTHER-ADDRESS``（None 表示不回带该属性）。
    filter_mode
        ``"eif"`` / ``"adf"`` / ``"apdf"``，见模块 docstring。
    timeout_text
        超时结果的 ``error`` 文案（P1-2 用它验证"措辞不影响判定"）。
    drop_first
        哪几种 CHANGE-REQUEST 的**首个数据包**会丢（模拟偶发丢包，P1-1 用）。
        若实现把重试做进同一次事务（``attempts >= 2``），丢包被同事务吃掉；
        若实现改成外层多次调用（每次 ``attempts == 1``），第二次调用会成功。
        两种实现都能通过，因此它测的是行为而不是实现。
    change_port_supported
        服务器是否实现 change_port。设 False 后：**只改端口**的 CHANGE-REQUEST
        一律超时（模拟"服务器压根不实现这个属性"，而不是 NAT 在过滤）。
        用于 P1-3：区分"双超时 = NAT 在挡"与"双超时 = 服务器不实现"。
    other_unreachable
        服务器**宣告了** OTHER-ADDRESS，但该地址实际不可达（上游丢弃 / 该出口未开放，
        真机上很常见：`stun.miwifi.com` 的 change_ip+port 常年超时）。
        设 True 后：凡回包源 IP 等于 OTHER-ADDRESS 的响应都会被丢弃（表现为超时），
        但"发出过的包"仍会打开本通道的白名单（真实 NAT 就是这样）。
        用途：把 P1-2（措辞）与 P0-1（预热）的变量隔离开 —— 见 `test_regression_p1.py`。
    """

    ALT_IP = "203.0.113.200"  # 无 OTHER-ADDRESS 时，Change-IP 回包的源地址

    def __init__(
        self,
        primary: tuple[str, int],
        other: tuple[str, int] | None = None,
        mapped_port: int = 40001,
        filter_mode: str = "adf",
        timeout_text: str = "timeout",
        drop_first: set[tuple[bool, bool]] | None = None,
        other_unreachable: bool = False,
        change_port_supported: bool = True,
    ) -> None:
        self.primary = tuple(primary)
        self.other = tuple(other) if other else None
        self.mapped_port = mapped_port
        self.filter_mode = filter_mode
        self.timeout_text = timeout_text
        self.other_unreachable = other_unreachable
        self.change_port_supported = change_port_supported
        self._drop_first = {(bool(a), bool(b)) for a, b in (drop_first or set())}
        self._dropped: set[tuple[bool, bool]] = set()
        self.calls: list[Call] = []
        self._channels: dict[int, _Channel] = {}

    # ------------------------------------------------------------------
    # 断言用的视图
    # ------------------------------------------------------------------
    def channel_ids(self) -> list[int]:
        """出现过的通道（socket）id，按首次调用顺序。"""
        seen: list[int] = []
        for call in self.calls:
            if id(call.sock) not in seen:
                seen.append(id(call.sock))
        return seen

    def calls_on(self, sock_id: int) -> list[Call]:
        return [c for c in self.calls if id(c.sock) == sock_id]

    def measure_socket_id(self) -> int | None:
        """测量通道 = **最后一个** CHANGE-REQUEST 所在的 socket。

        为什么取"最后一个"而不是"第一个"：阶段 1 允许先做一次能力探测（`_verify_capability`
        为了确认服务器到底支不支持 CHANGE-REQUEST，本来就要真发一个），那次也会带
        `change_ip=True`，但它发生在探测通道上、目的只是判断"服务器回不回"。
        真正的行为测量是阶段 2，位于整个流程的最后。
        """
        for call in reversed(self.calls):
            if call.is_change_request:
                return id(call.sock)
        return None

    def change_socket_ids(self) -> list[int]:
        """所有发出过 CHANGE-REQUEST 的通道（阶段 1 的能力探测 + 阶段 2 的测量）。"""
        seen: list[int] = []
        for call in self.calls:
            if call.is_change_request and id(call.sock) not in seen:
                seen.append(id(call.sock))
        return seen

    def change_attempts(self) -> list[int]:
        """所有 CHANGE-REQUEST 调用带的 attempts 值，失败时贴进断言信息。"""
        return [c.attempts for c in self.calls if c.is_change_request]

    def trace(self) -> str:
        """调用序列的可读快照，失败时直接贴进断言信息。"""
        return "；".join(
            f"#{i} sock{id(c.sock) % 1000} "
            f"{'CHANGE' if c.is_change_request else 'binding'}"
            f"{' change_ip' if c.change_ip else ''}"
            f"{' change_port' if c.change_port else ''} "
            f"→ {c.dest[0]}:{c.dest[1]} (attempts={c.attempts})"
            for i, c in enumerate(self.calls)
        )

    # ------------------------------------------------------------------
    # 替身本体
    # ------------------------------------------------------------------
    def __call__(
        self,
        sock,
        dest,
        change_ip: bool = False,
        change_port: bool = False,
        timeout: float = 1.5,
        attempts: int = 1,
    ) -> "stun.BindingResult":
        change_ip, change_port = bool(change_ip), bool(change_port)
        dest = tuple(dest)
        channel = self._channel(sock)
        self.calls.append(Call(sock, dest, change_ip, change_port, int(attempts or 1)))

        if not (change_ip or change_port):
            # 包已经发出去了 → 真实 NAT 的白名单一定被写进这张表（与回不回包无关）
            channel.allowed_ips.add(dest[0])
            channel.allowed_endpoints.add(dest)
            if self._unreachable(dest):
                return self._timeout()
            return self._success(source=dest)

        if change_port and not change_ip and not self.change_port_supported:
            # 服务器不实现 change_port：这是"服务器能力"问题，与 NAT 过滤无关
            return self._timeout()

        source = self._change_source(dest, change_ip, change_port)
        if not self._filter_allows(channel, source):
            return self._timeout()
        if self._should_drop(change_ip, change_port, attempts):
            return self._timeout()
        if self._unreachable(source):
            return self._timeout()
        return self._success(source=source)

    # ---- 内部 ----
    def _unreachable(self, addr: tuple[str, int]) -> bool:
        """OTHER-ADDRESS 宣告了但不可达：回包一律收不到（表现为超时）。"""
        return bool(self.other_unreachable and self.other and addr[0] == self.other[0])

    def _channel(self, sock) -> _Channel:
        return self._channels.setdefault(id(sock), _Channel())

    def _change_source(self, dest, change_ip: bool, change_port: bool) -> tuple[str, int]:
        alt_ip = self.other[0] if self.other else self.ALT_IP
        alt_port = self.other[1] if self.other else dest[1] + 1
        return (alt_ip if change_ip else dest[0], alt_port if change_port else dest[1])

    def _filter_allows(self, channel: _Channel, source: tuple[str, int]) -> bool:
        if self.filter_mode == "eif":
            return True
        if self.filter_mode == "adf":
            return source[0] in channel.allowed_ips
        return source in channel.allowed_endpoints

    def _should_drop(self, change_ip: bool, change_port: bool, attempts: int) -> bool:
        """首个数据包丢失的建模：``attempts >= 2`` 说明同事务内还会重发。"""
        kind = (change_ip, change_port)
        if kind not in self._drop_first or kind in self._dropped:
            return False
        self._dropped.add(kind)
        return attempts <= 1

    def _success(self, source: tuple[str, int]) -> "stun.BindingResult":
        r = stun.BindingResult()
        r.ok = True
        r.mapped = (MAPPED_IP, self.mapped_port)
        r.source = tuple(source)
        r.other = self.other
        r.rtt_ms = 20.0
        return r

    def _timeout(self) -> "stun.BindingResult":
        r = stun.BindingResult()
        r.error = self.timeout_text
        if hasattr(r, "error_kind"):  # v1.1 起才有结构化错误类别
            r.error_kind = TIMEOUT_KIND
        return r


def make_probe(ip: str, port: int = 3478, rfc5780: bool = True, rtt: float = 20.0) -> ProbeResult:
    """构造一台"已探测成功"的 STUN 服务器。"""
    return ProbeResult(
        server=StunServer(ip, port),
        ok=True,
        rtt_ms=rtt,
        ip=ip,
        public=(MAPPED_IP, 40001),
        supports_rfc5780=rfc5780,
        tier="preferred",
    )


def run_detector(fake: FakeStunNetwork, probes: list[ProbeResult], max_samples: int = 6):
    """在打桩环境下执行一次完整检测（不产生任何真实 UDP 报文）。

    同时把 `time.sleep` 换成空操作：被测实现为了规避服务器限流会在两次
    CHANGE-REQUEST 之间 sleep（150~250 ms），那是**发送节奏**而不是判定逻辑。
    真实发包已被替身接管，保留 sleep 只会让单测从毫秒级涨到秒级，CI 上纯属浪费。
    """
    detector = NatDetector(timeout=0.05, log=lambda msg: None)
    with patch.object(natmod, "local_outbound_ip", return_value=(LOCAL_IP, 0)), patch.object(
        stun, "binding_transaction", side_effect=fake
    ), patch.object(natmod.time, "sleep", lambda *_a, **_kw: None):
        return detector.run(probes, max_samples=max_samples)
