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
    """解析 "host"、"host:port"、"stun:host:port"、"stun://host:port"。"""
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
    if s.startswith("["):  # [ipv6]:port
        host, _, rest = s.partition("]")
        host = host[1:]
        port = 3478
        if rest.startswith(":") and rest[1:].isdigit():
            port = int(rest[1:])
        return StunServer(host, port)
    if s.count(":") == 1:
        host, _, p = s.partition(":")
        if not p.isdigit():
            raise ValueError(f"端口无效: {text!r}")
        port = int(p)
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
        return res
    res.ip = ip
    r = stun.probe_server(server.host, server.port, timeout=timeout)
    if r.ok:
        res.ok = True
        res.rtt_ms = r.rtt_ms
        res.public = r.mapped
        res.supports_rfc5780 = r.supports_rfc5780
    else:
        res.error = r.error or (r.error_reason or f"错误码 {r.error_code}")
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
        for r in pool.map(lambda s: _probe_one(s, timeout), servers):
            results.append(r)
            done += 1
            if progress:
                progress(done, total, r)

    failed = [r for r in results if not r.ok]
    if failed:
        with ThreadPoolExecutor(max_workers=min(workers, len(failed))) as pool:
            for new in pool.map(lambda s: _probe_one(s.server, retry_timeout), failed):
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
