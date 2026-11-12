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

import os
import socket
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from natscope import gateway, nat, stun
from natscope.nat import (
    CLASSIC_FULL_CONE,
    CLASSIC_RESTRICTED,
    CLASSIC_UNKNOWN,
    FILT_ADF,
    FILT_APDF,
    FILT_EIF,
    FILT_UNKNOWN,
    MAP_UNKNOWN,
    NatDetector,
    NatResult,
)
from natscope.servers import ProbeResult, StunServer

PRIMARY_HOST, PRIMARY_PORT = "stun-a.test", 3478
PRIMARY_IP = "192.0.2.1"
SECOND_IP = "192.0.2.2"
OTHER = ("198.51.100.9", 3479)  # 主服务器 OTHER-ADDRESS 宣告的"另一个地址"
PUBLIC_IP, PUBLIC_PORT = "203.0.113.7", 40000


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


class TestFilteringTestOrder(unittest.TestCase):
    """P0 回归：过滤行为测试不得被多目标采样"预热"。"""

    def _run(self):
        fake = SimulatedNat()
        with mock.patch.object(stun, "binding_transaction", fake), mock.patch.object(
            nat, "local_outbound_ip", lambda: ("192.168.1.10", 0)
        ):
            result = NatDetector(timeout=0.2).run(make_servers())
        return fake, result

    def test_other_address_is_not_warmed_before_test2(self):
        fake, _ = self._run()
        self.assertIsNotNone(fake.other_warmed_at_test2, "Test II 根本没有被执行")
        self.assertFalse(
            fake.other_warmed_at_test2,
            "执行 Test II 时 OTHER-ADDRESS 已被预热 —— 过滤测试失效，"
            "会把『端口受限型』误判成『完全锥型』",
        )

    def test_change_request_precedes_other_address_sampling(self):
        fake, _ = self._run()
        first_change = fake.call_index(lambda d, ci, cp: ci or cp)
        first_other = fake.call_index(lambda d, ci, cp: d == OTHER)
        self.assertIsNotNone(first_change, "没有发出任何 CHANGE-REQUEST")
        self.assertIsNotNone(first_other, "没有向 OTHER-ADDRESS 采样")
        self.assertLess(
            first_change,
            first_other,
            f"调用顺序错误：CHANGE-REQUEST 在 #{first_change}，"
            f"向 OTHER-ADDRESS 发包在 #{first_other}；前者必须更早",
        )

    def test_verdict_is_not_full_cone_on_address_restricted_nat(self):
        _, result = self._run()
        self.assertEqual(result.filtering_behavior, FILT_ADF)
        self.assertNotEqual(
            result.filtering_behavior,
            FILT_EIF,
            "地址受限型 NAT 被判成 EIF（放宽过滤）",
        )
        self.assertNotEqual(
            result.classic_type,
            CLASSIC_FULL_CONE,
            "地址受限型 NAT 被判成完全锥型 —— 这正是 P0 的错误结论",
        )
        self.assertEqual(result.classic_type, CLASSIC_RESTRICTED)

    def test_steps_keep_rfc_order(self):
        _, result = self._run()
        titles = [step.name for step in result.steps]
        self.assertLess(
            next(i for i, t in enumerate(titles) if t.startswith("Test II")),
            next(i for i, t in enumerate(titles) if t.startswith("多目标映射采样")),
            f"报告里的步骤顺序应与 RFC 3489 一致，实际：{titles}",
        )


class TestGatewayRobustness(unittest.TestCase):
    """网关探测面对不可信输入必须"降级"而不是"抛异常"。"""

    def test_split_url_rejects_bad_port(self):
        for bad in (
            "http://192.168.1.1:99999/desc.xml",
            "http://192.168.1.1:abc/desc.xml",
            "http://192.168.1.1:0/desc.xml",
            "http://[::1/desc.xml",
            "http:///desc.xml",
        ):
            with self.subTest(url=bad):
                with self.assertRaises(OSError):
                    gateway._split_url(bad)

    def test_fetch_wraps_value_error_into_oserror(self):
        with self.assertRaises(OSError):
            gateway._fetch("http://192.168.1.1:99999/desc.xml", timeout=0.1)

    def test_soap_wraps_value_error_into_oserror(self):
        with self.assertRaises(OSError):
            gateway._soap_get_external_ip(
                "http://192.168.1.1:abc/ctl",
                "urn:schemas-upnp-org:service:WANIPConnection:1",
            )

    def test_probe_upnp_survives_malformed_location(self):
        with mock.patch.object(
            gateway, "_ssdp_discover", lambda timeout=2.0: ["http://192.168.1.1:99999/d.xml"]
        ):
            cap = gateway.probe_upnp("192.168.1.1", timeout=0.1)
        self.assertEqual(cap.name, "UPnP-IGD")
        self.assertFalse(cap.available)
        self.assertEqual(cap.state, "limited")

    def test_probe_upnp_survives_fetch_value_error(self):
        def boom(url, timeout=2.0):
            raise ValueError("port should be numeric")

        with mock.patch.object(gateway, "_ssdp_discover", lambda timeout=2.0: ["http://x/d.xml"]):
            with mock.patch.object(gateway, "_fetch", boom):
                cap = gateway.probe_upnp("192.168.1.1", timeout=0.1)
        self.assertEqual(cap.state, "limited")

    def test_probe_gateway_never_raises(self):
        def boom(*args, **kwargs):
            raise ValueError("源自不可信输入的非法端口")

        no_gateway = gateway.Capability("NAT-PMP")
        no_gateway.state = "absent"
        with mock.patch.object(gateway, "default_gateway", lambda: "192.168.1.1"), \
                mock.patch.object(gateway, "probe_upnp", boom), \
                mock.patch.object(gateway, "probe_natpmp", boom), \
                mock.patch.object(gateway, "probe_pcp", boom):
            rep = gateway.probe_gateway("192.168.1.10", timeout=0.1)
        self.assertEqual(rep.gateway_ip, "192.168.1.1")
        for cap in rep.capabilities:
            self.assertEqual(cap.state, "limited", cap.name)
            self.assertIn("探测异常", cap.detail, cap.name)
            self.assertFalse(cap.available, cap.name)

    def test_probe_gateway_without_gateway_still_reports(self):
        with mock.patch.object(gateway, "default_gateway", lambda: ""), \
                mock.patch.object(gateway, "guess_gateways", lambda ip="": iter(())), \
                mock.patch.object(gateway, "probe_upnp", side_effect=OSError("no route")):
            rep = gateway.probe_gateway("", timeout=0.1)
        self.assertEqual(rep.upnp.state, "limited")
        self.assertEqual(rep.natpmp.state, "absent")
        self.assertEqual(rep.pcp.state, "absent")
        self.assertFalse(rep.any_available)


class TestStunStrictness(unittest.TestCase):
    """撒谎的长度必须变成可诊断的错误，而不是"这台服务器没响应"。"""

    @staticmethod
    def _header(length: int, tid: bytes = b"\x11" * 12) -> bytes:
        return (
            b"\x01\x01" + struct_pack_h(length) + b"\x21\x12\xa4\x42" + tid
        )

    def test_declared_length_longer_than_datagram(self):
        packet = self._header(100) + b"\x00" * 8
        with self.assertRaises(stun.StunError) as ctx:
            stun.parse_message(packet)
        self.assertIn("截断", str(ctx.exception))

    def test_attribute_overruns_body(self):
        body = struct_pack_hh(stun.ATTR_MAPPED_ADDRESS, 40) + b"\x00" * 8
        packet = self._header(len(body)) + body
        with self.assertRaises(stun.StunError) as ctx:
            stun.parse_message(packet)
        self.assertIn("越界", str(ctx.exception))

    def test_wellformed_message_still_parses(self):
        body = struct_pack_hh(stun.ATTR_SOFTWARE, 4) + b"test"
        packet = self._header(len(body)) + body
        msg = stun.parse_message(packet)
        self.assertEqual(msg.attrs[stun.ATTR_SOFTWARE], b"test")

    def test_trailing_slack_is_tolerated(self):
        # 某些实现会在包尾多塞几个字节；UDP 不会把这种包截断，宽容处理即可
        body = struct_pack_hh(stun.ATTR_SOFTWARE, 4) + b"test"
        packet = self._header(len(body)) + body + b"\x00\x00\x00"
        msg = stun.parse_message(packet)
        self.assertEqual(msg.attrs[stun.ATTR_SOFTWARE], b"test")

    def test_binding_transaction_reports_malformed_packet(self):
        packet = self._header(64) + b"\x00" * 4  # 声明 64，实际只有 4

        class FakeSocket:
            def settimeout(self, value):
                pass

            def sendto(self, data, dest):
                pass

            def recvfrom(self, size):
                return packet, ("192.0.2.1", 3478)

        with mock.patch.object(stun, "build_binding_request", lambda *a, **k: (b"", b"\x11" * 12)):
            res = stun.binding_transaction(FakeSocket(), ("192.0.2.1", 3478), timeout=0.2)
        self.assertFalse(res.ok)
        self.assertIn("非法报文", res.error)
        self.assertIn("截断", res.error)


class TestResolveIpv6(unittest.TestCase):
    """IPv6 服务器不支持，但报错必须说清楚"是 IPv6 不支持"，不是"DNS 坏了"。"""

    def test_ipv6_literal_is_reported_clearly(self):
        for host in ("2001:db8::1", "[2001:db8::1]"):
            with self.subTest(host=host):
                with self.assertRaises(OSError) as ctx:
                    stun.resolve(host, 3478)
                message = str(ctx.exception)
                self.assertIn("IPv6", message)
                self.assertNotIn("DNS 解析失败", message)

    def test_ipv4_host_still_resolves(self):
        ip, port = stun.resolve("127.0.0.1", 3478)
        self.assertEqual((ip, port), ("127.0.0.1", 3478))

    def test_ipv4_is_not_mistaken_for_ipv6(self):
        self.assertFalse(stun._is_ipv6_literal("stun.l.google.com"))
        self.assertFalse(stun._is_ipv6_literal("192.0.2.1"))
        self.assertTrue(stun._is_ipv6_literal("::1"))
        self.assertTrue(stun._is_ipv6_literal("[::1]"))


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
    """P1 回归：判定只认结构化 error_kind，不认 error 的措辞。"""

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
        self.assertEqual(res.error_kind, stun.ERR_TIMEOUT)
        self.assertEqual(res.error, "timeout")

    def test_apdf_survives_error_wording_change(self):
        # 措辞故意换掉：判定仍必须落在 APDF，而不是 unknown
        def fake(sock, dest, change_ip=False, change_port=False, timeout=1.5, attempts=1):
            return stun.BindingResult(
                ok=False, error="timed out (1.2s, 2 attempts)", error_kind=stun.ERR_TIMEOUT
            )

        res = NatResult()
        srv = ProbeResult(server=StunServer("stun-a.test", 3478), ok=True, ip=PRIMARY_IP)
        with mock.patch.object(stun, "binding_transaction", fake):
            NatDetector()._filter_tests(object(), srv, res)
        self.assertEqual(res.filtering_behavior, FILT_APDF)

    def test_other_error_kind_does_not_mean_filtered(self):
        # OSError（例如网络不可达）不等于"被 NAT 过滤"，只能是 unknown
        def fake(sock, dest, change_ip=False, change_port=False, timeout=1.5, attempts=1):
            return stun.BindingResult(
                ok=False, error="network unreachable", error_kind=stun.ERR_OSERROR
            )

        res = NatResult()
        srv = ProbeResult(server=StunServer("stun-a.test", 3478), ok=True, ip=PRIMARY_IP)
        with mock.patch.object(stun, "binding_transaction", fake):
            NatDetector()._filter_tests(object(), srv, res)
        self.assertEqual(res.filtering_behavior, FILT_UNKNOWN)

    def test_filter_tests_retry(self):
        seen: list[int] = []

        def fake(sock, dest, change_ip=False, change_port=False, timeout=1.5, attempts=1):
            seen.append(attempts)
            return stun.BindingResult(ok=False, error="timeout", error_kind=stun.ERR_TIMEOUT)

        res = NatResult()
        srv = ProbeResult(server=StunServer("stun-a.test", 3478), ok=True, ip=PRIMARY_IP)
        with mock.patch.object(stun, "binding_transaction", fake):
            NatDetector()._filter_tests(object(), srv, res)
        self.assertEqual(len(seen), 2, "Test II / III 各应发一次事务")
        self.assertTrue(all(a >= 2 for a in seen), f"单包重试缺失：attempts={seen}")


class TestStepBookkeeping(unittest.TestCase):
    """P1-10 回归：Test I 重试成功后不得留下状态互相矛盾的重复步骤。"""

    def test_single_test_one_step_after_failover(self):
        class PrimaryDown(SimulatedNat):
            def __call__(self, sock, dest, change_ip=False, change_port=False,
                         timeout=1.5, attempts=1):
                if not change_ip and not change_port and dest[0] == PRIMARY_IP:
                    return stun.BindingResult(
                        ok=False, error="timeout", error_kind=stun.ERR_TIMEOUT
                    )
                return super().__call__(sock, dest, change_ip, change_port, timeout, attempts)

        fake = PrimaryDown()
        with mock.patch.object(stun, "binding_transaction", fake), mock.patch.object(
            nat, "local_outbound_ip", lambda: ("192.168.1.10", 0)
        ):
            result = NatDetector(timeout=0.2).run(make_servers())

        self.assertEqual(result.primary_server, "stun-b.test:3478", "未切换到备选服务器")
        steps = [s for s in result.steps if s.name == "Test I：获取映射地址"]
        self.assertEqual(len(steps), 1, f"Test I 出现 {len(steps)} 条步骤：{steps}")
        self.assertEqual(steps[0].status, "ok")
        self.assertIn("已改用", steps[0].detail)


def struct_pack_h(value: int) -> bytes:
    import struct

    return struct.pack("!H", value)


def struct_pack_hh(a: int, b: int) -> bytes:
    import struct

    return struct.pack("!HH", a, b)


if __name__ == "__main__":
    unittest.main()
