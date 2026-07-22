"""node_assignment の単体テスト（実機不要・pytest）。"""
from app.domain.node_assignment import (
    BindReason,
    NodeKind,
    kind_of_node_id,
    node_id_matches_kind,
    normalize_mac,
    suggest_node_id,
    validate_bind,
)


# ---- normalize_mac ----------------------------------------------------------
def test_normalize_various_formats():
    assert normalize_mac("AA:BB:CC:DD:EE:FF") == "aa:bb:cc:dd:ee:ff"
    assert normalize_mac("aabbccddeeff") == "aa:bb:cc:dd:ee:ff"
    assert normalize_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"


def test_normalize_rejects_bad():
    assert normalize_mac("") is None
    assert normalize_mac("AA:BB:CC") is None            # 桁不足
    assert normalize_mac("00:00:00:00:00:00") is None   # 全ゼロは未初期化扱い
    assert normalize_mac("zz:bb:cc:dd:ee:ff") is None   # 非16進


# ---- kind / range -----------------------------------------------------------
def test_kind_of_node_id():
    assert kind_of_node_id(0) == NodeKind.SQ
    assert kind_of_node_id(5) == NodeKind.SQ
    assert kind_of_node_id(6) == NodeKind.GW
    assert kind_of_node_id(7) == NodeKind.GW
    assert kind_of_node_id(8) == NodeKind.RC
    assert kind_of_node_id(10) == NodeKind.SG
    assert kind_of_node_id(11) == NodeKind.SG
    assert kind_of_node_id(12) is None
    assert kind_of_node_id(0xFE) is None


def test_node_id_matches_kind():
    assert node_id_matches_kind(3, NodeKind.SQ)
    assert not node_id_matches_kind(6, NodeKind.SQ)   # 6はGW枠
    assert node_id_matches_kind(6, NodeKind.GW)
    assert not node_id_matches_kind(8, NodeKind.GW)   # 8はRC枠


# ---- validate_bind ----------------------------------------------------------
MAC_A = "aa:aa:aa:aa:aa:aa"
MAC_B = "bb:bb:bb:bb:bb:bb"


def test_bind_ok_into_empty_slot():
    r = validate_bind(kind=NodeKind.SQ, node_id=0, mac=MAC_A, current_bindings={})
    assert r.ok and r.reason == BindReason.OK


def test_bind_range_error():
    # SQ を GW 枠(6)へは割り当てられない
    r = validate_bind(kind=NodeKind.SQ, node_id=6, mac=MAC_A, current_bindings={})
    assert not r.ok and r.reason == BindReason.RANGE


def test_bind_dup_slot_taken_by_other_mac():
    r = validate_bind(
        kind=NodeKind.SQ, node_id=0, mac=MAC_A,
        current_bindings={0: MAC_B},
    )
    assert not r.ok and r.reason == BindReason.DUP


def test_bind_idempotent_same_mac_same_slot():
    # 同じMACを同じ番号へ再割当は冪等でOK
    r = validate_bind(
        kind=NodeKind.SQ, node_id=0, mac=MAC_A,
        current_bindings={0: MAC_A},
    )
    assert r.ok and r.reason == BindReason.OK


def test_bind_rebind_same_mac_other_slot_rejected():
    # 同じMACが別番号にいる → 付け替えは明示操作にさせる
    r = validate_bind(
        kind=NodeKind.SQ, node_id=1, mac=MAC_A,
        current_bindings={0: MAC_A},
    )
    assert not r.ok and r.reason == BindReason.REBIND


def test_bind_bad_mac():
    r = validate_bind(kind=NodeKind.SQ, node_id=0, mac="nope", current_bindings={})
    assert not r.ok and r.reason == BindReason.BAD_MAC


def test_bind_normalizes_before_compare():
    # 大文字/区切り違いでも同一MACとみなす（冪等）
    r = validate_bind(
        kind=NodeKind.SQ, node_id=0, mac="AA-AA-AA-AA-AA-AA",
        current_bindings={0: "aa:aa:aa:aa:aa:aa"},
    )
    assert r.ok and r.reason == BindReason.OK


# ---- suggest_node_id --------------------------------------------------------
def test_suggest_lowest_free():
    assert suggest_node_id(kind=NodeKind.SQ, current_bindings={}) == 0
    assert suggest_node_id(kind=NodeKind.SQ, current_bindings={0: MAC_A}) == 1
    assert suggest_node_id(
        kind=NodeKind.GW, current_bindings={6: MAC_A}
    ) == 7


def test_suggest_full_returns_none():
    full = {i: f"aa:aa:aa:aa:aa:{i:02x}" for i in range(0, 6)}
    assert suggest_node_id(kind=NodeKind.SQ, current_bindings=full) is None
