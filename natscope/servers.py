# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""内置 STUN 服务器池、并发可用性探测与分级。"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import stun

# 用户提供的公共 STUN 服务器（已去重），默认 3478 端口
_DEFAULT_HOSTS = """
stun.12voip.com
stun.aa.net.uk
stun.acrobits.cz
stun.actionvoip.com
stun.annatel.net
stun.antisip.com
stun.cablenet-as.net
stun.cheapvoip.com
stun.commpeak.com
stun.cope.es
stun.dcalling.de
stun.dus.net
stun.easyvoip.com
stun.epygi.com
stun.freecall.com
stun.freeswitch.org
stun.freevoipdeal.com
stun.halonet.pl
stun.hoiio.com
stun.infra.net
stun.internetcalls.com
stun.intervoip.com
stun.ipfire.org
stun.ippi.fr
stun.it1.hr
stun.jumblo.com
stun.justvoip.com
stun.linphone.org
stun.liveo.fr
stun.lowratevoip.com
stun.miwifi.com
stun.myvoiptraffic.com
stun.mywatson.it
stun.netappel.com
stun.netgsm.com.tr
stun.nfon.net
stun.nonoh.net
stun.ooma.com
stun.pjsip.org
stun.poivy.com
stun.powervoip.com
stun.ppdi.com
stun.rockenstein.de
stun.rolmail.net
stun.rynga.com
stun.sip.us
stun.sipdiscount.com
stun.siplogin.de
stun.sipnet.net
stun.sipnet.ru
stun.siptraffic.com
stun.smartvoip.com
stun.smsdiscount.com
stun.solcon.nl
stun.solnet.ch
stun.sonetel.com
stun.sonetel.net
stun.srce.hr
stun.tel.lu
stun.telbo.com
stun.t-online.de
stun.twt.it
stun.uls.co.za
stun.usfamily.net
stun.vo.lu
stun.voicetrading.com
stun.voip.aebc.com
stun.voip.blackberry.com
stun.voip.eutelia.it
stun.voipblast.com
stun.voipbuster.com
stun.voipbusterpro.com
stun.voipcheap.com
stun.voipfibre.com
stun.voipgain.com
stun.voipinfocenter.com
stun.voipplanet.nl
stun.voippro.com
stun.voipraider.com
stun.voipstunt.com
stun.voipwise.com
stun.voipzoom.com
stun.voys.nl
stun.voztele.com
stun.webcalldirect.com
stun.zadarma.com
"""

_GOOGLE_HOSTS = [
    ("stun.l.google.com", 19302),
    ("stun1.l.google.com", 19302),
    ("stun2.l.google.com", 19302),
    ("stun3.l.google.com", 19302),
    ("stun4.l.google.com", 19302),
]

DEFAULT_SERVERS: list[tuple[str, int]] = [
    (h.strip(), 3478) for h in _DEFAULT_HOSTS.splitlines() if h.strip()
] + _GOOGLE_HOSTS

TIER_PREFERRED = "preferred"  # 优选
TIER_BACKUP = "backup"        # 备选
TIER_UNAVAILABLE = "unavailable"  # 不可用

TIER_LABEL = {
    TIER_PREFERRED: "优选",
    TIER_BACKUP: "备选",
    TIER_UNAVAILABLE: "不可用",
}

FAST_RTT_MS = 300.0


@dataclass(frozen=True)
class StunServer:
    host: str
    port: int = 3478

    @property
    def key(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def label(self) -> str:
        return self.key

    def __str__(self) -> str:
        return self.key


@dataclass
class ProbeResult:
    server: StunServer
    ok: bool = False
    rtt_ms: float = 0.0
    ip: str = ""
    public: tuple[str, int] | None = None
    supports_rfc5780: bool = False
    error: str = ""
    tier: str = TIER_UNAVAILABLE

    @property
    def tier_label(self) -> str:
        return TIER_LABEL.get(self.tier, self.tier)


def parse_server(text: str) -> StunServer:
    """解析 "host"、"host:port"、"stun:host:port"、"stun://host:port"、"[ipv6]:port"。

    解析失败一律抛 ``ValueError``——静默接受畸形输入只会让用户拿到一台
    "永远不可用"的服务器，却看不出是自己写错了。
    """
    s = text.strip()
    if not s:
        raise ValueError("服务器地址为空")
    for prefix in ("stun://", "stuns://", "stun:", "turn:"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
            break
    s = s.strip("/").strip()
    if not s:
        raise ValueError(f"服务器地址无效: {text!r}")
    if any(ch.isspace() for ch in s):
        raise ValueError(f"服务器地址不能包含空白字符: {text!r}")

    if s.startswith("["):  # [ipv6]:port
        host, sep, rest = s.partition("]")
        if not sep:
            raise ValueError(f"IPv6 地址缺少右括号: {text!r}")
        host = host[1:]
        port = 3478
        if rest:
            if not rest.startswith(":"):
                raise ValueError(f"IPv6 地址后的端口格式无效: {text!r}")
            digits = rest[1:]
            if not digits.isdigit():
                raise ValueError(f"端口无效: {text!r}")
            port = int(digits)
        if not host:
            raise ValueError(f"主机名为空: {text!r}")
        if not (1 <= port <= 65535):
            raise ValueError(f"端口越界: {port}")
        return StunServer(host, port)

    if s.count(":") == 1:
        host, _, p = s.partition(":")
        if not p.isdigit():
            raise ValueError(f"端口无效: {text!r}")
        port = int(p)
    elif s.count(":") > 1:
        # 裸 IPv6 字面量（不带方括号）无法与 host:port 区分，直接拒绝，
        # 要求用户写成 [ipv6]:port
        raise ValueError(f"IPv6 地址请写成 [地址]:端口 的形式: {text!r}")
    else:
        host, port = s, 3478
    if not (1 <= port <= 65535):
        raise ValueError(f"端口越界: {port}")
    if not host:
        raise ValueError(f"主机名为空: {text!r}")
    return StunServer(host, port)


def _probe_one(server: StunServer, timeout: float) -> ProbeResult:
    res = ProbeResult(server=server)
    try:
        ip, port = stun.resolve(server.host, server.port)
    except OSError as exc:
        res.error = str(exc)
        res.error_kind = stun.ErrorKind.DNS.value
        return res
    res.ip = ip
    # 必须把**已解析出来的 ip** 交给探测，让它只解析一次：
    # 一是省掉每台一份多余的 DNS 查询（90 台 = 90 次），二是保证 nat.py 里
    # "同一目的 IP" 的前提成立——两次解析拿到不同 A 记录会破坏映射比对。
    r = stun.binding_to(ip, port, timeout=timeout)
    if r.ok:
        res.ok = True
        res.rtt_ms = r.rtt_ms
        res.public = r.mapped
        res.supports_rfc5780 = r.supports_rfc5780
    else:
        res.error = r.error or (r.error_reason or f"错误码 {r.error_code}")
        res.error_kind = r.error_kind
    return res


def _probe_one_safe(server: StunServer, timeout: float) -> ProbeResult:
    """单台服务器的探测绝不能把整轮探测带崩。

    之前 `pool.map` 里任一 `_probe_one` 抛异常，整个 `probe_servers` 就 traceback
    退出（CLI 直接崩、GUI 把已算完的结果丢掉）。一台畸形服务器不该有这种权力。
    """
    try:
        return _probe_one(server, timeout)
    except Exception as exc:  # noqa: BLE001 - 单点故障必须降级
        res = ProbeResult(server=server)
        res.error = f"探测异常：{type(exc).__name__}: {exc}"
        res.error_kind = stun.ErrorKind.UNKNOWN.value
        return res


def probe_servers(
    servers: list[StunServer],
    timeout: float = 1.2,
    retry_timeout: float = 2.0,
    workers: int = 32,
    progress=None,
) -> list[ProbeResult]:
    """并发探测；首轮失败的对像再重试一轮，避免偶发丢包被判死。

    progress: 可选回调 (done: int, total: int, result: ProbeResult)
    """
    total = len(servers)
    if not total:
        return []
    results: list[ProbeResult] = []
    done = 0

    with ThreadPoolExecutor(max_workers=min(workers, total)) as pool:
        for r in pool.map(lambda s: _probe_one_safe(s, timeout), servers):
            results.append(r)
            done += 1
            if progress:
                progress(done, total, r)

    failed = [r for r in results if not r.ok]
    if failed:
        with ThreadPoolExecutor(max_workers=min(workers, len(failed))) as pool:
            for new in pool.map(lambda s: _probe_one_safe(s.server, retry_timeout), failed):
                if new.ok:
                    idx = results.index(next(r for r in results if r.server == new.server))
                    results[idx] = new
                    if progress:
                        progress(done, total, new)

    for r in results:
        if not r.ok:
            r.tier = TIER_UNAVAILABLE
        elif r.rtt_ms <= FAST_RTT_MS:
            r.tier = TIER_PREFERRED
        else:
            r.tier = TIER_BACKUP
    return rank_results(results)


def rank_results(results: list[ProbeResult]) -> list[ProbeResult]:
    """优选（RTT 升序）→ 备选（RTT 升序）→ 不可用。"""
    order = {TIER_PREFERRED: 0, TIER_BACKUP: 1, TIER_UNAVAILABLE: 2}

    def sort_key(r: ProbeResult):
        return (order.get(r.tier, 3), r.rtt_ms if r.ok else 9e9, r.server.host)

    return sorted(results, key=sort_key)


def distinct_ip_servers(results: list[ProbeResult], limit: int = 6) -> list[ProbeResult]:
    """挑出 IP 互不相同的可用服务器，用于映射行为检测。"""
    seen: dict[str, ProbeResult] = {}
    for r in results:
        if not r.ok or not r.ip:
            continue
        if r.ip not in seen:
            seen[r.ip] = r
        if len(seen) >= limit:
            break
    return list(seen.values())


@dataclass
class ProbeSummary:
    results: list[ProbeResult] = field(default_factory=list)

    @property
    def preferred(self) -> list[ProbeResult]:
        return [r for r in self.results if r.tier == TIER_PREFERRED]

    @property
    def backup(self) -> list[ProbeResult]:
        return [r for r in self.results if r.tier == TIER_BACKUP]

    @property
    def unavailable(self) -> list[ProbeResult]:
        return [r for r in self.results if r.tier == TIER_UNAVAILABLE]

    @property
    def usable(self) -> list[ProbeResult]:
        return [r for r in self.results if r.ok]

    @property
    def rfc5780_servers(self) -> list[ProbeResult]:
        return [r for r in self.results if r.ok and r.supports_rfc5780]

    def best(self) -> ProbeResult | None:
        return self.results[0] if self.results and self.results[0].ok else None
