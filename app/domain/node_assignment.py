"""M4LAPS ノード割当ドメイン（純粋・DB非依存・テスト対象）

MACアドレスを一次識別子とし、実行時に node_id(0..11) を割り当てる方式の
「割当が妥当か」を判定する純粋関数群。firmware/common/protocol.h の
node_id_matches_kind() と同じレンジ規則をサーバー側にも置き、二重で防ぐ。

種別(kind)はコンパイル時にファームで固定される。番号(node_id)は本モジュールの
規則に従ってアプリ/GWが実行時に割り当てる。ヘッダ(20バイト)には一切影響しない。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class NodeKind(IntEnum):
    """protocol.h の NodeKind と一致させること。"""
    GW = 0
    SQ = 1
    RC = 2
    SG = 3


# 種別ごとに割り当て可能な node_id レンジ（timing_devices の固定12台と一致）
#   SQ0..SQ5 = 0..5 / GW6,GW7 = 6,7 / RC8,RC9 = 8,9 / SG10,SG11 = 10,11
KIND_ID_RANGE: dict[NodeKind, range] = {
    NodeKind.SQ: range(0, 6),    # 0,1,2,3,4,5
    NodeKind.GW: range(6, 8),    # 6,7
    NodeKind.RC: range(8, 10),   # 8,9
    NodeKind.SG: range(10, 12),  # 10,11
}

NODE_ID_UNASSIGNED = 0xFE
NODE_ID_BROADCAST = 0xFF


class BindReason(IntEnum):
    """protocol.h PayloadJoinAck.reason と一致させること。"""
    OK = 0
    RANGE = 1      # 種別に対して node_id がレンジ外
    DUP = 2        # その node_id は別MACに割当済み
    FULL = 3       # 種別の空き番号が無い（将来用）
    BAD_MAC = 4    # MAC書式不正
    REBIND = 5     # 同一MACが別番号に割当済み（付け替え要確認）


@dataclass(frozen=True)
class BindResult:
    accepted: bool
    reason: BindReason

    @property
    def ok(self) -> bool:
        return self.accepted


def kind_of_node_id(node_id: int) -> NodeKind | None:
    """node_id から種別を逆引き（0..11 のみ）。範囲外は None。"""
    for kind, rng in KIND_ID_RANGE.items():
        if node_id in rng:
            return kind
    return None


def node_id_matches_kind(node_id: int, kind: NodeKind) -> bool:
    """protocol.h の同名関数と同じ判定。"""
    rng = KIND_ID_RANGE.get(kind)
    return rng is not None and node_id in rng


def normalize_mac(mac: str) -> str | None:
    """'AA:BB:...' / 'aabb...' / '-' 区切り等を 'aa:bb:cc:dd:ee:ff' に正規化。
    妥当でなければ None。全ゼロMACは不正扱い（未初期化のため）。
    """
    if not mac:
        return None
    hexchars = [c for c in mac.lower() if c in "0123456789abcdef"]
    if len(hexchars) != 12:
        return None
    octets = ["".join(hexchars[i:i + 2]) for i in range(0, 12, 2)]
    if all(o == "00" for o in octets):
        return None
    return ":".join(octets)


def validate_bind(
    *,
    kind: NodeKind,
    node_id: int,
    mac: str,
    current_bindings: dict[int, str],
) -> BindResult:
    """MAC を node_id に割り当ててよいかを判定する（純粋）。

    kind             : 名乗り出たノードの種別（ファーム固定）
    node_id          : 割り当てたい番号（0..11）
    mac              : そのノードのMAC（正規化前でよい）
    current_bindings : いま確定している {node_id: mac} の対応（timing_devices由来）

    受理条件（すべて満たす）:
      1) MACが妥当
      2) node_id が kind のレンジ内
      3) その node_id が空き、または既に同じMACに割当済み（冪等）
      4) 同じMACが別の node_id に割当済みでない（付け替えは REBIND として拒否）
    """
    norm = normalize_mac(mac)
    if norm is None:
        return BindResult(False, BindReason.BAD_MAC)

    if not node_id_matches_kind(node_id, kind):
        return BindResult(False, BindReason.RANGE)

    # 正規化した対応表で比較
    normalized = {nid: normalize_mac(m) for nid, m in current_bindings.items() if m}

    # 同じMACが別番号に既にいる → 付け替えは明示操作にさせる
    for nid, m in normalized.items():
        if m == norm and nid != node_id:
            return BindResult(False, BindReason.REBIND)

    occupant = normalized.get(node_id)
    if occupant is not None and occupant != norm:
        return BindResult(False, BindReason.DUP)

    return BindResult(True, BindReason.OK)


def suggest_node_id(
    *,
    kind: NodeKind,
    current_bindings: dict[int, str],
) -> int | None:
    """その種別の空き番号のうち最小のものを返す（UIの初期候補用）。
    空きが無ければ None。
    """
    rng = KIND_ID_RANGE.get(kind)
    if rng is None:
        return None
    used = {nid for nid, m in current_bindings.items() if m}
    for nid in rng:
        if nid not in used:
            return nid
    return None
