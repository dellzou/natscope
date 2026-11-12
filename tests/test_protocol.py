"""协议编解码与判定逻辑单元测试（不依赖真实网络）。

运行：python -m unittest discover -s tests -v
"""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from natscope import stun
from natscope.nat import (
    CLASSIC_CONE_UNKNOWN,
    CLASSIC_FULL_CONE,
    CLASSIC_PORT_RESTRICTED,
    CLASSIC_RESTRICTED,
    CLASSIC_SYMMETRIC,
    FILT_ADF,
    FILT_EIF,
    MAP_ADM,
    MAP_APDM,
    MAP_EIM,
    MappingSample,
    _classic_from_behavior,
    _mapping_mode,
)
from natscope.servers import StunServer, parse_server


def make_response(
    tid: bytes,
    mapped_ip: str = "203.0.113.5",
    mapped_port: int = 40961,
    xor: bool = True,
    other: tuple[str, int] | None = None,
    software: str = "",
) -> bytes:
    body = b""
    ip_int = struct.unpack("!I", bytes(int(x) for x in mapped_ip.split(".")))[0]
    if xor:
        xport = mapped_port ^ (stun.MAGIC_COOKIE >> 16)
        xaddr = ip_int ^ stun.MAGIC_COOKIE
        val = b"\x00\x01" + struct.pack("!H", xport) + struct.pack("!I", xaddr)
        body += struct.pack("!HH", stun.ATTR_XOR_MAPPED_ADDRESS, len(val)) + val
    else:
        val = b"\x00\x01" + struct.pack("!H", mapped_port) + struct.pack("!I", ip_int)
        body += struct.pack("!HH", stun.ATTR_MAPPED_ADDRESS, len(val)) + val
    if other:
        oip = struct.unpack("!I", bytes(int(x) for x in other[0].split(".")))[0]
        val = b"\x00\x01" + struct.pack("!H", other[1]) + struct.pack("!I", oip)
        body += struct.pack("!HH", stun.ATTR_OTHER_ADDRESS, len(val)) + val
    if software:
        raw = software.encode()
        pad = (4 - len(raw) % 4) % 4
        body += struct.pack("!HH", stun.ATTR_SOFTWARE, len(raw)) + raw + b"\x00" * pad
    return struct.pack("!HHI", stun.MSG_BINDING_SUCCESS, len(body), stun.MAGIC_COOKIE) + tid + body


class TestStunCodec(unittest.TestCase):
    def test_build_binding_request_basic(self):
        pkt, tid = stun.build_binding_request()
        self.assertEqual(len(pkt), 20)
        self.assertEqual(len(tid), 12)
        mtype, length, cookie = struct.unpack("!HHI", pkt[:8])
        self.assertEqual(mtype, stun.MSG_BINDING_REQUEST)
        self.assertEqual(length, 0)
        self.assertEqual(cookie, stun.MAGIC_COOKIE)

    def test_build_change_request(self):
        pkt, _ = stun.build_binding_request(change_ip=True, change_port=True)
        self.assertEqual(len(pkt), 20 + 8)
        atype, alen = struct.unpack("!HH", pkt[20:24])
        self.assertEqual(atype, stun.ATTR_CHANGE_REQUEST)
        self.assertEqual(alen, 4)
        flag = struct.unpack("!I", pkt[24:28])[0]
        self.assertEqual(flag, stun.CHANGE_IP_FLAG | stun.CHANGE_PORT_FLAG)

    def test_parse_xor_mapped(self):
        tid = os.urandom(12)
        msg = stun.parse_message(make_response(tid, "203.0.113.5", 40961))
        self.assertTrue(msg.is_success)
        self.assertEqual(msg.mapped(), ("203.0.113.5", 40961))

    def test_parse_mapped_fallback(self):
        tid = os.urandom(12)
        msg = stun.parse_message(make_response(tid, "198.51.100.7", 1234, xor=False))
        self.assertEqual(msg.mapped(), ("198.51.100.7", 1234))

    def test_other_address_and_5780_flag(self):
        tid = os.urandom(12)
        raw = make_response(tid, "203.0.113.5", 40961, other=("203.0.113.6", 3479))
        msg = stun.parse_message(raw)
        self.assertEqual(msg.other_address(), ("203.0.113.6", 3479))
        self.assertTrue(msg.supports_change_request())

    def test_no_other_address_means_no_5780(self):
        tid = os.urandom(12)
        msg = stun.parse_message(make_response(tid))
        self.assertFalse(msg.supports_change_request())

    def test_software_attribute(self):
        tid = os.urandom(12)
        msg = stun.parse_message(make_response(tid, software="coturn-4.6"))
        self.assertEqual(msg.software, "coturn-4.6")

    def test_reject_non_stun(self):
        with self.assertRaises(stun.StunError):
            stun.parse_message(b"\x00" * 40)

    def test_reject_short(self):
        with self.assertRaises(stun.StunError):
            stun.parse_message(b"\x00\x01")

    def test_error_code(self):
        tid = os.urandom(12)
        val = b"\x00\x00\x04\x14" + b"Unknown Attribute"
        body = struct.pack("!HH", stun.ATTR_ERROR_CODE, len(val)) + val
        raw = struct.pack("!HHI", stun.MSG_BINDING_ERROR, len(body), stun.MAGIC_COOKIE) + tid + body
        msg = stun.parse_message(raw)
        self.assertEqual(msg.error_code, 420)
        self.assertFalse(msg.supports_change_request())


class TestServerParse(unittest.TestCase):
    def test_plain_host(self):
        s = parse_server("stun.example.com")
        self.assertEqual((s.host, s.port), ("stun.example.com", 3478))

    def test_host_port(self):
        s = parse_server("stun.l.google.com:19302")
        self.assertEqual((s.host, s.port), ("stun.l.google.com", 19302))

    def test_scheme_prefix(self):
        self.assertEqual(parse_server("stun:stun.example.com:3479").port, 3479)
        self.assertEqual(parse_server("stun://stun.example.com").port, 3478)

    def test_ipv6_bracket(self):
        s = parse_server("[2001:db8::1]:3478")
        self.assertEqual(s.host, "2001:db8::1")
        self.assertEqual(s.port, 3478)

    def test_invalid(self):
        for bad in ("", "   ", "host:abc", "host:99999"):
            with self.assertRaises(ValueError):
                parse_server(bad)

    def test_key(self):
        self.assertEqual(StunServer("a.com", 3478).key, "a.com:3478")


class TestMappingBehavior(unittest.TestCase):
    def test_eim(self):
        s = [
            MappingSample("1.1.1.1", 3478, "9.9.9.9", 5000, "a"),
            MappingSample("2.2.2.2", 3478, "9.9.9.9", 5000, "b"),
            MappingSample("3.3.3.3", 19302, "9.9.9.9", 5000, "c"),
        ]
        self.assertEqual(_mapping_mode(s), MAP_EIM)

    def test_adm_when_only_ip_changes(self):
        s = [
            MappingSample("1.1.1.1", 3478, "9.9.9.9", 5000, "a"),
            MappingSample("2.2.2.2", 3478, "9.9.9.9", 5001, "b"),
        ]
        self.assertEqual(_mapping_mode(s), MAP_ADM)

    def test_apdm_when_port_changes(self):
        s = [
            MappingSample("1.1.1.1", 3478, "9.9.9.9", 5000, "a"),
            MappingSample("1.1.1.1", 3479, "9.9.9.9", 5001, "b"),
        ]
        self.assertEqual(_mapping_mode(s), MAP_APDM)

    def test_single_sample_unknown(self):
        self.assertEqual(_mapping_mode([MappingSample("1.1.1.1", 3478, "9.9.9.9", 5000, "a")]), "unknown")


class TestClassicMapping(unittest.TestCase):
    def test_pairs(self):
        self.assertEqual(_classic_from_behavior(MAP_EIM, FILT_EIF), CLASSIC_FULL_CONE)
        self.assertEqual(_classic_from_behavior(MAP_EIM, FILT_ADF), CLASSIC_RESTRICTED)
        self.assertEqual(_classic_from_behavior(MAP_EIM, "apdf"), CLASSIC_PORT_RESTRICTED)
        self.assertEqual(_classic_from_behavior(MAP_EIM, "unknown"), CLASSIC_CONE_UNKNOWN)
        self.assertEqual(_classic_from_behavior(MAP_APDM, FILT_EIF), CLASSIC_SYMMETRIC)
        self.assertEqual(_classic_from_behavior(MAP_ADM, "unknown"), CLASSIC_SYMMETRIC)


class TestDecide(unittest.TestCase):
    """_decide 的分支覆盖——这是绝大多数公共 STUN 环境实际会走到的路径。"""

    def _decide(self, mapping, filtering, public=(("203.0.113.1"), 1234), local=("192.168.1.2", 1234)):
        from natscope.nat import NatDetector, NatResult

        r = NatResult(ok=True, local_ip=local[0], local_port=local[1],
                      public_ip=public[0], public_port=public[1],
                      mapping_behavior=mapping, filtering_behavior=filtering)
        NatDetector()._decide(r)
        return r

    def test_cone_unknown_when_filtering_unmeasurable(self):
        r = self._decide(MAP_EIM, "unknown")
        self.assertEqual(r.classic_type, CLASSIC_CONE_UNKNOWN)

    def test_full_cone(self):
        self.assertEqual(self._decide(MAP_EIM, FILT_EIF).classic_type, CLASSIC_FULL_CONE)

    def test_port_restricted(self):
        self.assertEqual(self._decide(MAP_EIM, "apdf").classic_type, CLASSIC_PORT_RESTRICTED)

    def test_symmetric_wins_over_filtering(self):
        # 映射端口已随目的变化时，即使过滤行为测出 EIF 也必须判对称型
        self.assertEqual(self._decide(MAP_APDM, FILT_EIF).classic_type, CLASSIC_SYMMETRIC)

    def test_open_internet(self):
        r = self._decide(MAP_EIM, "unknown", public=("192.168.1.2", 1234), local=("192.168.1.2", 1234))
        self.assertEqual(r.classic_type, "open")


class TestReport(unittest.TestCase):
    def test_text_report_builds(self):
        from natscope.nat import NatResult
        from natscope.report import ReportContext, build_report_text

        r = NatResult(ok=True, local_ip="192.168.1.2", local_port=5000,
                      public_ip="203.0.113.9", public_port=5000,
                      classic_type=CLASSIC_FULL_CONE, mapping_behavior=MAP_EIM,
                      filtering_behavior=FILT_EIF, primary_server="stun.test:3478")
        r.add_step("Test I", "ok", "203.0.113.9:5000")
        txt = build_report_text(ReportContext(nat=r))
        self.assertIn("完全锥型", txt)
        self.assertIn("203.0.113.9:5000", txt)

    def test_image_report_builds(self):
        from natscope.nat import NatResult
        from natscope.report import ReportContext, render_report_image
        from natscope.theme import DARK, LIGHT

        r = NatResult(ok=True, local_ip="10.0.0.2", local_port=5000,
                      public_ip="203.0.113.9", public_port=40001,
                      classic_type=CLASSIC_SYMMETRIC, mapping_behavior=MAP_APDM,
                      filtering_behavior="apdf", primary_server="stun.test:3478")
        r.add_step("Test I", "ok", "x")
        ctx = ReportContext(nat=r)
        for pal in (LIGHT, DARK):
            img = render_report_image(ctx, pal)
            self.assertGreater(img.size[0], 500)
            self.assertGreater(img.size[1], 400)


if __name__ == "__main__":
    unittest.main()
