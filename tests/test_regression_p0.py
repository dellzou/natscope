# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""NatScope v1.1 修复验收：三条 P0 的回归测试。

在 **v1.0.0（tag v1.0.0）上 3/3 全红（7 个子断言失败）**，修复后必须 3/3 全绿。
红基线可复现：`git archive v1.0.0 natscope` 导出旧包，与本文件 + `nat_testkit.py`
放同一目录后 `python -m unittest test_regression_p0 -v`。

覆盖 QA 审计报告（`.tmp/qa_audit/QA审计报告-NatScope-1.0.0.md`）的 3 条 P0：

  * P0-1 `nat.py`    映射采样把 OTHER-ADDRESS 写进 NAT 过滤表（预热），
                     Change-IP 回包因此被放行，受限锥型被系统性误报为**完全锥型**。
  * P0-2 `nat.py`    只有 1 台可用服务器时映射行为不可测（`MAP_UNKNOWN`），
                     `_decide()` 把已经测出来的过滤行为丢掉 → 「无法判定」。
  * P0-3 `gateway.py` 畸形 SSDP `LOCATION` 让 `urllib.parse` 抛 `ValueError`，
                     `probe_upnp` / `probe_gateway` 都没兜住 → CLI 崩溃、GUI 丢结论。

P0-1 的判据：按 socket 分组，而不是看全局调用序列
--------------------------------------------------
v1.1 把 T04 拆成两阶段：阶段 1「能力探测」允许预热（`socket A`，只为确认服务器会不会
回包），阶段 2「行为测量」必须用**全新 socket（B）零预热**（只为测 NAT 放不放行）。
所以"全局调用序列里 CHANGE-REQUEST 必须最早"是错的判据 —— 阶段 1 的预热本来就早于
CHANGE-REQUEST，但它发生在 A，不污染 B。

本文件锁定的是本质：**测量通道必须干净**。

测量通道的定义：**最后一个 CHANGE-REQUEST 所在的 socket**。
不取"第一个"，是因为阶段 1 允许做一次能力探测（`_verify_capability` 为了确认服务器
到底支不支持 CHANGE-REQUEST，本来就要真发一个，同样带 `change_ip=True`），那一次只
关心"服务器回不回包"，不关心"NAT 放不放行"；真正的行为测量是阶段 2，位于流程最后。
把判据绑在"第一个 change_ip"上会被这种正当实现误伤。

具体四条断言（都在 `P0_1MeasurementChannelTest`）：
1. 存在 CHANGE-REQUEST 调用（否则场景没构造起来）；
2. 测量通道的**第一次调用**目标必须是主服务器（一上来奔 OTHER-ADDRESS = 通道被复用）；
3. 测量通道上任何指向其它目标的普通 Binding 都必须晚于 CHANGE-REQUEST
   （没有这类调用视为最干净，也算过）；
4. 通道数 ≥ 2（阶段 1 与阶段 2 必须是不同 socket）；
5. 结论：`filtering == adf` 且 `classic == restricted`（不受限锥型被误报为完全锥型）。

零网络：STUN 事务由 `nat_testkit.FakeStunNetwork` 承担（每个 socket 一条独立 NAT
过滤表）；UPnP 的 SSDP 发现由替身替换。P0-3 不替换 `gateway._fetch` —— 三个畸形 URL
在 `urlparse()` / `parsed.port` 阶段就抛异常，走不到 DNS 与 socket，保留真实 `_fetch`
依然是离线的，同时避免"用替身替代码抛异常"掩盖修复。

运行：
    python -m unittest tests.test_regression_p0 -v
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from contextlib import ExitStack
from unittest.mock import patch

try:  # 在仓库根目录 / tests 目录下运行时 natscope 已在 sys.path 上
    import natscope  # noqa: F401
except ImportError:  # 仅用于 .tmp 下的临时位置（复现红基线）
    for _cand in pathlib.Path(__file__).resolve().parents[1:5]:
        if (_cand / "natscope").is_dir():
            sys.path.insert(0, str(_cand))
            break

from natscope import gateway
from natscope.nat import (
    CLASSIC_FULL_CONE,
    CLASSIC_RESTRICTED,
    CLASSIC_UNKNOWN,
    FILT_ADF,
    FILT_EIF,
    MAP_UNKNOWN,
)

try:  # 作为包导入（python -m unittest tests.xxx）
    from .nat_testkit import (
        LOCAL_IP,
        MAPPED_IP,
        FakeStunNetwork,
        make_probe,
        run_detector,
    )
except ImportError:  # discover -s tests 时 tests/ 在 sys.path 上
    from nat_testkit import (  # type: ignore[no-redef]
        LOCAL_IP,
        MAPPED_IP,
        FakeStunNetwork,
        make_probe,
        run_detector,
    )


# ---------------------------------------------------------------------------
# P0-1
# ---------------------------------------------------------------------------
class P0_1MeasurementChannelTest(unittest.TestCase):
    """P0-1：测量通道必须是干净的（未预热、且独立于能力探测的 socket）。

    失败含义（v1.0.0 就是这样）：采样循环先向主服务器的 OTHER-ADDRESS 发了一个普通
    Binding，把该地址写进本机 NAT 的过滤白名单；随后 Change-IP+Port 的回包恰好来自
    同一个 OTHER-ADDRESS，于是被放行 → `ii_valid=True` → `FILT_EIF` → **完全锥型**。
    真实的地址受限锥型 NAT 本应丢弃该回包（Change-IP 超时 + Change-Port 成功 →
    `FILT_ADF` → 受限锥型）。误判方向是"高估 P2P 可用性"。
    """

    PRIMARY = ("198.51.100.1", 3478)
    OTHER = ("203.0.113.9", 3479)  # 主服务器的 OTHER-ADDRESS
    SECOND = ("198.51.100.50", 3478)  # 第二台可用服务器，仅用于映射比对

    def setUp(self) -> None:
        self.fake = FakeStunNetwork(self.PRIMARY, other=self.OTHER, filter_mode="adf")
        self.probes = [
            make_probe(*self.PRIMARY),
            make_probe(*self.SECOND, rfc5780=False, rtt=30.0),
        ]
        self.result = run_detector(self.fake, self.probes)

    def test_measurement_channel_must_be_clean(self):
        """Change-IP/Port 必须跑在一条未被预热、且独立于能力探测的通道上。

        真值：地址受限锥型（EIM + ADF）。
        """
        fake, result = self.fake, self.result
        trace = fake.trace()
        measure = fake.measure_socket_id()
        channels = fake.channel_ids()

        with self.subTest("前提：存在 CHANGE-REQUEST 调用"):
            self.assertIsNotNone(
                measure,
                "观测不到任何 CHANGE-REQUEST 调用，场景没构造起来：" + trace,
            )

        if measure is None:  # 前提已失败，后续断言无意义
            return

        on_measure = fake.calls_on(measure)

        with self.subTest("阶段 1（能力探测）与阶段 2（行为测量）必须是两个 socket"):
            self.assertGreaterEqual(
                len(channels),
                2,
                "两阶段架构要求「能力探测」与「行为测量」使用**不同的 socket**"
                "（不同 socket = 不同内部端口 = 独立过滤表项；复用同一 socket 会让阶段 1 "
                f"的预热污染测量）。当前只有 {len(channels)} 条通道：{trace}",
            )

        with self.subTest("测量通道的第一个目标必须是主服务器"):
            self.assertEqual(
                on_measure[0].dest,
                self.PRIMARY,
                "测量通道上第一次调用就发给了 "
                f"{on_measure[0].dest[0]}:{on_measure[0].dest[1]}，不是主服务器 "
                f"{self.PRIMARY[0]}:{self.PRIMARY[1]} —— 说明这条通道被用于测量之前"
                "就已经被其它目标预热。调用序列：" + trace,
            )

        with self.subTest("测量通道不得被任何跨目标采样预热"):
            change_idx = [i for i, c in enumerate(on_measure) if c.is_change_request]
            warm_idx = [
                i
                for i, c in enumerate(on_measure)
                if not c.is_change_request and c.dest != self.PRIMARY
            ]
            if warm_idx:
                self.assertLess(
                    max(change_idx),
                    min(warm_idx),
                    "测量通道上存在早于 CHANGE-REQUEST 的跨目标普通 Binding —— 这一步"
                    "会把目标地址写进 NAT 过滤表，Change-IP 回包随后被放行，"
                    "受限锥型被误判成完全锥型。调用序列：" + trace,
                )

        with self.subTest("判定结论：受限锥型不应被报成完全锥型"):
            # 注：这里的调用序列里 CHANGE-REQUEST 会被重试（attempts=3 或外层 3 次）。
            # 重试**不会**把"被挡"变成"放行"：过滤表是按 (ip, port) 精确匹配的，
            # 对同一个目标重复发同一类请求不会打开任何新路径（真机同样如此）。
            # 所以"重试"与"预热"是两件事 —— 前者增加送达概率，后者改变过滤表内容。
            self.assertEqual(
                result.filtering_behavior,
                FILT_ADF,
                "地址受限锥型 NAT 上，Change-IP+Port 应被过滤（超时）、Change-Port 应放行，"
                f"过滤行为应为 adf，实际 {result.filtering_behavior!r}。调用序列：" + trace,
            )
            self.assertEqual(
                result.classic_type,
                CLASSIC_RESTRICTED,
                "真值是地址受限锥型。若报 full_cone，说明测量通道被预热、Change-IP 回包"
                f"被放行。实际 {result.classic_type!r}（{result.classic_label}）。"
                "调用序列：" + trace,
            )


# ---------------------------------------------------------------------------
# P0-2
# ---------------------------------------------------------------------------
class P0_2FilteringOnlyDecisionTest(unittest.TestCase):
    """P0-2：映射行为不可测、但过滤行为已测出时，仍应给出锥型结论。

    场景：只有 1 台可用服务器，且它不回带 OTHER-ADDRESS，因此映射样本只有 1 个
    （无法比对 → `MAP_UNKNOWN`）；但这台服务器认真实现了 CHANGE-REQUEST，
    Test II（源 IP 真的变了）与 Test III 都成功 → 过滤行为 = EIF。

    失败含义（v1.0.0 就是这样）：`_decide()` 只认 `map_b == MAP_EIM` 分支，映射不可测
    就直接落到 `CLASSIC_UNKNOWN`，把已经测出来的 EIF 整个丢掉。而 RFC 3489 的经典判据
    本身就是 Test I + Test II，不依赖多服务器映射比对；Test II 成功即完全锥型。
    """

    PRIMARY = ("198.51.100.1", 3478)

    def test_filtering_only_decision(self):
        """单服务器 + CHANGE-REQUEST 全通 → 应判为完全锥型，而不是 unknown。"""
        fake = FakeStunNetwork(self.PRIMARY, other=None, filter_mode="eif")
        result = run_detector(fake, [make_probe(*self.PRIMARY)])

        with self.subTest("场景前提：映射不可测但过滤可测"):
            self.assertEqual(
                len(result.samples),
                1,
                f"本场景应只有 1 个映射样本（无法比对），实际 {len(result.samples)} 个：" +
                "；".join(str(s) for s in result.samples),
            )
            self.assertEqual(
                result.mapping_behavior,
                MAP_UNKNOWN,
                f"只有 1 个样本时映射行为应为 unknown，实际 {result.mapping_behavior!r}",
            )
            self.assertEqual(
                result.filtering_behavior,
                FILT_EIF,
                "Test II / Test III 都成功（回包源地址均改变），过滤行为应为 eif，"
                f"实际 {result.filtering_behavior!r}",
            )

        with self.subTest("判定结论：过滤行为可测就应定出锥型，而不是 unknown"):
            self.assertEqual(
                result.classic_type,
                CLASSIC_FULL_CONE,
                "仅过滤行为可测时应能定出锥型，实际返回 "
                f"{result.classic_type!r}（{result.classic_label}）。"
                "RFC 3489 判据：Test I 成功 + Test II 成功（Change IP+Port 回包源 IP "
                "确实变了）= 完全锥型，不依赖多服务器映射比对。"
                f" 当前 mapping_label={result.mapping_label!r}，"
                f"filtering_label={result.filtering_label!r}",
            )
            self.assertNotEqual(
                result.classic_type,
                CLASSIC_UNKNOWN,
                "过滤行为已经确定为 EIF，不应返回『无法判定 / unknown』；"
                "映射行为不可测只影响能否区分三种锥型之外的东西，不该吞掉整个结论。",
            )


# ---------------------------------------------------------------------------
# P0-3
# ---------------------------------------------------------------------------
class P0_3MalformedUpnpLocationTest(unittest.TestCase):
    """P0-3：畸形 SSDP `LOCATION` 不得让 UPnP 探测抛出异常。

    这三类 `LOCATION` 会让 `urllib.parse` 抛 `ValueError`，而 v1.0.0 中
    `probe_upnp` / `probe_gateway` 只捕获 `OSError` 与 `http.client.HTTPException`，
    异常一路穿透到 `cli.py`（进程 traceback 退出）与 `gui/app.py`（兜底 except 吞掉，
    已经算完的 NAT 结论被整体丢弃）。

    说明：用例不替换 `gateway._fetch`。三个 URL 都在 `urlparse()` / `parsed.port`
    阶段就抛异常，走不到 DNS 与 socket，所以真实 `_fetch` 依然是离线的；相反，若用替身
    去"模拟"异常，则修复后测试仍会红（异常来自替身而非被测代码），会误判修复。
    """

    MALFORMED_LOCATIONS = [
        ("端口非数字", "http://192.168.1.1:abc/desc.xml"),
        ("IPv6 字面量括号不闭合", "http://[::1/desc.xml"),
        ("端口越界", "http://192.168.1.1:99999/desc.xml"),
    ]

    def _probe_with_location(self, location: str) -> gateway.Capability:
        # 替身用 *args/**kwargs 吞掉签名变化（v1.1 就新增了 source_ip 关键字），
        # 否则"签名一变、替身先炸"，红的原因就变成替身不兼容而不是被测代码有问题。
        def fake_discover(*_args, _loc=location, **_kwargs):
            return [_loc]

        with patch.object(gateway, "_ssdp_discover", fake_discover):
            return gateway.probe_upnp("192.168.1.1", timeout=0.05)

    def test_malformed_upnp_location_does_not_raise(self):
        """三种畸形 LOCATION：不抛异常，且不得声称 UPnP 可用。"""
        for label, location in self.MALFORMED_LOCATIONS:
            with self.subTest(f"probe_upnp 不崩溃：{label} {location}"):
                try:
                    cap = self._probe_with_location(location)
                except Exception as exc:  # noqa: BLE001 - 这里就是要抓到任何穿透异常
                    self.fail(
                        f"probe_upnp 收到畸形 LOCATION 时抛出了未捕获的 "
                        f"{type(exc).__name__}: {exc}。该异常会穿透 probe_gateway，"
                        "在 CLI 让进程 traceback 退出、在 GUI 让已算完的 NAT 结论被丢弃。"
                        f" 畸形输入：{location}"
                    )
                self.assertIsInstance(
                    cap,
                    gateway.Capability,
                    f"probe_upnp 应始终返回 Capability，实际返回 {type(cap).__name__}",
                )
                self.assertFalse(
                    cap.available,
                    f"畸形 LOCATION 不可能探测到可用的 IGD，却返回 available=True（{label}）",
                )

        with self.subTest("probe_gateway 不因 UPnP 失败而整体穿透"):
            natpmp_ok = gateway.Capability(
                "NAT-PMP", available=True, state="ok", external_ip=MAPPED_IP
            )
            pcp_absent = gateway.Capability("PCP", state="absent")
            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(gateway, "default_gateway", lambda: "192.168.1.1")
                )
                stack.enter_context(
                    patch.object(gateway, "probe_natpmp", lambda ip, timeout=1.5: natpmp_ok)
                )
                stack.enter_context(
                    patch.object(gateway, "probe_pcp", lambda ip, timeout=1.5: pcp_absent)
                )
                stack.enter_context(
                    patch.object(
                        gateway,
                        "_ssdp_discover",
                        lambda *_a, **_kw: ["http://[::1/desc.xml"],
                    )
                )
                try:
                    rep = gateway.probe_gateway(LOCAL_IP)
                except Exception as exc:  # noqa: BLE001
                    self.fail(
                        f"probe_gateway 因 UPnP 的畸形 LOCATION 整体抛出 "
                        f"{type(exc).__name__}: {exc}。UPnP 只是三项能力之一，"
                        "它的失败不应带走 NAT-PMP / PCP 的探测结果。"
                    )
            self.assertTrue(
                rep.natpmp.available,
                "UPnP 探测失败时 NAT-PMP 的结果必须保留，实际 natpmp.available="
                f"{rep.natpmp.available}，upnp.state={rep.upnp.state}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
