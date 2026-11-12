# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""底层健壮性回归测试（先红后绿，不依赖真实网络）。

三组不变量，每组都对应一个真机上真出过问题的场景：

1. **过滤测试的执行顺序**：Test II / III 必须排在任何"朝 OTHER-ADDRESS 发包"
   的动作之前。一旦预热，NAT 里就有了该地址的入站许可，Test II 的回包被放行，
   「端口受限型」被自己的预热行为测成「完全锥型」。这里用一台"地址受限型
   NAT"的模拟器把这条顺序钉死——顺序错了结论就会变成 full_cone。
2. **网关探测不得抛异常**：SSDP 的 LOCATION / controlURL 是不可信输入，
   `urlparse(...).port` 抛的是 ValueError，旧实现接不住，会连带把已经算完的
   NAT 结论一起丢弃。
3. **STUN 报文严格性**：声明长度撒谎 / 属性越界必须抛 StunError（由
   binding_transaction 转成可诊断的 error），而不是静默截断当成合法报文。

运行：python -m unittest discover -s tests -v
"""

import logging
import os
import socket
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from natscope import gateway, nat, stun
from natscope.nat import (
    CLASSIC_FULL_CONE,
    CLASSIC_PORT_RESTRICTED,
    CLASSIC_RESTRICTED,
    CLASSIC_UNKNOWN,
    FILT_ADF,
    FILT_APDF,
    FILT_EIF,
    FILT_UNKNOWN,
    MAP_EIM,
    MAP_UNKNOWN,
    NatDetector,
    NatResult,
)
from natscope.servers import ProbeResult, StunServer

try:  # 作为包导入（python -m unittest tests.xxx）
    from .nat_testkit import Call, FakeStunNetwork, make_probe, run_detector
except ImportError:  # discover -s tests 时 tests/ 在 sys.path 上
    from nat_testkit import Call, FakeStunNetwork, make_probe, run_detector  # type: ignore[no-redef]

PRIMARY_HOST, PRIMARY_PORT = "stun-a.test", 3478
PRIMARY_IP = "192.0.2.1"
SECOND_IP = "192.0.2.2"
OTHER = ("198.51.100.9", 3479)  # 主服务器 OTHER-ADDRESS 宣告的"另一个地址"
PUBLIC_IP, PUBLIC_PORT = "203.0.113.7", 40000
#: 与 nat_testkit 的夹具对齐（两阶段架构用例使用）
PRIMARY = (PRIMARY_IP, PRIMARY_PORT)
SECOND = (SECOND_IP, 3478)


def make_servers() -> list[ProbeResult]:
    """两台可用服务器：前者支持 RFC 5780 且更快，必然被选为主服务器。"""
    return [
        ProbeResult(
            server=StunServer(PRIMARY_HOST, PRIMARY_PORT),
            ok=True,
            ip=PRIMARY_IP,
            rtt_ms=10.0,
            supports_rfc5780=True,
        ),
        ProbeResult(
            server=StunServer("stun-b.test", 3478),
            ok=True,
            ip=SECOND_IP,
            rtt_ms=20.0,
            supports_rfc5780=False,
        ),
    ]


class SimulatedNat:
    """一台 NAT 的模拟器，默认是"地址受限型"（RFC 3489 Restricted Cone）。

    规则只有一条，也是真机行为：**只放行我们主动发过包的源 IP**。
    因此"先朝 OTHER 发过包"会让 Test II 莫名其妙地通过——这正是那个 P0。

    ``strict_filtering=False``  改成"完全锥型"：谁来都放行。
    ``advertise_other=False``   服务器不宣告 OTHER-ADDRESS（模拟只有 1 台服务器）。
    """

    def __init__(self, other=OTHER, advertise_other=True, strict_filtering=True):
        self.other = other
        self.advertise_other = advertise_other
        self.strict_filtering = strict_filtering
        self.contacted_ips: set[str] = set()
        self.calls: list[tuple[tuple[str, int], bool, bool]] = []
        self.attempts_seen: list[int] = []
        #: 执行 Test II（change_ip=True）那一刻，OTHER 的 IP 是否已被预热
        self.other_warmed_at_test2: bool | None = None

    def __call__(
        self,
        sock,
        dest,
        change_ip: bool = False,
        change_port: bool = False,
        timeout: float = 1.5,
        attempts: int = 1,
    ) -> stun.BindingResult:
        dest = (dest[0], dest[1])
        self.calls.append((dest, bool(change_ip), bool(change_port)))
        self.attempts_seen.append(int(attempts))
        res = stun.BindingResult(rtt_ms=1.0)

        if change_ip:
            if self.other_warmed_at_test2 is None:
                self.other_warmed_at_test2 = self.other[0] in self.contacted_ips
            if self.strict_filtering and self.other[0] not in self.contacted_ips:
                res.error = "timeout"  # 地址受限型：没主动发过的源 IP 一律丢弃
                res.error_kind = stun.ERR_TIMEOUT
                return res
            res.ok = True
            res.source = self.other
            res.mapped = (PUBLIC_IP, PUBLIC_PORT)
            return res

        if change_port:
            if self.strict_filtering and dest[0] not in self.contacted_ips:
                res.error = "timeout"
                res.error_kind = stun.ERR_TIMEOUT
                return res
            res.ok = True
            res.source = (dest[0], dest[1] + 1)
            res.mapped = (PUBLIC_IP, PUBLIC_PORT)
            return res

        # 普通 Binding：登记"主动发过包的 IP"，这是 NAT 建许可的唯一途径
        self.contacted_ips.add(dest[0])
        res.ok = True
        res.source = dest
        res.mapped = (PUBLIC_IP, PUBLIC_PORT)  # 映射端口恒定 → 映射行为必然是 EIM
        if self.advertise_other and dest == (PRIMARY_IP, PRIMARY_PORT):
            res.other = self.other
        return res

    def call_index(self, pred) -> int | None:
        for i, (dest, ci, cp) in enumerate(self.calls):
            if pred(dest, ci, cp):
                return i
        return None


class TestSamplingBeforeFiltering(unittest.TestCase):
    """铁律：任何 CHANGE-REQUEST 都不得早于任何一次采样完成。

    这是 P0-1 与 P1-18 的共同解。两个方向的坑：
      * 采样先、过滤后（但共用 socket）→ 采样给 OTHER-ADDRESS 开了白名单，
        Test II 回包被放行 → 受限锥型判成"完全锥型"（P0-1）；
      * 过滤先、采样后 → 被 CHANGE-REQUEST 打过的服务器地址族被 NAT 重新
        分配端口 → EIM 判成"对称型"（P1-18）。
    现在采样全部在阶段 1 的 socket 上完成，过滤测量在阶段 2 的独立 socket 上，
    两条约束同时满足。

    注意：NAT 的过滤表是**按 socket（内部端口）记账**的，所以本用例用
    ``FakeStunNetwork`` 的按 socket 隔离模型；用"全局共享白名单"的替身会把
    正常实现的隔离误判成污染。
    """

    def test_sampling_completes_before_any_change_request(self):
        fake = FakeStunNetwork(PRIMARY, other=OTHER, filter_mode="adf")
        probes = [make_probe(*PRIMARY), make_probe(*SECOND, rfc5780=False, rtt=30.0)]
        result = run_detector(fake, probes)

        change_idx = [i for i, c in enumerate(fake.calls) if c.is_change_request]
        sample_idx = [
            i for i, c in enumerate(fake.calls)
            if not c.is_change_request and c.dest != PRIMARY
        ]
        self.assertTrue(change_idx, "没有发出任何 CHANGE-REQUEST：" + fake.trace())
        self.assertTrue(sample_idx, "没有做任何跨目标采样：" + fake.trace())
        self.assertLess(
            max(sample_idx),
            min(change_idx),
            "有采样发生在 CHANGE-REQUEST 之后 —— 该服务器的地址族可能已被扰动，"
            "映射端口会失真（P1-18）。调用序列：" + fake.trace(),
        )
        self.assertEqual(result.filtering_behavior, FILT_ADF)
        self.assertEqual(result.classic_type, CLASSIC_RESTRICTED)

    def test_measure_channel_only_talks_to_primary(self):
        fake = FakeStunNetwork(PRIMARY, other=OTHER, filter_mode="adf")
        probes = [make_probe(*PRIMARY), make_probe(*SECOND, rfc5780=False, rtt=30.0)]
        run_detector(fake, probes)
        measure = fake.measure_socket_id()
        self.assertIsNotNone(measure)
        dests = {c.dest for c in fake.calls_on(measure) if not c.is_change_request}
        self.assertEqual(
            dests,
            {PRIMARY},
            f"测量通道上出现了主服务器以外的目标：{dests}；调用序列：" + fake.trace(),
        )


class TestClassicFallback(unittest.TestCase):
    """P0 回归：过滤行为已测出、映射行为测不出时，不得把已有结论丢掉。"""

    def test_single_server_falls_back_to_classic(self):
        # 只有 1 台可用服务器且它不宣告 OTHER-ADDRESS → 样本 < 2 → 映射行为不可测；
        # 但 Test II 已经成功，结论不该是「无法判定」。
        fake = SimulatedNat(advertise_other=False, strict_filtering=False)
        with mock.patch.object(stun, "binding_transaction", fake), mock.patch.object(
            nat, "local_outbound_ip", lambda: ("192.168.1.10", 0)
        ):
            result = NatDetector(timeout=0.2).run(make_servers()[:1])
        self.assertEqual(result.mapping_behavior, MAP_UNKNOWN)
        self.assertEqual(result.filtering_behavior, FILT_EIF)
        self.assertEqual(result.classic_type, CLASSIC_FULL_CONE)
        self.assertTrue(
            any("经典" in w for w in result.warnings),
            "回退到经典判据时必须把前提写进警告",
        )

    def test_nothing_measured_still_reports_unknown(self):
        res = NatResult(ok=True, local_ip="192.168.1.2", local_port=5000,
                        public_ip="203.0.113.9", public_port=40001,
                        mapping_behavior=MAP_UNKNOWN, filtering_behavior=FILT_UNKNOWN)
        NatDetector()._decide(res)
        self.assertEqual(res.classic_type, CLASSIC_UNKNOWN)


class TestErrorKind(unittest.TestCase):
    """P1-2 回归：判定只认结构化 error_kind，不认 error 的措辞。"""

    def test_timeout_sets_structured_kind(self):
        class SilentSocket:
            def settimeout(self, value):
                pass

            def sendto(self, data, dest):
                pass

            def recvfrom(self, size):
                raise socket.timeout()

        res = stun.binding_transaction(SilentSocket(), ("192.0.2.1", 3478), timeout=0.01)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_kind, stun.ErrorKind.TIMEOUT.value)
        self.assertEqual(res.error, "timeout")

    def test_error_kind_is_str_enum(self):
        # 与旧字符串常量保持可比较，避免外部脚本 / 测试基建被迫改动
        self.assertEqual(stun.ErrorKind.TIMEOUT, "timeout")
        self.assertEqual(stun.ERR_TIMEOUT, "timeout")
        self.assertEqual(stun.ERR_OSERROR, stun.ErrorKind.OS.value)


class TestFilteringArchitecture(unittest.TestCase):
    """两阶段：行为测量必须在干净通道上；能力诊断只在需要时、用一次性通道。"""

    def test_two_phase_layout(self):
        fake = FakeStunNetwork(PRIMARY, other=OTHER, filter_mode="adf")
        probes = [make_probe(*PRIMARY), make_probe(*SECOND, rfc5780=False, rtt=30.0)]
        result = run_detector(fake, probes)

        measure = fake.measure_socket_id()
        self.assertIsNotNone(measure, fake.trace())
        on_measure = fake.calls_on(measure)
        self.assertEqual(on_measure[0].dest, PRIMARY, fake.trace())
        self.assertEqual(
            [c for c in on_measure if not c.is_change_request and c.dest != PRIMARY],
            [],
            "测量通道被其它目标预热过：" + fake.trace(),
        )
        self.assertEqual(result.filtering_behavior, FILT_ADF)
        self.assertEqual(result.classic_type, CLASSIC_RESTRICTED)

    def test_apdf_requires_capability_proof(self):
        fake = FakeStunNetwork(PRIMARY, other=OTHER, filter_mode="apdf")
        result = run_detector(fake, [make_probe(*PRIMARY), make_probe(*SECOND, rfc5780=False)])
        self.assertEqual(result.filtering_behavior, FILT_APDF)
        self.assertEqual(result.classic_type, CLASSIC_PORT_RESTRICTED)

    def test_apdf_not_guessed_without_capability_proof(self):
        fake = FakeStunNetwork(PRIMARY, other=None, filter_mode="apdf")
        result = run_detector(fake, [make_probe(*PRIMARY), make_probe(*SECOND, rfc5780=False)])
        self.assertEqual(result.filtering_behavior, FILT_UNKNOWN)
        self.assertTrue(
            any("change_port 能力" in w or "无法区分" in w for w in result.warnings),
            f"降级时必须说明原因，实际 warnings={result.warnings}",
        )

    def test_retry_recovers_single_dropped_packet(self):
        # 首包被丢：外层重试后仍应测出 Change-IP 成功 → 完全锥型（而非误降级）
        fake = FakeStunNetwork(
            PRIMARY, other=OTHER, filter_mode="eif", drop_first={(True, True)}
        )
        result = run_detector(fake, [make_probe(*PRIMARY), make_probe(*SECOND, rfc5780=False)])
        self.assertEqual(result.filtering_behavior, FILT_EIF)


class TestRoutabilityFilter(unittest.TestCase):
    """P2-16：私网 OTHER-ADDRESS 不能当采样目标。"""

    def test_private_addresses_rejected(self):
        for ip in ("10.10.0.62", "172.31.9.136", "192.168.1.1", "127.0.0.1",
                   "169.254.1.1", "100.64.0.1", "fe80::1"):
            self.assertFalse(nat._is_routable(ip), ip)

    def test_public_addresses_accepted(self):
        # 文档保留段（198.51.100/24、203.0.113/24）是测试夹具常用地址，必须放行：
        # ipaddress.is_private 在 3.13 把这类段也算私有，不能直接用它判定
        for ip in ("111.206.174.2", "198.51.100.9", "203.0.113.9", "8.8.8.8"):
            self.assertTrue(nat._is_routable(ip), ip)


class TestMappingModeMultiWan(unittest.TestCase):
    """多 WAN 下不能拿"不同出口 IP"的映射端口当对称型证据。"""

    def test_multi_wan_is_not_mistaken_for_symmetric(self):
        from natscope.nat import MAP_EIM, MappingSample, _mapping_mode

        samples = [
            MappingSample("198.51.100.1", 3478, "183.236.121.7", 54875, "a"),
            MappingSample("198.51.100.9", 3479, "183.236.121.7", 54875, "b"),
            MappingSample("198.51.100.2", 3478, "112.95.164.56", 54875, "c"),
        ]
        self.assertEqual(_mapping_mode(samples), MAP_EIM)

    def test_real_symmetric_still_detected(self):
        from natscope.nat import MAP_APDM, MappingSample, _mapping_mode

        samples = [
            MappingSample("198.51.100.1", 3478, "203.0.113.7", 40000, "a"),
            MappingSample("198.51.100.1", 3479, "203.0.113.7", 40001, "b"),
        ]
        self.assertEqual(_mapping_mode(samples), MAP_APDM)


class TestGatewayNoSubprocess(unittest.TestCase):
    """C9：正常路径必须 0 次 subprocess（exe 里派生进程是杀软启发式加分项）。"""

    def test_default_gateway_uses_iphelper_without_subprocess(self):
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(args)
            raise AssertionError("正常路径不应派生 subprocess")

        with mock.patch.object(gateway.subprocess, "run", fake_run), \
                mock.patch.object(
                    gateway, "_route_via_iphelper",
                    lambda: ("192.168.1.1", "192.168.1.50")):
            self.assertEqual(gateway.default_gateway(), "192.168.1.1")
        self.assertEqual(calls, [], "正常路径调用了 subprocess")

    def test_subprocess_still_used_as_fallback(self):
        seen = []

        class _Out:
            stdout = b"0.0.0.0 0.0.0.0 10.0.0.1 10.0.0.9 25\n"

        def fake_run(*args, **kwargs):
            seen.append(args)
            return _Out()

        with mock.patch.object(gateway.subprocess, "run", fake_run), \
                mock.patch.object(gateway, "_route_via_iphelper", lambda: ("", "")):
            self.assertEqual(gateway.default_gateway(), "10.0.0.1")
        self.assertTrue(seen, "iphelper 失败时必须回退到子进程路径")

    def test_struct_layout_matches_windows(self):
        # 布局对不上就必须走 fallback，否则会把垃圾字节当网关
        import ctypes

        self.assertEqual(ctypes.sizeof(gateway._MIB_IPFORWARD_ROW2), 104)
        self.assertEqual(ctypes.sizeof(gateway._IP_ADDRESS_PREFIX), 32)


class TestNatpmpSourceValidation(unittest.TestCase):
    """NAT-PMP / PCP 是明文 UDP，不是网关发来的响应必须丢弃。"""

    def _probe(self, addr):
        class _Sock:
            def settimeout(self, value):
                pass

            def sendto(self, data, dest):
                pass

            def recvfrom(self, size):
                return b"\x00\x00\x80\x00" + b"\x00" * 8, addr

            def close(self):
                pass

        with mock.patch.object(gateway.socket, "socket", lambda *a, **k: _Sock()):
            return gateway.probe_natpmp("192.168.1.1", timeout=0.05)

    def test_forged_source_rejected(self):
        cap = self._probe(("192.168.1.99", 5351))
        self.assertEqual(cap.state, "absent")
        self.assertIn("伪造", cap.detail)

    def test_gateway_source_accepted(self):
        cap = self._probe(("192.168.1.1", 5351))
        self.assertNotEqual(cap.state, "absent", cap.detail)


class TestDiagnostics(unittest.TestCase):
    """diag：滚动日志 + 诊断包。"""

    def test_rolling_handler_creates_file(self):
        from natscope import diag

        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "logs", "natscope.log")
            handlers_backup = list(logging.getLogger("natscope").handlers)
            for h in handlers_backup:
                logging.getLogger("natscope").removeHandler(h)
            diag._configured = False
            try:
                logger = diag.setup("DEBUG", path=path)
                logger.info("单元测试写入一行")
                for h in logger.handlers:
                    h.flush()
                self.assertTrue(os.path.isfile(path), "日志文件没有创建")
                body = diag.tail(50, path=path)
                self.assertIn("单元测试写入一行", body)
            finally:
                for h in list(logging.getLogger("natscope").handlers):
                    logging.getLogger("natscope").removeHandler(h)
                    h.close()  # 不关掉句柄，Windows 上临时目录删不掉
                for h in handlers_backup:
                    logging.getLogger("natscope").addHandler(h)
                diag._configured = True

    def test_diagnostics_has_environment_sections(self):
        from natscope import diag

        text = diag.diagnostics()
        for key in ("版本", "许可", "操作系统", "DPI", "默认网关", "最近"):
            self.assertIn(key, text)
        self.assertTrue(text.endswith("\n"))

    def test_save_diagnostics(self):
        from natscope import diag

        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "diag.txt")
            self.assertTrue(diag.save_diagnostics(path))
            with open(path, encoding="utf-8") as f:
                self.assertIn("NatScope 诊断信息", f.read())

    def test_rolling_limits(self):
        from natscope import diag

        self.assertEqual(diag.MAX_BYTES, 1024 * 1024)
        self.assertEqual(diag.BACKUP_COUNT, 1)


class TestParseServerValidation(unittest.TestCase):
    """parse_server 不再静默接受畸形输入。"""

    def test_rejects_malformed(self):
        from natscope.servers import parse_server

        for bad in ("[2001:db8::1]:abc", "host:3478:extra", "a b", "host:", "[::1", "[::1]x"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    parse_server(bad)

    def test_accepts_valid_forms(self):
        from natscope.servers import parse_server

        self.assertEqual(parse_server("[2001:db8::1]:3479").port, 3479)
        self.assertEqual(parse_server("[2001:db8::1]").port, 3478)
        self.assertEqual(parse_server("stun://a.com:3479").key, "a.com:3479")


class TestSingleDnsResolution(unittest.TestCase):
    """每台服务器只解析一次 DNS，且发包用的是同一个 IP。"""

    def test_resolve_called_once(self):
        from natscope.servers import StunServer, _probe_one

        calls = []

        def counting(host, port, family=socket.AF_INET):
            calls.append((host, port))
            return ("192.0.2.1", port)

        sent = []

        def fake_binding(ip, port, timeout=1.5):
            sent.append((ip, port))
            return stun.BindingResult(ok=True, mapped=("203.0.113.7", 40000), rtt_ms=5.0)

        with mock.patch.object(stun, "resolve", counting), \
                mock.patch.object(stun, "binding_to", fake_binding):
            _probe_one(StunServer("stun-x.test", 3478), 0.2)

        self.assertEqual(len(calls), 1, f"DNS 解析了 {len(calls)} 次：{calls}")
        self.assertEqual(sent, [("192.0.2.1", 3478)], "发包目标与解析结果不一致")


class TestStunParserPolicy(unittest.TestCase):
    """parse_message 的不对称策略 + CHANGED-ADDRESS 兼容。"""

    @staticmethod
    def _resp(attrs: bytes, length: int | None = None) -> bytes:
        body = attrs
        return (
            b"\x01\x01"
            + struct_pack_h(len(body) if length is None else length)
            + b"\x21\x12\xa4\x42"
            + b"\x11" * 12
            + body
        )

    def test_duplicate_attribute_keeps_first(self):
        # 两个 SOFTWARE 属性，值等长（5 字节 + 3 字节填充）；必须保留第一个
        body = struct_pack_hh(stun.ATTR_SOFTWARE, 5) + b"first" + b"\x00" * 3
        body += struct_pack_hh(stun.ATTR_SOFTWARE, 5) + b"secon" + b"\x00" * 3
        msg = stun.parse_message(self._resp(body))
        self.assertEqual(msg.attrs[stun.ATTR_SOFTWARE], b"first")

    def test_trailing_extra_bytes_tolerated(self):
        # "收多了"：按 length 解析，多余字节忽略（中间设备 padding 无害）
        body = struct_pack_hh(stun.ATTR_SOFTWARE, 4) + b"test"
        msg = stun.parse_message(self._resp(body) + b"\x00\x00\x00\x00")
        self.assertEqual(msg.attrs[stun.ATTR_SOFTWARE], b"test")

    def test_changed_address_counts_as_change_request_support(self):
        val = b"\x00\x01" + struct_pack_h(3479) + bytes([203, 0, 113, 9])
        body = struct_pack_hh(stun.ATTR_CHANGED_ADDRESS, len(val)) + val
        msg = stun.parse_message(self._resp(body))
        self.assertTrue(msg.supports_change_request(), "CHANGED-ADDRESS 应算作支持依据")
        self.assertEqual(msg.changed_address(), ("203.0.113.9", 3479))
        self.assertEqual(msg.other_address(), ("203.0.113.9", 3479), "other 要能回退到 CHANGED")

    def test_other_address_still_preferred(self):
        val_o = b"\x00\x01" + struct_pack_h(3479) + bytes([198, 51, 100, 9])
        val_c = b"\x00\x01" + struct_pack_h(3479) + bytes([203, 0, 113, 9])
        body = struct_pack_hh(stun.ATTR_OTHER_ADDRESS, len(val_o)) + val_o
        body += struct_pack_hh(stun.ATTR_CHANGED_ADDRESS, len(val_c)) + val_c
        msg = stun.parse_message(self._resp(body))
        self.assertEqual(msg.other_address(), ("198.51.100.9", 3479))


class TestMappingOutlierTolerance(unittest.TestCase):
    """P1-18 回归：单个离群样本不得把 EIM 判成对称型。

    场景：4 个样本映射端口一致，另 1 个目的因刚被 CHANGE-REQUEST 碰过而被 NAT
    重新分配了端口（公网出口 IP 相同）。这是测量伪影，不是"映射随目的变化"。
    """

    @staticmethod
    def _samples(outlier_port: int = 21699):
        from natscope.nat import MappingSample

        return [
            MappingSample("198.51.100.1", 3478, "203.0.113.7", 50000, "a"),
            MappingSample("198.51.100.2", 3478, "203.0.113.7", 50000, "b"),
            MappingSample("198.51.100.3", 3478, "203.0.113.7", 50000, "c"),
            MappingSample("198.51.100.4", 3478, "203.0.113.7", 50000, "d"),
            MappingSample("198.51.100.5", 3479, "203.0.113.7", outlier_port, "e"),
        ]

    def test_single_outlier_is_still_eim(self):
        from natscope.nat import MAP_EIM, _mapping_mode

        self.assertEqual(_mapping_mode(self._samples()), MAP_EIM)

    def test_majority_consistent_is_eim(self):
        from natscope.nat import MAP_EIM, _mapping_mode

        samples = self._samples()[:3]  # 3 一致
        self.assertEqual(_mapping_mode(samples), MAP_EIM)

    def test_true_symmetric_is_not_eim(self):
        from natscope.nat import MAP_APDM, MappingSample, _mapping_mode

        # 每个目的各持一个端口 → 没有多数 → 不可能是 EIM
        samples = [
            MappingSample("198.51.100.1", 3478, "203.0.113.7", 50001, "a"),
            MappingSample("198.51.100.1", 3479, "203.0.113.7", 50002, "b"),
            MappingSample("198.51.100.2", 3478, "203.0.113.7", 50003, "c"),
        ]
        self.assertEqual(_mapping_mode(samples), MAP_APDM)

    def test_decide_not_symmetric_on_single_outlier(self):
        from natscope.nat import (
            CLASSIC_PORT_RESTRICTED,
            FILT_APDF,
            MAP_EIM,
            _mapping_mode,
        )

        res = NatResult(
            ok=True, local_ip="192.168.1.2", local_port=5000,
            public_ip="203.0.113.7", public_port=50000,
            mapping_behavior=_mapping_mode(self._samples()),
            filtering_behavior=FILT_APDF,
        )
        NatDetector()._decide(res)
        self.assertEqual(res.mapping_behavior, MAP_EIM)
        self.assertEqual(res.classic_type, CLASSIC_PORT_RESTRICTED)


class AnomalousSampleNetwork(FakeStunNetwork):
    """让某个采样目的的**首次**回复映射端口 +1（模拟单包抖动 / NAT 表项扰动）。"""

    def __init__(self, *args, anomaly_dst, **kwargs):
        super().__init__(*args, **kwargs)
        self.anomaly_dst = tuple(anomaly_dst)
        self.anomaly_served = False
        self.calls_to_anomaly = 0

    def __call__(self, sock, dest, change_ip=False, change_port=False,
                 timeout=1.5, attempts=1):
        res = super().__call__(sock, dest, change_ip, change_port, timeout, attempts)
        if tuple(dest) == self.anomaly_dst and not (change_ip or change_port):
            self.calls_to_anomaly += 1
            if not self.anomaly_served and res.ok and res.mapped:
                self.anomaly_served = True
                res.mapped = (res.mapped[0], res.mapped[1] + 1)
        return res


class TestOutlierRecheck(unittest.TestCase):
    """双保险：单个抖动样本不得翻转结论，且必须留下复核记录。"""

    def test_single_jittered_sample_is_rechecked_and_dropped(self):
        fake = AnomalousSampleNetwork(PRIMARY, other=OTHER, filter_mode="apdf",
                                      anomaly_dst=OTHER)
        result = run_detector(
            fake, [make_probe(*PRIMARY), make_probe(*SECOND, rfc5780=False, rtt=30.0)]
        )
        self.assertGreaterEqual(
            fake.calls_to_anomaly, 2,
            f"离群目的只被请求 {fake.calls_to_anomaly} 次 —— 没有复核：" + fake.trace(),
        )
        self.assertEqual(
            result.mapping_behavior, MAP_EIM,
            "抖动样本没有被吃掉，映射行为被翻转：" + fake.trace(),
        )
        self.assertEqual(result.classic_type, CLASSIC_PORT_RESTRICTED)
        self.assertTrue(
            any("复核" in n for n in result.notes),
            f"复核动作必须留下记录，实际 notes={result.notes}",
        )

    def test_verdict_does_not_depend_on_which_server_measures(self):
        """换一台服务器做过滤测量，结论不得变化（P1-18 的原始症状）。"""
        primary_first = FakeStunNetwork(PRIMARY, other=OTHER, filter_mode="apdf")
        r1 = run_detector(
            primary_first, [make_probe(*PRIMARY), make_probe(*SECOND, rfc5780=False, rtt=30.0)]
        )
        # 让第二台成为 cr_server（更快且声明支持）
        second_first = FakeStunNetwork(PRIMARY, other=OTHER, filter_mode="apdf")
        r2 = run_detector(
            second_first,
            [make_probe(*PRIMARY, rtt=50.0), make_probe(*SECOND, rfc5780=True, rtt=10.0)],
        )
        self.assertNotEqual(r1.rfc5780_server, r2.rfc5780_server, "两次必须选中不同的测量服务器")
        self.assertEqual(r1.filtering_behavior, r2.filtering_behavior)
        self.assertEqual(r1.classic_type, r2.classic_type)
        self.assertEqual(r1.mapping_behavior, r2.mapping_behavior)


class PerServerCapabilityNetwork(FakeStunNetwork):
    """只有 ``capable_dsts`` 里的服务器会响应 change_port（其余"未实现"）。"""

    def __init__(self, *args, capable_dsts=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.capable_dsts = {tuple(d) for d in capable_dsts}

    def __call__(self, sock, dest, change_ip=False, change_port=False,
                 timeout=1.5, attempts=1):
        if change_port and not change_ip and tuple(dest) not in self.capable_dsts:
            self.calls.append(
                Call(sock, tuple(dest), bool(change_ip), bool(change_port),
                     int(attempts or 1), float(timeout if timeout is not None else 0.0))
            )
            r = stun.BindingResult()
            r.error = self.timeout_text
            r.error_kind = stun.ERR_TIMEOUT
            return r
        return super().__call__(sock, dest, change_ip, change_port, timeout, attempts)


class TestCapabilityProverIsMeasurer(unittest.TestCase):
    """P1-3 收紧：**证明者必须 = 测量者**，不许拿别的服务器的证据背书。

    「A 会响应 change_port」不能证明「B 的两次超时是 NAT 挡的」——B 自己没实现
    change_port 也会两次超时。拿 A 的证据判 B 的 APDF，等于给用户的网络扣一顶
    莫须有的帽子。
    """

    def test_apdf_credit_names_the_measuring_server(self):
        fake = FakeStunNetwork(PRIMARY, other=OTHER, filter_mode="apdf")
        result = run_detector(
            fake, [make_probe(*PRIMARY), make_probe(*SECOND, rfc5780=False, rtt=30.0)]
        )
        self.assertEqual(result.filtering_behavior, FILT_APDF)
        self.assertTrue(
            any("自己正向证明" in n for n in result.notes),
            f"APDF 必须由测量服务器自己证明，notes={result.notes}",
        )
        self.assertEqual(result.rfc5780_server, "192.0.2.1:3478")

    def test_unprovable_measurer_must_switch_to_a_self_proving_server(self):
        # 主服务器不实现 change_port，第二台实现 → 必须换到第二台"自证 + 重测"
        fake = PerServerCapabilityNetwork(
            PRIMARY, other=OTHER, filter_mode="apdf", capable_dsts=[SECOND]
        )
        result = run_detector(
            fake,
            [make_probe(*PRIMARY, rtt=10.0), make_probe(*SECOND, rfc5780=True, rtt=30.0)],
        )
        self.assertEqual(result.filtering_behavior, FILT_APDF)
        credit = [n for n in result.notes if "自己正向证明" in n]
        self.assertTrue(credit, f"缺少自证记录，notes={result.notes}")
        self.assertIn(
            "192.0.2.2:3478", credit[0],
            f"必须由能自证的第二台服务器背书，实际：{credit[0]}",
        )

    def test_no_server_can_self_prove_yields_unknown(self):
        fake = FakeStunNetwork(
            PRIMARY, other=OTHER, filter_mode="apdf", change_port_supported=False
        )
        result = run_detector(
            fake, [make_probe(*PRIMARY), make_probe(*SECOND, rfc5780=True, rtt=30.0)]
        )
        self.assertEqual(result.filtering_behavior, FILT_UNKNOWN)
        self.assertNotEqual(result.filtering_behavior, FILT_APDF)
        self.assertTrue(
            any("没有任何一台" in w for w in result.warnings),
            f"降级必须写明原因，warnings={result.warnings}",
        )


class TestProbeBatchIsolation(unittest.TestCase):
    """单台服务器探测异常不得拖垮整轮探测（`_probe_one_safe`）。"""

    def test_one_server_raising_does_not_break_the_batch(self):
        from natscope import servers as servers_mod
        from natscope.servers import probe_servers

        bad = StunServer("bad.test", 3478)
        good = StunServer("good.test", 3478)

        def fake_probe(server, timeout):
            if server.host == "bad.test":
                raise ValueError("模拟某一台服务器解析出畸形数据")
            res = ProbeResult(server=server, ok=True, ip="198.51.100.7", rtt_ms=5.0)
            return res

        with mock.patch.object(servers_mod, "_probe_one", fake_probe):
            results = probe_servers([bad, good], timeout=0.05, retry_timeout=0.05, workers=2)

        self.assertEqual(len(results), 2, "整轮探测被单台异常打断了")
        by_key = {r.server.key: r for r in results}
        self.assertFalse(by_key["bad.test:3478"].ok)
        self.assertIn("探测异常", by_key["bad.test:3478"].error)
        self.assertTrue(by_key["good.test:3478"].ok, "正常服务器也必须被探测到")


class TestNonRoutableOtherAddress(unittest.TestCase):
    """OTHER/CHANGED 不可路由的服务器没资格当证明者/测量者。

    实测：stun.freeswitch.org 的某些 DNS 实例把 OTHER-ADDRESS 报成
    ``10.10.0.62:443``（另一实例报 143.198.60.79:443，是可用的）。预热打向私网
    地址等于没预热，随后 change_port 必然"无响应"——若当成"服务器不实现"，
    结论就被误降到 unknown，等于让一条 DNS 记录决定用户的 NAT 结论。
    """

    def test_private_other_makes_server_unqualified(self):
        fake = FakeStunNetwork(PRIMARY, other=("10.10.0.62", 443), filter_mode="apdf")
        result = run_detector(
            fake, [make_probe(*PRIMARY), make_probe(*SECOND, rfc5780=False, rtt=30.0)]
        )
        self.assertEqual(result.filtering_behavior, FILT_UNKNOWN)
        self.assertNotEqual(result.filtering_behavior, FILT_APDF)
        self.assertTrue(
            any("不可路由" in w or "无法验证" in w for w in result.warnings),
            f"必须写明是「无法验证」而不是「服务器不实现」，warnings={result.warnings}",
        )

    def test_routable_other_still_proves(self):
        # 对照组：同样的 NAT，但 OTHER 是公网地址 → 能自证 → APDF
        fake = FakeStunNetwork(PRIMARY, other=OTHER, filter_mode="apdf")
        result = run_detector(
            fake, [make_probe(*PRIMARY), make_probe(*SECOND, rfc5780=False, rtt=30.0)]
        )
        self.assertEqual(result.filtering_behavior, FILT_APDF)


class TestReportedServerMatchesMeasurer(unittest.TestCase):
    """报告里“由哪台测得”必须与实际测量者一致，不许自相矛盾。"""

    def test_reported_server_follows_the_switch(self):
        # 主服务器不实现 change_port、第二台实现 → 换台重测后，报告名字也要跟着换
        fake = PerServerCapabilityNetwork(
            PRIMARY, other=OTHER, filter_mode="apdf", capable_dsts=[SECOND]
        )
        result = run_detector(
            fake,
            [make_probe(*PRIMARY, rtt=10.0), make_probe(*SECOND, rfc5780=True, rtt=30.0)],
        )
        self.assertEqual(result.filtering_behavior, FILT_APDF)
        self.assertEqual(
            result.rfc5780_server, "192.0.2.2:3478",
            "结论由第二台测得，报告的 rfc5780_server 却仍是第一台",
        )
        credit = [n for n in result.notes if "自己正向证明" in n]
        self.assertTrue(credit)
        self.assertIn("192.0.2.2:3478", credit[0])


class FirstServerOserrorNetwork(FakeStunNetwork):
    """首台服务器的 CHANGE-REQUEST 返回 OSError（模拟该台此刻不可达）。"""

    def __init__(self, *args, broken=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.broken = {tuple(d) for d in broken}

    def __call__(self, sock, dest, change_ip=False, change_port=False,
                 timeout=1.5, attempts=1):
        if (change_ip or change_port) and tuple(dest) in self.broken:
            self.calls.append(
                Call(sock, tuple(dest), bool(change_ip), bool(change_port),
                     int(attempts or 1), float(timeout if timeout is not None else 0.0))
            )
            r = stun.BindingResult()
            r.error = "network unreachable"
            r.error_kind = stun.ERR_OSERROR
            return r
        return super().__call__(sock, dest, change_ip, change_port, timeout, attempts)


class TestFirstServerFailureDoesNotAbort(unittest.TestCase):
    """P10 回归：首台出现"非超时异常"时必须换下一台，而不是直接判不可测。

    真机症状：同一台机器时而 port_restricted、时而 cone_unknown，取决于 RTT
    排序恰好选中哪台当首台。真值是 port_restricted，等于把可达的精度白丢了。
    """

    def test_oserror_on_first_server_switches_to_next(self):
        fake = FirstServerOserrorNetwork(
            PRIMARY, other=OTHER, filter_mode="apdf", broken=[PRIMARY]
        )
        result = run_detector(
            fake,
            [make_probe(*PRIMARY, rtt=10.0), make_probe(*SECOND, rfc5780=True, rtt=30.0)],
        )
        measured = {c.dest for c in fake.calls if c.is_change_request}
        self.assertIn(SECOND, measured, f"第二台根本没被尝试：{measured}")
        self.assertEqual(
            result.filtering_behavior, FILT_APDF,
            f"首台异常导致整轮放弃 → {result.filtering_behavior!r}；调用序列：" + fake.trace(),
        )
        self.assertEqual(result.classic_type, CLASSIC_PORT_RESTRICTED)

    def test_all_servers_broken_reports_unknown_with_reason(self):
        fake = FirstServerOserrorNetwork(
            PRIMARY, other=OTHER, filter_mode="apdf", broken=[PRIMARY, SECOND]
        )
        result = run_detector(
            fake,
            [make_probe(*PRIMARY, rtt=10.0), make_probe(*SECOND, rfc5780=True, rtt=30.0)],
        )
        self.assertEqual(result.filtering_behavior, FILT_UNKNOWN)
        self.assertTrue(
            any("都无法完成" in w or "不可测" in w for w in result.warnings),
            f"全不可用时必须说明原因，warnings={result.warnings}",
        )


class TestReportStepOrder(unittest.TestCase):
    """展示层：报告步骤按 RFC 3489 叙事排序，不泄漏内部执行顺序。"""

    def test_steps_follow_rfc_narrative(self):
        fake = FakeStunNetwork(PRIMARY, other=OTHER, filter_mode="apdf")
        result = run_detector(
            fake, [make_probe(*PRIMARY), make_probe(*SECOND, rfc5780=False, rtt=30.0)]
        )
        names = [s.name for s in result.steps]
        order = ["Test I", "Test II", "Test III", "多目标映射采样", "判定"]
        positions = []
        for prefix in order:
            idx = next((i for i, n in enumerate(names) if n.startswith(prefix)), None)
            self.assertIsNotNone(idx, f"缺少步骤 {prefix}：{names}")
            positions.append(idx)
        self.assertEqual(
            positions, sorted(positions),
            f"步骤展示顺序应遵循 RFC 叙事 {order}，实际：{names}",
        )

    def test_unknown_step_names_keep_relative_order_and_go_last(self):
        from natscope.nat import NatResult, _reorder_steps

        res = NatResult()
        res.add_step("多目标映射采样", "ok", "")
        res.add_step("自定义步骤甲", "ok", "")
        res.add_step("Test II：Change IP+Port", "fail", "")
        res.add_step("自定义步骤乙", "ok", "")
        res.add_step("Test I：获取映射地址", "ok", "")
        _reorder_steps(res)
        self.assertEqual(
            [s.name for s in res.steps],
            ["Test I：获取映射地址", "Test II：Change IP+Port",
             "多目标映射采样", "自定义步骤甲", "自定义步骤乙"],
        )


def struct_pack_h(value: int) -> bytes:
    import struct

    return struct.pack("!H", value)


def struct_pack_hh(a: int, b: int) -> bytes:
    import struct

    return struct.pack("!HH", a, b)


if __name__ == "__main__":
    unittest.main()
