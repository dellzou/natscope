"""网关端口映射服务探测：UPnP-IGD、NAT-PMP (RFC 6886)、PCP (RFC 6887)。

只做"能力识别"，不真正添加端口映射（避免在用户网关上留下残留条目）。
"""

from __future__ import annotations

import http.client
import re
import socket
import struct
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_ST = "urn:schemas-upnp-org:device:InternetGatewayDevice:1"
UPNP_TIMEOUT = 2.0
PMP_PORT = 5351

_IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


@dataclass
class Capability:
    name: str
    available: bool = False
    external_ip: str = ""
    detail: str = ""
    version: str = ""
    # 用于界面上的三态：未发现 / 存在但受限 / 可用
    state: str = "unknown"  # unknown / absent / limited / ok

    @property
    def state_label(self) -> str:
        return {
            "ok": "可用",
            "limited": "存在但受限",
            "absent": "未发现",
            "unknown": "未检测",
        }.get(self.state, self.state)


@dataclass
class GatewayReport:
    gateway_ip: str = ""
    upnp: Capability = field(default_factory=lambda: Capability("UPnP-IGD"))
    natpmp: Capability = field(default_factory=lambda: Capability("NAT-PMP"))
    pcp: Capability = field(default_factory=lambda: Capability("PCP"))
    notes: list[str] = field(default_factory=list)

    @property
    def capabilities(self) -> list[Capability]:
        return [self.upnp, self.natpmp, self.pcp]

    @property
    def any_available(self) -> bool:
        return any(c.available for c in self.capabilities)

    @property
    def summary(self) -> str:
        names = [c.name for c in self.capabilities if c.available]
        if names:
            return "网关支持 " + " / ".join(names) + "，可主动申请端口映射改善 P2P 连通性。"
        if any(c.state == "limited" for c in self.capabilities):
            return "检测到端口映射服务但未能完成查询（可能被网关策略限制）。"
        return "未发现网关端口映射服务；P2P 连通性只能依赖 NAT 穿透。"


# --------------------------------------------------------------------------
# 默认网关
# --------------------------------------------------------------------------


def default_gateway() -> str:
    """获取 IPv4 默认网关。

    优先用 `route print`（输出几乎全为数字与英文，不受系统语言影响），
    失败再解析 ipconfig，最后回退到本地网段的 .1 / .254。
    """
    gw = _gateway_from_route()
    if gw:
        return gw
    gw = _gateway_from_ipconfig()
    if gw:
        return gw
    return ""


def _gateway_from_route() -> str:
    try:
        out = subprocess.run(
            ["route", "print", "-4", "0.0.0.0"],
            capture_output=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.decode("mbcs", "replace")
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
            cand = parts[2]
            if _is_ipv4(cand) and not cand.startswith("0.") and cand != "0.0.0.0":
                return cand
    return ""


def _gateway_from_ipconfig() -> str:
    try:
        out = subprocess.run(
            ["ipconfig"],
            capture_output=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    text = out.decode("mbcs", "replace")
    for line in text.splitlines():
        low = line.lower()
        if ("gateway" in low or "网关" in line) and ":" in line:
            m = _IPV4_RE.search(line)
            if m and _is_ipv4(m.group(1)) and m.group(1) != "0.0.0.0":
                return m.group(1)
    return ""


def _is_ipv4(s: str) -> bool:
    parts = s.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def guess_gateways(local_ip: str) -> list[str]:
    """本地 IP 所在 /24 的常见网关候选。"""
    if not local_ip or not _is_ipv4(local_ip):
        return []
    base = local_ip.rsplit(".", 1)[0]
    return [f"{base}.1", f"{base}.254"]


# --------------------------------------------------------------------------
# UPnP-IGD
# --------------------------------------------------------------------------


def _ssdp_discover(timeout: float = UPNP_TIMEOUT) -> list[str]:
    """SSDP M-SEARCH，返回所有网关的 LOCATION。"""
    locations: list[str] = []
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {SSDP_ST}\r\n"
        "\r\n"
    ).encode("ascii")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        except OSError:
            pass
        sock.settimeout(timeout)
        sock.sendto(msg, SSDP_ADDR)
        deadline = timeout
        while deadline > 0:
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                break
            text = data.decode("utf-8", "replace")
            if not text.upper().startswith("HTTP/1.1 200"):
                continue
            for line in text.splitlines():
                if line.lower().startswith("location:"):
                    loc = line.split(":", 1)[1].strip()
                    if loc and loc not in locations:
                        locations.append(loc)
                    break
            deadline -= 0.1
            sock.settimeout(0.5)
    except OSError:
        pass
    finally:
        sock.close()
    return locations


def _fetch(url: str, timeout: float = UPNP_TIMEOUT) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", ""):
        raise OSError("不支持的协议")
    host = parsed.hostname or ""
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"User-Agent": "NatScope/1.0"})
        resp = conn.getresponse()
        return resp.read(256 * 1024).decode("utf-8", "replace")
    finally:
        conn.close()


def _find_control_url(xml_text: str, base_url: str) -> tuple[str, str] | None:
    """从设备描述里找 WANIPConnection / WANPPPConnection 的 controlURL。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    base = base_url if base_url.endswith("/") else base_url.rsplit("/", 1)[0] + "/"

    for svc in root.iter():
        if not svc.tag.endswith("service"):
            continue
        stype = ""
        ctrl = ""
        for child in svc:
            tag = child.tag.split("}")[-1]
            if tag == "serviceType":
                stype = (child.text or "").strip()
            elif tag == "controlURL":
                ctrl = (child.text or "").strip()
        if not ctrl:
            continue
        if "WANIPConnection" in stype or "WANPPPConnection" in stype:
            if ctrl.startswith("http"):
                return stype, ctrl
            return stype, urllib.parse.urljoin(base, ctrl)
    return None


def _soap_get_external_ip(control_url: str, service_type: str) -> tuple[bool, str, str]:
    """调用 GetExternalIPAddress，返回 (成功, 外网 IP, 说明)。"""
    parsed = urllib.parse.urlparse(control_url)
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:GetExternalIPAddress xmlns:u="{service_type}"/>'
        "</s:Body></s:Envelope>"
    )
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": f'"{service_type}#GetExternalIPAddress"',
        "User-Agent": "NatScope/1.0",
    }
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    conn = http.client.HTTPConnection(parsed.hostname or "", parsed.port or 80, timeout=UPNP_TIMEOUT)
    try:
        conn.request("POST", path, body=body.encode("utf-8"), headers=headers)
        resp = conn.getresponse()
        text = resp.read(64 * 1024).decode("utf-8", "replace")
    finally:
        conn.close()

    m = re.search(r"<NewExternalIPAddress>\s*([^<]*)\s*</NewExternalIPAddress>", text, re.I)
    if m and _is_ipv4(m.group(1).strip()) and m.group(1).strip() != "0.0.0.0":
        return True, m.group(1).strip(), "GetExternalIPAddress 成功"
    if resp.status >= 400:
        return False, "", f"SOAP 调用被拒绝（HTTP {resp.status}）"
    return False, "", "网关未返回有效外网地址"


def probe_upnp(gateway_ip: str, timeout: float = UPNP_TIMEOUT) -> Capability:
    cap = Capability("UPnP-IGD")
    locations = _ssdp_discover(timeout)
    if not locations:
        cap.state = "absent"
        cap.detail = "SSDP 未发现 IGD 设备"
        return cap

    for loc in locations[:3]:
        try:
            desc = _fetch(loc, timeout)
        except (OSError, http.client.HTTPException) as exc:
            cap.state = "limited"
            cap.detail = f"发现设备但读取描述失败：{exc}"
            continue
        found = _find_control_url(desc, loc)
        if not found:
            continue
        service_type, control_url = found
        cap.version = "WANIPConnection" if "WANIPConnection" in service_type else "WANPPPConnection"
        try:
            ok, ip, msg = _soap_get_external_ip(control_url, service_type)
        except (OSError, http.client.HTTPException) as exc:
            cap.state = "limited"
            cap.detail = f"发现 IGD 但 SOAP 调用失败：{exc}"
            continue
        if ok:
            cap.available = True
            cap.state = "ok"
            cap.external_ip = ip
            cap.detail = f"{msg}，外网地址 {ip}"
            return cap
        cap.state = "limited"
        cap.detail = msg
    if cap.state == "unknown":
        cap.state = "absent"
        cap.detail = "发现 SSDP 响应但无 WAN 连接服务"
    return cap


# --------------------------------------------------------------------------
# NAT-PMP / PCP
# --------------------------------------------------------------------------


def probe_natpmp(gateway_ip: str, timeout: float = 1.5) -> Capability:
    cap = Capability("NAT-PMP")
    if not gateway_ip:
        cap.state = "absent"
        cap.detail = "未知默认网关"
        return cap
    # RFC 6886：version=0, opcode=0（公网地址请求）, reserved
    req = struct.pack("!BBH", 0, 0, 0)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        sock.sendto(req, (gateway_ip, PMP_PORT))
        data, _ = sock.recvfrom(64)
    except socket.timeout:
        cap.state = "absent"
        cap.detail = "网关 5351 端口无响应"
        return cap
    except OSError as exc:
        cap.state = "absent"
        cap.detail = str(exc)
        return cap
    finally:
        sock.close()

    if len(data) < 8:
        cap.state = "absent"
        cap.detail = "响应过短"
        return cap
    version, opcode, result = struct.unpack("!BBH", data[:4])
    if version != 0:
        cap.state = "absent"
        cap.detail = "响应版本非 NAT-PMP"
        return cap
    cap.version = f"v{version}"
    if result != 0:
        cap.state = "limited"
        cap.detail = f"网关返回错误码 {result}"
        return cap
    if len(data) >= 12:
        ip = ".".join(str(b) for b in data[8:12])
        cap.available = True
        cap.state = "ok"
        cap.external_ip = ip if ip != "0.0.0.0" else ""
        cap.detail = f"外网地址 {cap.external_ip or '未分配'}"
        return cap
    cap.state = "limited"
    cap.detail = "响应缺少外网地址"
    return cap


def probe_pcp(gateway_ip: str, timeout: float = 1.5) -> Capability:
    cap = Capability("PCP")
    if not gateway_ip:
        cap.state = "absent"
        cap.detail = "未知默认网关"
        return cap

    nonce = struct.pack("!I", 0x4E415453) + b"\x00" * 8  # 固定 nonce 即可
    # 24 字节头：version=2, R=0|opcode=1(MAP), reserved, lifetime, client ip(16)
    header = struct.pack("!BBHI", 2, 1, 0, 120)
    header += b"\x00" * 16  # client IP 全 0 表示"由服务器推断"
    # 36 字节 MAP 载荷
    map_body = nonce + struct.pack("!B", 17) + b"\x00" * 3
    map_body += struct.pack("!HH", 9, 9)  # internal port / suggested external port
    map_body += b"\x00" * 16  # suggested external IP
    req = header + map_body

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        sock.sendto(req, (gateway_ip, PMP_PORT))
        data, _ = sock.recvfrom(128)
    except socket.timeout:
        cap.state = "absent"
        cap.detail = "网关 5351 端口无响应（PCP 与 NAT-PMP 共用端口）"
        return cap
    except OSError as exc:
        cap.state = "absent"
        cap.detail = str(exc)
        return cap
    finally:
        sock.close()

    if len(data) < 24:
        cap.state = "absent"
        cap.detail = "响应过短"
        return cap
    version = data[0]
    result = data[3]
    cap.version = f"v{version}"
    if version != 2:
        cap.state = "absent"
        cap.detail = f"响应版本 {version}，非 PCP v2"
        return cap
    if result == 0:
        cap.available = True
        cap.state = "ok"
        if len(data) >= 24 + 36:
            ip_raw = data[44:60]
            cap.external_ip = socket.inet_ntop(socket.AF_INET6, ip_raw)
        cap.detail = "MAP 请求成功，网关支持 PCP"
        return cap
    reasons = {1: "不支持该 PCP 版本", 2: "未授权", 3: "格式错误", 4: "资源不足"}
    cap.state = "limited"
    cap.detail = reasons.get(result, f"结果码 {result}")
    return cap


def probe_gateway(local_ip: str = "", timeout: float = 1.5) -> GatewayReport:
    """三类探测并发执行，总耗时约等于最慢的一项（~2 s）。"""
    from concurrent.futures import ThreadPoolExecutor

    rep = GatewayReport()
    gw = default_gateway()
    if not gw:
        for cand in guess_gateways(local_ip):
            gw = cand
            break
    rep.gateway_ip = gw
    if not gw:
        rep.notes.append("未能获取默认网关地址，仅尝试 UPnP 组播发现")
        rep.upnp = probe_upnp("", timeout=UPNP_TIMEOUT)
        rep.natpmp.state = "absent"
        rep.pcp.state = "absent"
        return rep

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_upnp = pool.submit(probe_upnp, gw, UPNP_TIMEOUT)
        f_pmp = pool.submit(probe_natpmp, gw, min(timeout, 1.5))
        f_pcp = pool.submit(probe_pcp, gw, min(timeout, 1.5))
        rep.upnp = f_upnp.result()
        rep.natpmp = f_pmp.result()
        rep.pcp = f_pcp.result()

    if not rep.any_available and rep.upnp.state == "absent":
        rep.notes.append(f"网关 {gw} 未响应 UPnP / NAT-PMP / PCP")
    return rep
