# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""视图模型：把检测结果归一成「该画什么」的纯数据。

分层约定（v1.1 起强制）：

* 本模块 —— **该画什么**。只做数据建模，不碰任何像素，也不做任何排版；
* :mod:`natscope.report` —— **怎么画**（Pillow，导出 JPG）；
* :mod:`natscope.gui` —— **怎么画**（tkinter Canvas，界面上实时显示）。

报告与界面消费同一份模型，所以「截图上看到的」与「导出的图片里画的」不可能
各说一套。硬约束：**本模块不得导入 tkinter 或 PIL**，导入期必须零 GUI 依赖
（``tests/test_viz.py`` 里有运行期断言，别绕过）。

配色不在这里落地。这里只给出语义色调 ``tone``（``ok`` / ``warn`` / ``bad``
/ ``na``），由消费方映射到 :class:`natscope.theme.Palette` 的具体颜色——
深色与浅色两套主题因此自动跟随。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .nat import (
    CLASSIC_BLOCKED,
    CLASSIC_CONE_UNKNOWN,
    CLASSIC_FULL_CONE,
    CLASSIC_OPEN,
    CLASSIC_PORT_RESTRICTED,
    CLASSIC_RESTRICTED,
    CLASSIC_SYMMETRIC,
    CLASSIC_UNKNOWN,
    FILT_ADF,
    FILT_APDF,
    FILT_EIF,
    MAP_ADM,
    MAP_APDM,
    MAP_EIM,
    NatResult,
)

if TYPE_CHECKING:  # 只借类型，运行期不把 gateway 拉进 sys.modules
    from .gateway import GatewayReport

# ---- 单元格状态 -----------------------------------------------------------

#: 当前结论精确落在这一格
STATE_ACTIVE = "active"
#: 结论落在这一片区域内，但无法细分（例如过滤行为不可测的锥型）
STATE_RELATED = "related"
#: 与结论无关、但仍需展示（灰显）
STATE_DIM = "dim"
#: 该组合在本次网络环境下不适用
STATE_NA = "na"

# ---- 语义色调 -------------------------------------------------------------

TONE_OK = "ok"
TONE_WARN = "warn"
TONE_BAD = "bad"
TONE_NA = "na"

#: 候选态的统一色调。
#:
#: 候选意味着「无法细分」，此时各格/各段自带的类型色（绿色＝好、橙色＝存疑）
#: 会读出「绿色那个更可能是对的」这种错误信息，所以**渲染时一律忽略
#: 各自的 tone**，统一用本色调。消费方请用 :attr:`Cell.display_tone`
#: 或直接引用本常量，不要拿 ``cell.tone`` 去上色。
CANDIDATE_TONE = TONE_WARN

#: 传统类型 -> 语义色调。全项目**唯一**的类型语义映射，
#: ``theme.type_color`` 负责把它翻译成具体颜色。
TONE_BY_TYPE: dict[str, str] = {
    CLASSIC_OPEN: TONE_OK,
    CLASSIC_FULL_CONE: TONE_OK,
    CLASSIC_RESTRICTED: TONE_WARN,
    CLASSIC_PORT_RESTRICTED: TONE_WARN,
    CLASSIC_CONE_UNKNOWN: TONE_WARN,
    CLASSIC_SYMMETRIC: TONE_BAD,
    CLASSIC_BLOCKED: TONE_BAD,
    CLASSIC_UNKNOWN: TONE_NA,
}

# ---- 行为矩阵的坐标与文案 -------------------------------------------------

#: 行 = 映射行为（RFC 5780 §5 的三种）
MATRIX_ROWS: tuple[str, ...] = (MAP_EIM, MAP_ADM, MAP_APDM)
#: 列 = 过滤行为（RFC 5780 §6 的三种）
MATRIX_COLS: tuple[str, ...] = (FILT_EIF, FILT_ADF, FILT_APDF)

ROW_LABEL: dict[str, str] = {
    MAP_EIM: "端点无关映射",
    MAP_ADM: "地址相关映射",
    MAP_APDM: "地址与端口相关映射",
}
#: 矩阵行头用的短标签（矩阵行头很窄，全称会顶到格子）
ROW_SHORT: dict[str, str] = {
    MAP_EIM: "端点无关",
    MAP_ADM: "地址相关",
    MAP_APDM: "地址与端口",
}
ROW_CODE: dict[str, str] = {MAP_EIM: "EIM", MAP_ADM: "ADM", MAP_APDM: "APDM"}
COL_LABEL: dict[str, str] = {
    FILT_EIF: "端点无关过滤",
    FILT_ADF: "地址相关过滤",
    FILT_APDF: "地址与端口相关过滤",
}
COL_CODE: dict[str, str] = {FILT_EIF: "EIF", FILT_ADF: "ADF", FILT_APDF: "APDF"}

#: 供卡片/下拉框直接显示的短标签（带英文缩写）
MAPPING_LABEL: dict[str, str] = {
    MAP_EIM: "端点无关映射 EIM",
    MAP_ADM: "地址相关映射 ADM",
    MAP_APDM: "地址与端口相关映射 APDM",
    "unknown": "不可测",
}
FILTERING_LABEL: dict[str, str] = {
    FILT_EIF: "端点无关过滤 EIF",
    FILT_ADF: "地址相关过滤 ADF",
    FILT_APDF: "地址与端口相关过滤 APDF",
    "unknown": "不可测",
}

#: (行, 列) -> (中文名, 英文名)。EIM 行的三格就是三种锥型；
#: ADM / APDM 两行的六格全部归「对称型」（RFC 5780 §5.3：映射端口随目的
#: 地址变化时，过滤行为不再影响传统四类型的判定）。
_CONE_TEXT: dict[tuple[str, str], tuple[str, str]] = {
    (MAP_EIM, FILT_EIF): ("完全锥型", "Full Cone"),
    (MAP_EIM, FILT_ADF): ("受限锥型", "Restricted Cone"),
    (MAP_EIM, FILT_APDF): ("端口受限锥型", "Port Restricted Cone"),
}
_SYMMETRIC_TEXT: tuple[str, str] = ("对称型", "Symmetric")

#: 从传统类型反推映射行为（只用于测量结果缺失时兜底，不做推测性区分）
_MAPPING_FROM_CLASSIC: dict[str, str] = {
    CLASSIC_OPEN: MAP_EIM,
    CLASSIC_FULL_CONE: MAP_EIM,
    CLASSIC_RESTRICTED: MAP_EIM,
    CLASSIC_PORT_RESTRICTED: MAP_EIM,
    CLASSIC_CONE_UNKNOWN: MAP_EIM,
}
#: 从传统类型反推过滤行为
_FILTERING_FROM_CLASSIC: dict[str, str] = {
    CLASSIC_FULL_CONE: FILT_EIF,
    CLASSIC_RESTRICTED: FILT_ADF,
    CLASSIC_PORT_RESTRICTED: FILT_APDF,
}

_MAPPINGS = (MAP_EIM, MAP_ADM, MAP_APDM)
_FILTERINGS = (FILT_EIF, FILT_ADF, FILT_APDF)


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Cell:
    """行为矩阵中的一格：一次「映射 × 过滤」组合。

    ``state`` 决定怎么强调（是否高亮），``tone`` 决定用什么语义色；
    两者正交——灰显的格子照样带 tone，换主题时不需要重新计算。

    **上色请用** :attr:`display_tone` **而不是** ``tone``：候选格（``related``）
    必须忽略各自的类型色，否则「完全锥型」那格会显示成绿色，读起来像
    「这个更可能」。
    """

    row: str
    col: str
    title: str
    sub: str
    state: str
    tone: str

    @property
    def display_tone(self) -> str:
        """渲染用色调：候选态统一 :data:`CANDIDATE_TONE`，其余用自身 tone。"""
        return CANDIDATE_TONE if self.state == STATE_RELATED else self.tone


@dataclass(frozen=True)
class Matrix:
    """3×3 行为矩阵。``note`` 为空串表示无需额外说明。"""

    rows: tuple[str, ...]
    cols: tuple[str, ...]
    cells: tuple[tuple[Cell, ...], ...]
    note: str

    def get(self, row: str, col: str) -> Cell:
        """按代码取格；代码非法时抛 :class:`KeyError`。"""
        i = self.rows.index(row)
        j = self.cols.index(col)
        return self.cells[i][j]

    def highlighted(self) -> tuple[Cell, ...]:
        """当前结论落在（或可能落在）的所有格子。"""
        return tuple(
            c for line in self.cells for c in line if c.state in (STATE_ACTIVE, STATE_RELATED)
        )


@dataclass(frozen=True)
class SpectrumSegment:
    """类型谱上的一段。

    元组顺序即「从最好到最差」的展示顺序（左 → 右），消费方按**下标**
    定位几何位置；``score`` 只用于附加的「P2P 友好度」数值展示。
    """

    key: str
    label: str
    sub: str
    tone: str
    state: str  # current / candidate / dim / na
    score: int
    hint: str


@dataclass(frozen=True)
class SpectrumSpan:
    """类型谱上一段**连续候选区间**。

    三个候选段各自画一个三角，会读成「三个都命中」或「三选一，挑一个」；
    正确含义是「落在这三段构成的区间里」。所以消费方应该画**一条横跨区间
    的加粗条 + 一个居中的三角**，而不是三段各画一个。
    """

    start: int
    end: int
    label: str

    @property
    def length(self) -> int:
        return self.end - self.start + 1


@dataclass(frozen=True)
class SampleChip:
    """一条映射样本：目的地址 → 公网映射。"""

    index: int
    server: str
    dst: str
    mapped: str
    stable: bool
    tone: str

    @property
    def text(self) -> str:
        return f"{self.dst} → {self.mapped}"


@dataclass(frozen=True)
class Badge:
    """网关端口映射能力徽标。``state`` 为 gateway.Capability 的三态。"""

    key: str
    label: str
    value: str
    detail: str
    tone: str
    state: str


@dataclass(frozen=True)
class Scenario:
    """一个 P2P 使用场景的可用性。"""

    key: str
    label: str
    sub: str
    level: str  # ok / warn / bad / na
    tone: str
    hint: str


# --------------------------------------------------------------------------
# 取色与判定
# --------------------------------------------------------------------------


def type_tone(classic_type: str) -> str:
    """传统类型 -> 语义色调，未知类型一律 ``na``。"""
    return TONE_BY_TYPE.get(classic_type, TONE_NA)


def _resolved_mapping(nat: NatResult) -> str:
    """优先采用实测的映射行为；缺失时按传统类型兜底（对称型不兜底）。"""
    if nat.mapping_behavior in _MAPPINGS:
        return nat.mapping_behavior
    return _MAPPING_FROM_CLASSIC.get(nat.classic_type, "")


def _resolved_filtering(nat: NatResult) -> str:
    if nat.filtering_behavior in _FILTERINGS:
        return nat.filtering_behavior
    return _FILTERING_FROM_CLASSIC.get(nat.classic_type, "")


# --------------------------------------------------------------------------
# 行为矩阵
# --------------------------------------------------------------------------


def build_matrix(nat: NatResult) -> Matrix:
    """构造 3×3 行为矩阵，并把当前结论标进去。

    判定顺序（先到先得）：

    1. ``blocked`` / ``open`` —— 行为模型不适用，9 格全部 ``na``；
    2. 映射与过滤行为都测得 —— **只有一格** ``active``（其余被实测排除，
       灰显而不是「可能」，把「可能」标成高亮会读出错误信息）；
    3. 映射行为测到、过滤行为不可测 —— 整行 ``related``：
       锥型行标「锥型（三选一，无法细分）」，对称型行标「ADM / APDM 未细分」；
    4. 映射行为不可测 —— 9 格 ``dim``，并在 ``note`` 里说明原因。
    """
    classic = nat.classic_type or CLASSIC_UNKNOWN
    mapping = _resolved_mapping(nat)
    filtering = _resolved_filtering(nat)

    def make(row: str, col: str, state: str) -> Cell:
        if row == MAP_EIM:
            title, sub = _CONE_TEXT[(row, col)]
        else:
            title, sub = _SYMMETRIC_TEXT
        tone = TONE_OK if (row, col) == (MAP_EIM, FILT_EIF) else (
            TONE_WARN if row == MAP_EIM else TONE_BAD
        )
        return Cell(row=row, col=col, title=title, sub=sub, state=state, tone=tone)

    note = ""

    if classic in (CLASSIC_BLOCKED, CLASSIC_OPEN):
        if classic == CLASSIC_BLOCKED:
            note = "UDP 出站被阻断，映射与过滤行为均不可测，行为矩阵不适用。"
        else:
            note = "本机直接持有公网地址（无 NAT），映射与过滤行为不适用。"
        cells = tuple(
            tuple(make(r, c, STATE_NA) for c in MATRIX_COLS) for r in MATRIX_ROWS
        )
        return Matrix(rows=MATRIX_ROWS, cols=MATRIX_COLS, cells=cells, note=note)

    if mapping in (MAP_ADM, MAP_APDM):
        rows = [mapping]
    elif mapping == "" and classic == CLASSIC_SYMMETRIC:
        # 只知是对称型、不知是 ADM 还是 APDM：两行同一列都可能
        rows = [MAP_ADM, MAP_APDM]
    elif mapping == MAP_EIM:
        rows = [MAP_EIM]
    else:
        rows = []

    grid: list[tuple[Cell, ...]] = []
    for row in MATRIX_ROWS:
        line: list[Cell] = []
        for col in MATRIX_COLS:
            if not rows or row not in rows:
                state = STATE_DIM
            elif filtering:
                # 映射与过滤都已知：只有交叉的那一格是结论，其余被实测排除
                state = STATE_ACTIVE if col == filtering else STATE_DIM
            else:
                # 过滤行为不可测：整行都是候选，无法再细分
                state = STATE_RELATED
            line.append(make(row, col, state))
        grid.append(tuple(line))

    # ---- note：只在**一个**地方按「特例在前、通用在后」赋值 ----
    #
    # 这里曾经在两处各赋一次（rows 构造处一处、这里一处），后写的那次会把
    # 前写的整个盖住：于是「对称型未细分」的文案永远执行不到，而且覆盖后还
    # 说错了事实（两行是「两种可能」，不是「反推出来的某一行」）。死代码本身
    # 不可怕，可怕的是「我们以为它会执行」——所以收敛成单一赋值点并按优先级
    # 排好，任何一条分支都只可能被一个条件命中。
    #
    # 「均已测得」只能基于**实测字段**判断：viz 的 mapping / filtering 可能是
    # 从 classic_type 反推来的（_MAPPING_FROM_CLASSIC / _FILTERING_FROM_CLASSIC），
    # 拿反推值说"已测得"就是在陈述一句不成立的事实，还会和同一份报告页脚的
    # 警告（"映射行为未能测量"）直接打架：假话在图下方，真话被埋在页脚。
    #
    # 反推链是 classic_type → mapping/filtering（两跳里的第二跳），不存在
    # "从 filtering 直接推 mapping"的路径，所以文案写「由传统类型反推」。
    # 状态标记（如行头的「·反推」）留给渲染层，正文只负责把事实说清楚。
    mapping_measured = nat.mapping_behavior in _MAPPINGS
    filtering_measured = nat.filtering_behavior in _FILTERINGS
    if classic == CLASSIC_SYMMETRIC and not mapping_measured:
        # 特例优先：两行是「两种可能」，不是「反推出来的某一行」
        note = "对称型：映射行为未细分到 ADM / APDM，两行同一列均可能。"
    elif not rows:
        note = "映射行为不可测（可用服务器不足），行为矩阵没有确定位置。"
    elif not filtering:
        # 映射已测、过滤不可测：整行 related 已经把「候选」画出来了，
        # 锥型行再加一句，其余（对称型已细分）不需要额外说明。
        if mapping == MAP_EIM:
            # 措辞必须与类型谱的区间标签（spectrum_span().label）前半句逐字一致：
            # 同一张图里两套说法会被读成「两个不同的状态」。
            note = (
                "锥型（三选一，无法细分）：过滤行为不可测，"
                "无法确认是完全 / 受限 / 端口受限锥型。"
            )
    elif mapping_measured and filtering_measured:
        note = "映射与过滤行为均已测得，结论落在高亮格。"
    elif filtering_measured:
        # P0-2 兜底分支：classic 由过滤行为推出，mapping 仍是 unknown（真机可达）
        note = "映射行为未测得，矩阵行由传统类型反推，列由实测过滤行为确定。"
    elif mapping_measured:
        # mapping 实测、filtering 反推：行是实测的，列才是推出来的
        note = "过滤行为未测得，矩阵列由传统类型反推，行由实测映射行为确定。"
    else:
        # 两个字段都靠反推（如只记录 classic_type 的旧数据）
        note = "映射与过滤行为均未测得，矩阵由传统类型反推定位。"

    return Matrix(rows=MATRIX_ROWS, cols=MATRIX_COLS, cells=tuple(grid), note=note)


# --------------------------------------------------------------------------
# 类型谱
# --------------------------------------------------------------------------

#: (key, 中文, 英文, tone, P2P 友好度, 一句话说明)，顺序即展示顺序（好 → 差）
_SPECTRUM_SPEC: tuple[tuple[str, str, str, str, int, str], ...] = (
    (CLASSIC_OPEN, "开放互联网", "Open", TONE_OK, 100, "无需穿透，全部 P2P 场景可直连"),
    (CLASSIC_FULL_CONE, "完全锥型", "Full Cone", TONE_OK, 88, "任意对端知道映射地址即可连入"),
    (CLASSIC_RESTRICTED, "受限锥型", "Restricted", TONE_WARN, 68, "已通信对端可回连，被动访问需映射"),
    (
        CLASSIC_PORT_RESTRICTED,
        "端口受限锥型",
        "Port Restricted",
        TONE_WARN,
        46,
        "需同 IP 同端口，联机常显示「中等」",
    ),
    (CLASSIC_SYMMETRIC, "对称型", "Symmetric", TONE_BAD, 20, "每个目的地独立端口，穿透依赖中继"),
    (CLASSIC_BLOCKED, "UDP 阻断", "Blocked", TONE_BAD, 0, "UDP 出站被封，P2P 基本不可用"),
)

#: 锥型无法细分时，可能落在哪三段
_CONE_KEYS = (CLASSIC_FULL_CONE, CLASSIC_RESTRICTED, CLASSIC_PORT_RESTRICTED)


def build_spectrum(nat: NatResult) -> tuple[SpectrumSegment, ...]:
    """构造「从开放到阻断」的类型谱，并标出当前类型。

    ``cone_unknown`` 会把三种锥型一起标成 ``candidate``——这正是不确定性
    的可视化表达：用户看到的是「三个格子都亮着」，而不是一个假的精确结论。
    """
    classic = nat.classic_type or CLASSIC_UNKNOWN
    segments: list[SpectrumSegment] = []
    for key, label, sub, tone, score, hint in _SPECTRUM_SPEC:
        if classic == key:
            state = "current"
        elif classic == CLASSIC_CONE_UNKNOWN and key in _CONE_KEYS:
            state = "candidate"
        elif classic == CLASSIC_UNKNOWN:
            state = "dim"
        else:
            state = "dim"
        segments.append(
            SpectrumSegment(
                key=key,
                label=label,
                sub=sub,
                tone=tone,
                state=state,
                score=score,
                hint=hint,
            )
        )
    return tuple(segments)


def spectrum_span(segments: tuple[SpectrumSegment, ...]) -> SpectrumSpan | None:
    """取出连续 ``candidate`` 段构成的区间；没有候选段时返回 ``None``。

    目前只有「锥型不可细分」会产生候选区间（完全锥型 / 受限锥型 /
    端口受限锥型三段），标签直接写成「锥型（三选一，无法细分）」；
    其他情况按段名拼接，保证函数本身不绑死在某一种结论上。
    """
    idx = [i for i, s in enumerate(segments) if s.state == "candidate"]
    if not idx:
        return None
    start, end = idx[0], idx[-1]
    keys = [s.key for s in segments[start:end + 1]]
    if keys == list(_CONE_KEYS):
        label = "锥型（三选一，无法细分）"
    else:
        label = " / ".join(s.label for s in segments[start:end + 1])
    return SpectrumSpan(start=start, end=end, label=label)


# --------------------------------------------------------------------------
# 映射样本
# --------------------------------------------------------------------------


def build_samples(nat: NatResult) -> tuple[SampleChip, ...]:
    """把映射样本整理成可直接渲染的 chip 列表。

    ``stable`` 表示该样本的映射端口与首个样本一致——不一致就是 NAT 端口
    随目的地址漂移的直接证据（对称型的判据）。
    """
    if not nat.samples:
        return ()
    first = nat.samples[0].mapped_port
    chips: list[SampleChip] = []
    for i, s in enumerate(nat.samples, start=1):
        stable = s.mapped_port == first
        chips.append(
            SampleChip(
                index=i,
                server=s.server or "",
                dst=f"{s.dst_ip}:{s.dst_port}",
                mapped=f"{s.mapped_ip}:{s.mapped_port}",
                stable=stable,
                tone=TONE_OK if stable else TONE_WARN,
            )
        )
    return tuple(chips)


# --------------------------------------------------------------------------
# 网关徽标
# --------------------------------------------------------------------------

_BADGE_LABEL: dict[str, str] = {
    "UPnP-IGD": "UPnP",
    "NAT-PMP": "NAT-PMP",
    "PCP": "PCP",
}

_TONE_BY_CAP_STATE: dict[str, str] = {
    "ok": TONE_OK,
    "limited": TONE_WARN,
    "absent": TONE_NA,
    "unknown": TONE_NA,
}


def build_badges(gw: GatewayReport | None) -> tuple[Badge, ...]:
    """网关三类端口映射服务 -> 徽标。

    ``gw`` 为 ``None``（本次未探测）时也返回**一个**徽标而不是空元组：
    消费方不必为「空列表」写特例，直接渲染即可。
    """
    if gw is None:
        return (
            Badge(
                key="gateway",
                label="端口映射",
                value="未检测",
                detail="本次未执行网关探测",
                tone=TONE_NA,
                state="unknown",
            ),
        )
    badges: list[Badge] = []
    for cap in gw.capabilities:
        badges.append(
            Badge(
                key=cap.name.lower().replace("-", ""),
                label=_BADGE_LABEL.get(cap.name, cap.name),
                value=cap.state_label,
                detail=cap.detail or "",
                tone=_TONE_BY_CAP_STATE.get(cap.state, TONE_NA),
                state=cap.state,
            )
        )
    return tuple(badges)


# --------------------------------------------------------------------------
# 场景可用性
# --------------------------------------------------------------------------

#: 场景键 -> (中文, 英文)
SCENARIO_LABEL: dict[str, tuple[str, str]] = {
    "game": ("游戏联机", "P2P Game"),
    "bittorrent": ("BT 下载", "BitTorrent"),
    "remote": ("远程访问", "Inbound"),
    "voice": ("语音 / 视频", "VoIP"),
}

#: 等级 -> 一句话
LEVEL_LABEL: dict[str, str] = {
    "ok": "可直连",
    "warn": "受限",
    "bad": "不可用",
    "na": "未知",
}

#: 传统类型 -> (游戏联机, BT 下载, 远程访问(被动), 语音/视频) 的可用性等级
_AVAILABILITY: dict[str, tuple[str, str, str, str]] = {
    CLASSIC_OPEN: ("ok", "ok", "ok", "ok"),
    CLASSIC_FULL_CONE: ("ok", "ok", "ok", "ok"),
    CLASSIC_RESTRICTED: ("ok", "ok", "warn", "ok"),
    CLASSIC_PORT_RESTRICTED: ("warn", "warn", "bad", "warn"),
    CLASSIC_SYMMETRIC: ("bad", "warn", "bad", "bad"),
    CLASSIC_CONE_UNKNOWN: ("ok", "ok", "warn", "ok"),
    CLASSIC_BLOCKED: ("bad", "bad", "bad", "bad"),
    CLASSIC_UNKNOWN: ("na", "na", "na", "na"),
}


def build_scenarios(nat: NatResult) -> tuple[Scenario, ...]:
    """按传统类型查表给出四个典型 P2P 场景的可用性。"""
    levels = _AVAILABILITY.get(nat.classic_type or CLASSIC_UNKNOWN, _AVAILABILITY[CLASSIC_UNKNOWN])
    out: list[Scenario] = []
    for key, level in zip(SCENARIO_LABEL, levels):
        label, sub = SCENARIO_LABEL[key]
        out.append(
            Scenario(
                key=key,
                label=label,
                sub=sub,
                level=level,
                tone=level,  # 等级与色调同名，消费方直接映射
                hint=LEVEL_LABEL[level],
            )
        )
    return tuple(out)


# --------------------------------------------------------------------------
# 结论
# --------------------------------------------------------------------------

#: 传统类型 -> (标题, 副标题, tone)
_VERDICT: dict[str, tuple[str, str, str]] = {
    CLASSIC_OPEN: ("开放互联网", "Open Internet · 无需 NAT 穿透", TONE_OK),
    CLASSIC_FULL_CONE: ("完全锥型 NAT", "Full Cone · 穿透成功率最高", TONE_OK),
    CLASSIC_RESTRICTED: ("受限锥型 NAT", "Restricted Cone · 主动通信后可回连", TONE_WARN),
    CLASSIC_PORT_RESTRICTED: (
        "端口受限锥型 NAT",
        "Port Restricted Cone · 需同 IP 同端口",
        TONE_WARN,
    ),
    CLASSIC_CONE_UNKNOWN: ("锥型 NAT（未细分）", "Cone · 过滤行为不可测", TONE_WARN),
    CLASSIC_SYMMETRIC: ("对称型 NAT", "Symmetric · 每目的地址独立映射", TONE_BAD),
    CLASSIC_BLOCKED: ("UDP 被阻断", "Blocked · 仅 TCP 可用", TONE_BAD),
    CLASSIC_UNKNOWN: ("无法判定", "Unknown · 可用服务器不足", TONE_NA),
}


def verdict(nat: NatResult) -> tuple[str, str, str]:
    """返回结论大字所需的 (标题, 副标题, tone)。"""
    return _VERDICT.get(nat.classic_type or CLASSIC_UNKNOWN, _VERDICT[CLASSIC_UNKNOWN])


__all__ = [
    "Badge",
    "CANDIDATE_TONE",
    "COL_CODE",
    "COL_LABEL",
    "Cell",
    "FILTERING_LABEL",
    "LEVEL_LABEL",
    "MAPPING_LABEL",
    "MATRIX_COLS",
    "MATRIX_ROWS",
    "Matrix",
    "ROW_CODE",
    "ROW_LABEL",
    "ROW_SHORT",
    "SCENARIO_LABEL",
    "STATE_ACTIVE",
    "STATE_DIM",
    "STATE_NA",
    "STATE_RELATED",
    "SampleChip",
    "Scenario",
    "SpectrumSegment",
    "SpectrumSpan",
    "TONE_BAD",
    "TONE_BY_TYPE",
    "TONE_NA",
    "TONE_OK",
    "TONE_WARN",
    "build_badges",
    "build_matrix",
    "build_samples",
    "build_scenarios",
    "build_spectrum",
    "spectrum_span",
    "type_tone",
    "verdict",
]
