# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""STUN 协议实现（RFC 5389，向下兼容 RFC 3489）。

只做 NAT 类型探测需要的那一小部分：Binding Request / Response、
XOR-MAPPED-ADDRESS、MAPPED-ADDRESS、CHANGE-REQUEST、RESPONSE-ORIGIN、OTHER-ADDRESS。

全部使用标准库，避免引入任何运行时依赖。
"""

from __future__ import annotations

import os
import socket
import struct
import time
from dataclasses import dataclass, field
from enum import Enum

MAGIC_COOKIE = 0x2112A442

MSG_BINDING_REQUEST = 0x0001
MSG_BINDING_SUCCESS = 0x0101
MSG_BINDING_ERROR = 0x0111

ATTR_MAPPED_ADDRESS = 0x0001
ATTR_CHANGE_REQUEST = 0x0003
ATTR_SOURCE_ADDRESS = 0x0004  # RFC 3489 旧字段
ATTR_CHANGED_ADDRESS = 0x0005  # RFC 3489 旧字段
ATTR_MESSAGE_INTEGRITY = 0x0008
ATTR_ERROR_CODE = 0x0009
ATTR_XOR_MAPPED_ADDRESS = 0x0020
ATTR_SOFTWARE = 0x8022
ATTR_RESPONSE_ORIGIN = 0x802B
ATTR_OTHER_ADDRESS = 0x802C

# CHANGE-REQUEST 标志位
CHANGE_IP_FLAG = 0x04
CHANGE_PORT_FLAG = 0x02

ATTR_NAMES = {
    ATTR_MAPPED_ADDRESS: "MAPPED-ADDRESS",
    ATTR_CHANGE_REQUEST: "CHANGE-REQUEST",
    ATTR_SOURCE_ADDRESS: "SOURCE-ADDRESS",
    ATTR_CHANGED_ADDRESS: "CHANGED-ADDRESS",
    ATTR_MESSAGE_INTEGRITY: "MESSAGE-INTEGRITY",
    ATTR_ERROR_CODE: "ERROR-CODE",
    ATTR_XOR_MAPPED_ADDRESS: "XOR-MAPPED-ADDRESS",
    ATTR_SOFTWARE: "SOFTWARE",
    ATTR_RESPONSE_ORIGIN: "RESPONSE-ORIGIN",
    ATTR_OTHER_ADDRESS: "OTHER-ADDRESS",
}


class StunError(Exception):
    """STUN 报文非法或不可解析。"""


class ErrorKind(str, Enum):
    """``BindingResult.error_kind`` 的取值。

    判定逻辑**只认这个枚举**，不认 ``error`` 里那句给人看的描述——否则哪天
    把 "timeout" 改成 "超时(1.2s)"，APDF 分支就会永久失效且无人察觉。
    继承 ``str`` 是为了和 v1.0 里那些字符串常量保持可比较。
    """

    TIMEOUT = "timeout"
    DNS = "dns"
    OS = "oserror"
    NON_STUN = "non_stun"
    UNKNOWN = "unknown"


# 兼容旧调用点（历史常量，值与 ErrorKind 一致）
ERR_TIMEOUT = ErrorKind.TIMEOUT.value
ERR_DNS = ErrorKind.DNS.value
ERR_OSERROR = ErrorKind.OS.value
ERR_STUN = ErrorKind.NON_STUN.value
ERR_UNKNOWN = ErrorKind.UNKNOWN.value


@dataclass
class StunMessage:
    msg_type: int
    transaction_id: bytes
    attrs: dict = field(default_factory=dict)
    raw: bytes = b""

    @property
    def is_success(self) -> bool:
        return self.msg_type == MSG_BINDING_SUCCESS

    @property
    def error_code(self) -> int:
        """完整错误号（class*100 + number），无 ERROR-CODE 时为 0。"""
        v = self.attrs.get(ATTR_ERROR_CODE)
        if not v or len(v) < 4:
            return 0
        return (v[2] & 0x07) * 100 + v[3]

    @property
    def error_reason(self) -> str:
        v = self.attrs.get(ATTR_ERROR_CODE)
        if not v or len(v) < 5:
            return ""
        return v[4:].decode("utf-8", "replace")

    @property
    def software(self) -> str:
        v = self.attrs.get(ATTR_SOFTWARE)
        return v.decode("utf-8", "replace").strip("\x00") if v else ""

    def mapped(self) -> tuple[str, int] | None:
        """优先取 XOR-MAPPED-ADDRESS，回退 MAPPED-ADDRESS。"""
        v = self.attrs.get(ATTR_XOR_MAPPED_ADDRESS)
        if v:
            return _decode_xor_address(v, self.transaction_id)
        v = self.attrs.get(ATTR_MAPPED_ADDRESS)
        if v:
            return _decode_address(v)
        return None

    def other_address(self) -> tuple[str, int] | None:
        """OTHER-ADDRESS；RFC 3489 时代的服务器只给 CHANGED-ADDRESS，一并兼容。"""
        return self.other or self.changed_address()

    @property
    def other(self) -> tuple[str, int] | None:
        v = self.attrs.get(ATTR_OTHER_ADDRESS)
        return _decode_address(v) if v else None

    def changed_address(self) -> tuple[str, int] | None:
        """CHANGED-ADDRESS（0x0005）：RFC 3489 里与 OTHER-ADDRESS 等价的字段。

        现网 91 台里 12 台给 OTHER-ADDRESS、另有 38 台只给 CHANGED-ADDRESS，
        只认前者会把一半可用候选挡在外面。
        """
        v = self.attrs.get(ATTR_CHANGED_ADDRESS)
        return _decode_address(v) if v else None

    def response_origin(self) -> tuple[str, int] | None:
        v = self.attrs.get(ATTR_RESPONSE_ORIGIN)
        return _decode_address(v) if v else None

    def supports_change_request(self) -> bool:
        """能否根据响应判断服务器支持 CHANGE-REQUEST。"""
        return _has_change_request_support(self)


def _has_change_request_support(msg: "StunMessage") -> bool:
    """CHANGE-REQUEST 能力判据（**唯一的判定点**，改判据只改这里）。

    420（Unknown Attribute）明确表示不支持；否则只要响应里声明了用于"另一个
    地址/端口"的属性就算候选：

    * ``OTHER-ADDRESS``(0x802C)   —— RFC 5780
    * ``CHANGED-ADDRESS``(0x0005) —— RFC 3489 等价字段

    注意这只是**属性层面的候选**，不代表服务器真的会回包。实际测量前应由
    ``natscope.nat.NatDetector`` 用「预热一次性 socket」做能力验证：现网 50 台
    声明支持的服务器里有 13 台根本不响应 CHANGE-REQUEST，直接用会得出假的
    「端口受限」结论。
    """
    if msg.error_code == 420:
        return False
    return ATTR_OTHER_ADDRESS in msg.attrs or ATTR_CHANGED_ADDRESS in msg.attrs


def _decode_address(value: bytes) -> tuple[str, int] | None:
    """MAPPED-ADDRESS / RESPONSE-ORIGIN / OTHER-ADDRESS 通用解码（明文）。"""
    if len(value) < 8:
        return None
    family = value[1]
    port = struct.unpack("!H", value[2:4])[0]
    if family == 0x01 and len(value) >= 8:
        return (".".join(str(b) for b in value[4:8]), port)
    if family == 0x02 and len(value) >= 20:
        raw = value[4:20]
        try:
            host = socket.inet_ntop(socket.AF_INET6, raw)
        except OSError:
            return None
        return (host, port)
    return None


def _decode_xor_address(value: bytes, tid: bytes) -> tuple[str, int] | None:
    if len(value) < 8:
        return None
    family = value[1]
    xport = struct.unpack("!H", value[2:4])[0]
    port = xport ^ (MAGIC_COOKIE >> 16)
    if family == 0x01 and len(value) >= 8:
        xaddr = struct.unpack("!I", value[4:8])[0]
        addr = xaddr ^ MAGIC_COOKIE
        return (socket.inet_ntoa(struct.pack("!I", addr)), port)
    if family == 0x02 and len(value) >= 20:
        mask = struct.pack("!I", MAGIC_COOKIE) + tid
        raw = bytes(a ^ b for a, b in zip(value[4:20], mask))
        try:
            return (socket.inet_ntop(socket.AF_INET6, raw), port)
        except OSError:
            return None
    return None


def build_binding_request(
    change_ip: bool = False,
    change_port: bool = False,
    transaction_id: bytes | None = None,
) -> tuple[bytes, bytes]:
    """构造 Binding Request，返回 (报文, transaction_id)。"""
    tid = transaction_id or os.urandom(12)
    body = b""
    if change_ip or change_port:
        flag = 0
        if change_ip:
            flag |= CHANGE_IP_FLAG
        if change_port:
            flag |= CHANGE_PORT_FLAG
        # CHANGE-REQUEST 固定 4 字节，低 2 位为标志
        body += struct.pack("!HH", ATTR_CHANGE_REQUEST, 4) + struct.pack("!I", flag)
    header = struct.pack("!HHI", MSG_BINDING_REQUEST, len(body), MAGIC_COOKIE) + tid
    return header + body, tid


def parse_message(data: bytes) -> StunMessage:
    """解析 STUN 报文；非 STUN 数据或长度自相矛盾时抛 StunError。

    严格性的理由：UDP 不会把被截断的数据报交给应用层（截断数据报会被内核
    直接丢弃），所以"收到的包比它自己声明的短"只可能是对端造错、或是伪造/
    混淆包。旧实现遇到这种情况会静默 break，把残缺报文当成合法报文返回，
    于是故障表现为"这台服务器没响应"，排查时完全看不出是报文坏了。
    """
    if len(data) < 20:
        raise StunError("报文过短")
    msg_type, length, cookie = struct.unpack("!HHI", data[:8])
    if cookie != MAGIC_COOKIE:
        raise StunError("magic cookie 不匹配，非 RFC 5389 报文")
    if msg_type & 0xC000:
        raise StunError("type 高两位必须为 0")
    body = data[20 : 20 + length]
    if len(body) < length:
        raise StunError(f"报文被截断：声明 {length} 字节，实际只有 {len(body)} 字节")
    tid = data[8:20]
    attrs: dict[int, bytes] = {}
    off = 0
    while off + 4 <= len(body):
        atype, alen = struct.unpack("!HH", body[off : off + 4])
        off += 4
        if off + alen > len(body):
            raise StunError(
                f"属性 0x{atype:04X} 越界：声明 {alen} 字节，剩余仅 {len(body) - off} 字节"
            )
        # 重复属性取**第一个**：RFC 5389 对 COMPREHENSION-REQUIRED 属性的通行
        # 做法，也让被中间设备追加在后面的"影子属性"无法覆盖服务器真正给的值
        attrs.setdefault(atype, body[off : off + alen])
        off += (alen + 3) & ~3
    return StunMessage(msg_type=msg_type, transaction_id=tid, attrs=attrs, raw=data)


@dataclass
class BindingResult:
    """一次 Binding 事务的结果。"""

    ok: bool = False
    mapped: tuple[str, int] | None = None
    source: tuple[str, int] | None = None  # 实际回包的源地址
    origin: tuple[str, int] | None = None
    other: tuple[str, int] | None = None
    rtt_ms: float = 0.0
    error_code: int = 0
    error_reason: str = ""
    error: str = ""  # 传输层错误描述（超时 / DNS 失败等），仅供展示
    error_kind: str = ""  # 结构化错误类别，见 ERR_* 常量
    message: StunMessage | None = None

    @property
    def supports_rfc5780(self) -> bool:
        return bool(self.message and self.message.supports_change_request())


def _is_ipv6_literal(host: str) -> bool:
    """判断主机串是否是 IPv6 字面量（含 ``[::1]`` 形式，不含主机名）。"""
    candidate = host.strip().strip("[]")
    if ":" not in candidate:
        return False
    try:
        socket.inet_pton(socket.AF_INET6, candidate)
        return True
    except (OSError, ValueError):
        return False


def resolve(host: str, port: int, family: int = socket.AF_INET) -> tuple[str, int]:
    """解析主机名，默认只返回 IPv4。失败抛 OSError（并带准确原因）。

    本工具的映射行为检测全程复用同一个 IPv4 UDP socket，因此 ``family`` 默认
    ``AF_INET``；传 ``AF_INET6`` 时只支持 IPv6 字面量（不做 AAAA 查询之外的
    额外处理，供将来扩展）。旧实现把"IPv6 地址族不支持"报成"DNS 解析失败"，
    会把用户引到"换个 DNS"的错误方向。
    """
    if _is_ipv6_literal(host):
        literal = host.strip().strip("[]")
        if family != socket.AF_INET6:
            raise OSError(f"暂不支持 IPv6 STUN 服务器：{host}（当前仅支持 IPv4）")
        return literal, port  # 字面量不需要查 DNS
    try:
        infos = socket.getaddrinfo(host, port, family, socket.SOCK_DGRAM)
    except socket.gaierror as exc:
        if family == socket.AF_INET and _has_ipv6_address(host):
            raise OSError(
                f"{host} 只有 IPv6 地址（AAAA），当前仅支持 IPv4 STUN 服务器"
            ) from exc
        raise OSError(f"DNS 解析失败: {host}") from exc
    if not infos:
        raise OSError(f"DNS 解析无结果: {host}")
    return infos[0][4][0], infos[0][4][1]


def _has_ipv6_address(host: str) -> bool:
    """A 记录查不到时，判断该主机是否只是"有 AAAA 没有 A"。"""
    try:
        infos = socket.getaddrinfo(host, 0, socket.AF_INET6, socket.SOCK_DGRAM)
    except (socket.gaierror, OSError, UnicodeError):
        return False
    return bool(infos)


def binding_transaction(
    sock: socket.socket,
    dest: tuple[str, int],
    change_ip: bool = False,
    change_port: bool = False,
    timeout: float = 1.5,
    attempts: int = 1,
) -> BindingResult:
    """在已绑定的 UDP socket 上执行一次 Binding 事务。

    复用同一个 sock 才能保证"同一本地端口"，这是映射行为检测的前提。
    """
    res = BindingResult()
    payload, tid = build_binding_request(change_ip, change_port)
    last_err = ""
    last_kind = ""

    for _ in range(max(1, attempts)):
        try:
            sock.settimeout(timeout)
            t0 = time.perf_counter()
            sock.sendto(payload, dest)
            deadline = t0 + timeout
            while True:
                remain = deadline - time.perf_counter()
                if remain <= 0:
                    raise socket.timeout()
                sock.settimeout(remain)
                data, addr = sock.recvfrom(2048)
                # 过滤不属于本次事务的报文（同一 socket 上可能残留上一次的响应）
                if len(data) < 20 or data[8:20] != tid:
                    continue
                res.rtt_ms = (time.perf_counter() - t0) * 1000.0
                res.source = (addr[0], addr[1])
                msg = parse_message(data)
                res.message = msg
                res.origin = msg.response_origin()
                res.other = msg.other_address()
                if msg.msg_type == MSG_BINDING_SUCCESS:
                    res.ok = True
                    res.mapped = msg.mapped()
                else:
                    res.error_code = msg.error_code
                    res.error_reason = msg.error_reason
                return res
        except socket.timeout:
            last_err = "timeout"
            last_kind = ErrorKind.TIMEOUT.value
        except OSError as exc:
            last_err = str(exc)
            last_kind = ErrorKind.OS.value
        except StunError as exc:
            last_err = f"非法报文: {exc}"
            last_kind = ErrorKind.NON_STUN.value

    res.error = last_err or "unknown"
    res.error_kind = last_kind or ErrorKind.UNKNOWN.value
    return res


def binding_to(ip: str, port: int, timeout: float = 1.5) -> BindingResult:
    """向**已解析**的 ``ip:port`` 发一次 Binding 事务（不再查 DNS）。

    给"解析一次、复用结果"的调用方用：既省掉每台服务器一次多余的 DNS 查询，
    也保证上层拿到的 ``ip`` 与实际发包的目的地址是同一个 A 记录。
    """
    res = BindingResult()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("", 0))
        return binding_transaction(sock, (ip, port), timeout=timeout)
    except OSError as exc:
        res.error = str(exc)
        res.error_kind = ErrorKind.OS.value
        return res
    finally:
        sock.close()


def probe_server(host: str, port: int, timeout: float = 1.5) -> BindingResult:
    """独立 socket 探测单个服务器（用于可用性分级）。"""
    res = BindingResult()
    try:
        addr = resolve(host, port)
    except OSError as exc:
        res.error = str(exc)
        res.error_kind = ErrorKind.DNS.value
        return res
    return binding_to(addr[0], addr[1], timeout=timeout)
