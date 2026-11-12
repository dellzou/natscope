# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""NatScope v1.1 回归测试：P1 行为锁（不是实现锁）。

两条 P1 都是"**实现一改就静默失效、且没有任何测试会发现**"的类型，所以这里锁的是
**可观察行为**，刻意不锁实现细节：

* P1-1  Test II / Test III 单包不重试 —— 一次 UDP 丢包就把结论降级一级
        （Full Cone → Restricted → Port Restricted），误判方向是"把好结果说成坏结果"。
        锁法：让"首个数据包必丢"，断言**结论不变**。
        实现无论是"同事务 attempts>=2"还是"外层多次独立调用"，本用例都成立。
* P1-3  Test III 的超时既可能是「NAT 在过滤」，也可能是「服务器根本不实现
        change_port」。故双超时必须先**正向证明**服务器会回 change_port，才允许判
        APDF；否则应报「锥型（未细分）」而不是「端口受限锥型」。
        锁法：同一场景成对跑「能力未证明 / 能力已证明」，断言两个方向都对。
* P1-2  APDF 判定曾用 `r2.error == "timeout"` 裸字符串比对 —— 底层任何措辞调整
        （加超时时长 / 中文化 / 换异常描述）都会让 APDF 分支永久不命中，
        端口受限锥型静默降级为「锥型未细分」。
        锁法：同一语义换 5 种措辞，断言**结论完全一致**（且为端口受限锥型）。

零网络：见 `nat_testkit.py`。

用例构造的隔离纪律（踩过的坑，后来者必读）
------------------------------------------
P1-2 最初写成「单服务器 + 主服务器回带 OTHER-ADDRESS」，结果在 v1.0.0 上**红的原因
是 P0-1 的预热**：采样阶段先向 OTHER-ADDRESS 发了包，Change-IP 回包因此被放行，
Test II 与 Test III 都"成功"，`error` 措辞这一维压根没被触发 —— 看上去用例红了，
实际测的是 P0-1，不是 P1-2。

改成「主服务器**不回带** OTHER-ADDRESS + 两台服务器凑映射样本」之后，红的原因才是
P1-2 本身：`('unknown', 'cone_unknown') != ('apdf', 'port_restricted')`。

**通用教训：两个缺陷的作用机制重叠时，构造用例必须先隔离变量**，否则你会以为测到了
A，其实测的是 B。规则化成两条：
1. 凡是要测「过滤判定」的用例，先确认没有任何预热能让 Change-IP 通过 —— 否则测到的
   可能是 P0-1；
2. P1-1 的 `filter_mode` 必须是 `"eif"`（全放行）+ 显式 `drop_first`，这样"结论降级"
   只可能来自丢包，不可能来自 NAT 过滤。

运行：
    python -m unittest tests.test_regression_p1 -v
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from unittest.mock import patch

try:  # 在仓库根目录 / tests 目录下运行时 natscope 已在 sys.path 上
    import natscope  # noqa: F401
except ImportError:  # 仅用于 .tmp 下的临时位置（复现红基线）
    for _cand in pathlib.Path(__file__).resolve().parents[1:5]:
        if (_cand / "natscope").is_dir():
            sys.path.insert(0, str(_cand))
            break

from natscope import nat as natmod
from natscope.nat import (
    CLASSIC_CONE_UNKNOWN,
    CLASSIC_FULL_CONE,
    CLASSIC_PORT_RESTRICTED,
    CLASSIC_RESTRICTED,
    CLASSIC_SYMMETRIC,
    FILT_ADF,
    FILT_APDF,
    FILT_EIF,
    FILT_UNKNOWN,
    MAP_EIM,
)

try:  # 作为包导入（python -m unittest tests.xxx）
    from .nat_testkit import FakeClock, FakeStunNetwork, make_probe, run_detector
except ImportError:  # discover -s tests 时 tests/ 在 sys.path 上
    from nat_testkit import (  # type: ignore[no-redef]
        FakeClock,
        FakeStunNetwork,
        make_probe,
        run_detector,
    )


PRIMARY = ("198.51.100.1", 3478)
SECOND = ("198.51.100.50", 3478)  # 第二台可用服务器，用于凑出第二个映射样本（EIM）
OTHER = ("203.0.113.9", 3479)  # 主服务器宣告的 OTHER-ADDRESS（P1-2 中"宣告但不可达"）


# ---------------------------------------------------------------------------
# P1-1
# ---------------------------------------------------------------------------
class P1_1RetryTest(unittest.TestCase):
    """P1-1：单个 UDP 丢包不得改变结论。

    场景：真值 = 完全锥型（NAT 放行任意地址），"首个数据包必丢"模拟一次偶发丢包。
    v1.0.0 的 Test II / III 都是 `attempts=1` 裸发（服务器探测阶段反而有 `retry_timeout`
    二轮重试），所以一次丢包就会被解释成"被 NAT 过滤"：
      * 只丢 Test II  → 判成 ADF  → 受限锥型（差 1 级）
      * II 与 III 各丢 → 判成 APDF → 端口受限锥型（差 2 级）

    顺带澄清一个常见担心：**重试不会把"被挡"变成"放行"**。NAT 的过滤表按
    (ip, port) 精确匹配，对同一目标重发同一类请求不会新增任何路径；重试只是
    提高"本来能通的那一包"的送达概率。本例里"丢包"和"被过滤"是两个独立的旋钮
    （`drop_first` vs `filter_mode`），就是为了把这点钉死：`filter_mode="eif"`
    下丢包必须被重试吃掉，而 P0-1 里 `filter_mode="adf"` 下重试 3 次依旧全超时。
    """

    def _run(self, drop: set[tuple[bool, bool]]):
        fake = FakeStunNetwork(PRIMARY, other=None, filter_mode="eif", drop_first=drop)
        probes = [make_probe(*PRIMARY), make_probe(*SECOND, rfc5780=False, rtt=30.0)]
        result = run_detector(fake, probes)
        return fake, result

    def test_single_packet_loss_must_not_change_conclusion(self):
        """每项测试各自的首个数据包丢失时，结论仍应是完全锥型。"""
        cases = [
            ("只丢 Test II 的首包", {(True, True)}),
            ("Test II 与 Test III 各丢首包", {(True, True), (False, True)}),
        ]
        for label, drop in cases:
            with self.subTest(label):
                fake, result = self._run(drop)
                detail = (
                    f"过滤行为 {result.filtering_behavior!r} / 结论 {result.classic_type!r}"
                    f"（{result.classic_label}）；CHANGE-REQUEST 的 attempts = "
                    f"{fake.change_attempts()}；调用序列：{fake.trace()}"
                )
                self.assertEqual(
                    result.filtering_behavior,
                    FILT_EIF,
                    "真值是完全锥型（NAT 放行任意地址），只是首个数据包丢了。"
                    "过滤行为应仍为 eif —— 说明 CHANGE-REQUEST 必须重试而不是裸发一次。"
                    + detail,
                )
                self.assertEqual(
                    result.classic_type,
                    CLASSIC_FULL_CONE,
                    "一次偶发丢包不应把『完全锥型』降级成『受限 / 端口受限锥型』。"
                    + detail,
                )


# ---------------------------------------------------------------------------
# P1-2
# ---------------------------------------------------------------------------
class P1_2ErrorWordingTest(unittest.TestCase):
    """P1-2：判定只能依赖结构化的错误类别，不能比对 `error` 的措辞。

    场景：Test II 与 Test III 都超时（真值 = 端口受限锥型 APDF）。
    语义完全一样，只是底层给出的 `error` 文案不同 —— 结论必须一致。
    v1.0.0 用 `r2.error == "timeout"` 比对，措辞一变 APDF 分支就永不命中，
    静默降级为「锥型（未细分）」。

    ⚠️ 这个场景的构造有个坑，见模块 docstring：**必须先把 P0-1 的预热干扰排除**。
    做法是让主服务器"宣告了 OTHER-ADDRESS，但那个地址实际不可达"
    （`other_unreachable=True`）：
      * v1.0.0：采样阶段往 OTHER-ADDRESS 发的包**打不开** Change-IP 的回包路径
        （对方回不来），于是两项测试都超时 —— 措辞这一维才真正被触发；
      * v1.1：阶段 1 的能力诊断用的是**另一个一次性 socket**，它先把预期回包源
        预热进白名单，从而能"正向证明服务器会响应 change_port"，双超时才允许
        判 APDF（P1-3 的修复）；测量通道依旧是干净的，所以两项都超时。
    如果不做这层隔离，v1.0.0 会因为预热把两项测试都"测成功"，用例红的原因就变成
    P0-1，而措辞根本没被验证到 —— 典型的"以为测了 A，其实测了 B"。
    """

    WORDINGS = [
        "timeout",
        "超时（1.2s 内无响应）",
        "timed out",
        "socket.timeout: timed out",
        "",  # 空串：只要有结构化类别就该照常判定
    ]

    def _run(self, wording: str):
        fake = FakeStunNetwork(
            PRIMARY,
            other=OTHER,
            filter_mode="apdf",
            timeout_text=wording,
            other_unreachable=True,
        )
        probes = [make_probe(*PRIMARY), make_probe(*SECOND, rfc5780=False, rtt=30.0)]
        return run_detector(fake, probes)

    def test_error_wording_must_not_change_conclusion(self):
        """同一语义、5 种措辞 → 结论必须完全相同且都是端口受限锥型。"""
        outcomes: dict[str, tuple[str, str]] = {}
        for text in self.WORDINGS:
            result = self._run(text)
            outcomes[text] = (result.filtering_behavior, result.classic_type)

        rendered = "；".join(
            f"{text!r} → filtering={got[0]!r} classic={got[1]!r}"
            for text, got in outcomes.items()
        )

        with self.subTest("措辞不影响结论"):
            baseline = outcomes["timeout"]
            for text, got in outcomes.items():
                self.assertEqual(
                    got,
                    baseline,
                    "同一语义（超时）只因 error 文案不同就给出了不同结论 —— 说明判定"
                    "依赖了 error 的字符串措辞。应改用结构化的错误类别"
                    f"（如 BindingResult.error_kind）。明细：{rendered}",
                )

        with self.subTest("两项都超时 = 端口受限锥型"):
            for text, got in outcomes.items():
                self.assertEqual(
                    got[0],
                    FILT_APDF,
                    f"Test II 与 Test III 都超时 → 过滤行为应为 apdf（措辞 {text!r}），"
                    f"实际 {got[0]!r}。明细：{rendered}",
                )
                self.assertEqual(
                    got[1],
                    CLASSIC_PORT_RESTRICTED,
                    f"APDF + EIM 应判为端口受限锥型（措辞 {text!r}），"
                    f"实际 {got[1]!r}。明细：{rendered}",
                )


# ---------------------------------------------------------------------------
# P1-3
# ---------------------------------------------------------------------------
class P1_3CapabilityProofTest(unittest.TestCase):
    """P1-3：双超时不能直接判 APDF —— 必须先证明「服务器真的会响应 change_port」。

    Test II / III 都超时有两种完全不同的原因：
      * NAT 在挡（端口受限锥型）→ 应判 APDF；
      * 服务器压根不实现 change_port（真机常见：不少公共 STUN 直接丢包）→ 什么都没测到。
    v1.0.0 把两者混为一谈，一律判 APDF，用户会被告知"端口受限锥型、建议开端口映射"，
    而真相可能只是"这台服务器不配合"。

    两个方向成对锁：
      * 能力**未证明** → `unknown` / `cone_unknown`（绝不能是 apdf / port_restricted）；
      * 能力**已证明** → `apdf` / `port_restricted`（防止修过头把 APDF 一刀切掉）。

    构造：服务器宣告了 OTHER-ADDRESS（看起来有能力），但阶段 2 的两项测试都超时
    （`other_unreachable=True`）；区别只在"它到底会不会回 change_port"
    （`change_port_supported`）。
    """

    def _run(self, change_port_supported: bool):
        fake = FakeStunNetwork(
            PRIMARY,
            other=OTHER,
            filter_mode="apdf",
            other_unreachable=True,
            change_port_supported=change_port_supported,
        )
        probes = [make_probe(*PRIMARY), make_probe(*SECOND, rfc5780=False, rtt=30.0)]
        return fake, run_detector(fake, probes)

    def test_both_timeout_without_capability_proof_must_be_unknown(self):
        """服务器不实现 change_port → 双超时必须落到 unknown，不能冒充端口受限锥型。"""
        fake, result = self._run(change_port_supported=False)
        detail = (
            f"filtering={result.filtering_behavior!r} classic={result.classic_type!r}；"
            f"steps={[(s.name, s.status) for s in result.steps]}；"
            f"调用序列：{fake.trace()}"
        )
        self.assertNotEqual(
            result.filtering_behavior,
            FILT_APDF,
            "两项测试都超时，但**没有任何服务器能证明自己会响应 change_port** —— "
            "此时无法区分『NAT 在过滤』与『服务器不实现』，不能判 apdf。" + detail,
        )
        self.assertEqual(
            result.filtering_behavior,
            FILT_UNKNOWN,
            "这种情形下过滤行为只能是 unknown（信息不足），实际却给出了结论。" + detail,
        )
        self.assertNotEqual(
            result.classic_type,
            CLASSIC_PORT_RESTRICTED,
            "v1.0.0 的缺陷正是把『服务器不配合』当成『端口受限锥型』报给用户，"
            "并建议用户去开端口映射。" + detail,
        )
        self.assertEqual(
            result.classic_type,
            CLASSIC_CONE_UNKNOWN,
            "映射行为已确认是 EIM（两台服务器映射端口一致），过滤行为不可测 → "
            "应报『锥型（未细分）』cone_unknown。" + detail,
        )

    def test_both_timeout_with_capability_proof_must_be_port_restricted(self):
        """反向对照：服务器确实会回 change_port（能力已证明）→ 双超时才等于 APDF。"""
        fake, result = self._run(change_port_supported=True)
        detail = (
            f"filtering={result.filtering_behavior!r} classic={result.classic_type!r}；"
            f"调用序列：{fake.trace()}"
        )
        self.assertEqual(
            result.filtering_behavior,
            FILT_APDF,
            "服务器已被正向证明会响应 change_port（阶段 1 用一次性 socket 预热后收到了回包），"
            "此时阶段 2 仍双超时 → 只能是 NAT 在过滤 → apdf。"
            "若这里变成 unknown，说明能力证明没生效或把 APDF 一刀切掉了。" + detail,
        )
        self.assertEqual(
            result.classic_type,
            CLASSIC_PORT_RESTRICTED,
            "EIM + APDF = 端口受限锥型。" + detail,
        )


# ---------------------------------------------------------------------------
# 时间预算耗尽（v1.1 新引入的重试/预算策略带来的风险）
# ---------------------------------------------------------------------------
class BudgetExhaustionTest(unittest.TestCase):
    """时间预算耗尽不得改变判定结论。

    这条与 P0-1 是同一类毛病：**偏差来自工具自身的执行策略，而不是网络事实**。
    P0-1 靠"预热污染过滤表"制造假阳性；这里靠"预算不够 → 单包超时被压缩"制造
    假阴性（把放行的 change_port 判成被过滤）。

    机制：`_measure_filtering` 里 `timeout = min(FILTER_TIMEOUT_S, remain)`。
    Test II 的重试（3 次 × 0.5 s + 间隔）先把预算吃掉大半，轮到 Test III 时
    `remain` 可能只剩几十~几百毫秒；如果服务器回包时延本来就有 0.3 s，
    就会**因为没等够**而判超时 → ADF 被推成 APDF（受限锥型 → 端口受限锥型，
    方向是让用户白折腾端口映射）。

    `other_unreachable=True` 是为了把 P0-1 的预热干扰排除：否则 v1.0.0 会用采样
    把 OTHER-ADDRESS 写进白名单，Change-IP 直接"测成功"，场景退化成 EIF，
    这两条用例在旧版本上会因为 P0-1 而红 —— 红的就不是"预算"这件事了。

    注意：v1.0.0 **没有**这套预算/重试机制（Test II/III 是固定 1.5 s 裸发），
    所以本用例在 v1.0.0 上本来就是绿的 —— 它防的是 v1.1 新引入的回归。
    """

    LATENCY_S = 0.30  # 服务器 change_port 回包时延（真机几十~几百 ms 都常见）
    BUDGET_S = 2.30  # 故意收紧总预算，让 Test II 的三次重试几乎吃光它

    def test_budget_exhaustion_must_not_flip_adf_into_apdf(self):
        """真值 = 地址受限锥型；预算将尽也必须报 adf / restricted。"""
        # 完整体验一遍"真机时间线"：假时钟把"等满 timeout 才超时"也算进预算
        clock = FakeClock()
        fake = FakeStunNetwork(
            PRIMARY,
            other=OTHER,
            filter_mode="adf",  # change_ip 被挡、change_port 放行
            other_unreachable=True,  # 见下：隔离 P0-1 的预热，只留"预算"这一个变量
            response_latency=self.LATENCY_S,
            clock=clock,
        )
        probes = [make_probe(*PRIMARY), make_probe(*SECOND, rfc5780=False, rtt=30.0)]

        # create=True：v1.0.0 没有这套预算机制（常量不存在），本用例在旧版本上应为绿，
        # 只是作为"v1.1 新引入风险"的护栏存在。
        with patch.object(natmod, "FILTER_BUDGET_S", self.BUDGET_S, create=True):
            result = run_detector(fake, probes, clock=clock)

        change_calls = [(c.change_ip, c.change_port) for c in fake.calls if c.is_change_request]
        detail = (
            f"filtering={result.filtering_behavior!r} classic={result.classic_type!r}；"
            f"预算 FILTER_BUDGET_S={self.BUDGET_S}s、回包时延={self.LATENCY_S}s、"
            f"时钟共推进 {clock.now - 1000.0:.2f}s；"
            f"CHANGE-REQUEST 调用={change_calls}；调用序列：{fake.trace()}"
        )

        self.assertEqual(
            result.filtering_behavior,
            FILT_ADF,
            "change_ip 被挡、change_port 放行 —— 真值是地址受限锥型。若这里变成 apdf，"
            "说明 Test III 的超时是因为**总预算不够、单包只等了很短时间**，"
            "而不是 NAT 真在过滤。修法：保证单包最小超时（如 ≥1.0 s），"
            "预算不足时延长总预算，而不是压缩单包。" + detail,
        )
        self.assertEqual(
            result.classic_type,
            CLASSIC_RESTRICTED,
            "受限锥型被预算耗尽推成了端口受限锥型（方向是让用户白折腾端口映射）。" + detail,
        )

    def test_same_topology_without_budget_pressure_stays_adf(self):
        """对照组：同样的拓扑与回包时延，预算充足时必须稳定得到 adf。

        没有这条，上面那条挂了也分不清是"预算问题"还是"拓扑/替身构造错"。
        """
        clock = FakeClock()
        fake = FakeStunNetwork(
            PRIMARY,
            other=OTHER,
            filter_mode="adf",
            other_unreachable=True,
            response_latency=self.LATENCY_S,
            clock=clock,
        )
        probes = [make_probe(*PRIMARY), make_probe(*SECOND, rfc5780=False, rtt=30.0)]
        result = run_detector(fake, probes, clock=clock)  # 用生产默认预算

        detail = (
            f"filtering={result.filtering_behavior!r} classic={result.classic_type!r}；"
            f"时钟共推进 {clock.now - 1000.0:.2f}s；调用序列：{fake.trace()}"
        )
        self.assertEqual(
            result.filtering_behavior,
            FILT_ADF,
            "预算充足时（生产默认值）必须得到 adf —— 若这里也失败，说明问题不在预算，"
            "而在拓扑构造或回包时延模型。" + detail,
        )
        self.assertEqual(result.classic_type, CLASSIC_RESTRICTED, detail)


# ---------------------------------------------------------------------------
# P1-18
# ---------------------------------------------------------------------------
class P1_18MappingOutlierTest(unittest.TestCase):
    """P1-18：单条离群映射样本不得把 EIM 翻转成对称型。

    现象（v1.0.0）：过滤测试与映射采样共用同一个 socket 时，被 CHANGE-REQUEST
    碰过的那个目的地址会让 NAT 重新分配映射端口，于是采样里混进一个"端口不同"的
    样本；`_mapping_mode` 只要发现"端口不全相同"就走 ADM/APDM → 判成**对称型**，
    而真值是同一条 EIM 通道。结论还会随"哪台服务器被选作 cr_server"漂移
    （cr_server 不同 → 被污染的样本不同 → 结论不同），同一次网络、连测几次都不一样。

    v1.1 的两层修复：
      1. 结构性：过滤测量挪到独立 socket（见 `P0_1MeasurementChannelTest`）；
      2. 兜底：`_mapping_mode` 改为**多数端口**判据，容许最多一个离群样本。
    本用例锁的是第 2 层 —— 即使真的出现离群样本，也不能翻转结论。
    """

    THIRD = ("198.51.100.90", 3478)  # 第三台服务器：它的映射端口"离群"

    def test_single_outlier_sample_must_not_flip_eim_into_symmetric(self):
        """3 个样本里 1 个端口离群 → 仍应判 EIM / 受限锥型，而不是对称型。"""
        fake = FakeStunNetwork(
            PRIMARY,
            other=OTHER,
            filter_mode="adf",
            other_unreachable=True,  # 隔离 P0-1 的预热，只留"离群样本"这一个变量
            odd_dest=self.THIRD,
            odd_port=40099,
        )
        probes = [
            make_probe(*PRIMARY),
            make_probe(*SECOND, rfc5780=False, rtt=30.0),
            make_probe(*self.THIRD, rfc5780=False, rtt=40.0),
        ]
        result = run_detector(fake, probes)
        ports = [(s.dst_ip, s.mapped_port) for s in result.samples]
        detail = (
            f"mapping={result.mapping_behavior!r} filtering={result.filtering_behavior!r} "
            f"classic={result.classic_type!r}；样本(目的IP, 映射端口)={ports}；"
            f"调用序列：{fake.trace()}"
        )

        self.assertEqual(
            result.mapping_behavior,
            MAP_EIM,
            "3 个样本里 2 个端口一致（40001）、1 个离群（40099）—— 多数端口一致即 EIM。"
            "离群值是测量伪影（该目的刚被 CHANGE-REQUEST 碰过），"
            "不是「映射随目的变化」。"
            + detail,
        )
        self.assertNotEqual(
            result.classic_type,
            CLASSIC_SYMMETRIC,
            "单个离群样本把结论翻成对称型，会误导用户去开端口映射 / 用中继。" + detail,
        )
        self.assertEqual(
            result.classic_type,
            CLASSIC_RESTRICTED,
            "EIM + ADF → 受限锥型。" + detail,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)