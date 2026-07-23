"""M4LAPS ノード割当リポジトリ（既存 timing_repository への追加分）

既存の timing_devices（固定12台・node_id/kind固定・mac編集可）は書き換えず、
その mac 列を「割当の確定先」として使う。加えて timing_unassigned を新設し、
まだ番号に紐づいていないノード（GWがJOINで拾ったMAC）を貯める。

⚠ 統合方法：既存 timing_repository.py が持つDB接続関数（get_conn 等）に合わせて
   _conn() を差し替えること。ここでは sqlite3 を直接使う最小実装にしてある。
   ensure_schema() は既存の schema 初期化から呼ぶか、起動時に1回呼ぶ。
"""
from __future__ import annotations

import sqlite3
import time

from app.domain.node_assignment import (
    BindReason,
    NodeKind,
    normalize_mac,
    validate_bind,
)

def _conn() -> sqlite3.Connection:
    """アプリ本体と同じDBに接続する。

    ⚠ 以前は環境変数 MINIYONKU_DB から固定パスを読んでいたが、
       アプリ本体は data/miniyonku.db を使い、さらにクラウド版では
       **店舗ごとにDBが分かれる**（current_db_path が店舗DBを返す）。
       固定パスのままだと別のDBを見てしまい、割当が反映されない・
       他店舗のデータを触るといった事故になるため、本体と同じ解決に合わせる。
    """
    from app.infrastructure.db.connection import current_db_path

    c = sqlite3.connect(current_db_path())
    c.row_factory = sqlite3.Row
    return c


def ensure_schema() -> None:
    """未割当ノード表を作る（IF NOT EXISTS）。既存表には触れない。"""
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS timing_unassigned (
                mac          TEXT PRIMARY KEY,   -- 正規化済み aa:bb:...
                kind         INTEGER NOT NULL,   -- NodeKind
                fw_major     INTEGER DEFAULT 0,
                fw_minor     INTEGER DEFAULT 0,
                nvs_node_id  INTEGER DEFAULT 254,-- ノードがNVSに持っていた番号(0xFE=無)
                last_seen    INTEGER NOT NULL,   -- epoch秒
                seen_count   INTEGER DEFAULT 1
            )
            """
        )
        c.commit()


# ---- 既存 timing_devices を読む（node_id -> mac の現状） ---------------------
def current_bindings() -> dict[int, str]:
    """timing_devices の {node_id: mac}（mac未設定は除外）。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT node_id, mac FROM timing_devices WHERE mac IS NOT NULL AND mac != ''"
        ).fetchall()
    return {int(r["node_id"]): r["mac"] for r in rows}


def assignments_map() -> list[dict]:
    """GWが取得する MAC->node_id の一覧（オンライン時にキャッシュさせる）。

    ⚠ timing_devices.kind は 'SQ'/'GW'/'RC'/'SG' の**文字列**で保存されている。
       GWへは protocol.h の NodeKind（数値）で返す必要があるため、ここで変換する。
       （以前は int(r["kind"]) としており、実機のJOIN時に必ず落ちていた）
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT node_id, kind, mac FROM timing_devices "
            "WHERE mac IS NOT NULL AND mac != '' ORDER BY node_id"
        ).fetchall()

    out = []
    for r in rows:
        raw = r["kind"]
        try:
            # 文字列('SQ')でも数値(1)でも受け付ける
            kind_val = int(raw)
        except (TypeError, ValueError):
            try:
                kind_val = int(NodeKind[str(raw).strip().upper()])
            except KeyError:
                continue          # 未知の種別は返さない（GWを混乱させない）
        out.append({"node_id": int(r["node_id"]), "kind": kind_val, "mac": r["mac"]})
    return out


# ---- 未割当ノードの記録（GWのJOIN受信で呼ぶ） -------------------------------
def record_unassigned(
    *, mac: str, kind: int, fw_major: int = 0, fw_minor: int = 0,
    nvs_node_id: int = 0xFE,
) -> dict:
    """名乗り出たノードを未割当表へupsert。既にtiming_devicesに割当済みなら
    その情報を返して未割当には積まない。
    戻り値: {"status": "assigned"|"unassigned", "mac":..., "node_id"?:...}
    """
    norm = normalize_mac(mac)
    if norm is None:
        return {"status": "bad_mac", "mac": mac}

    # 既に割当済みか？
    for nid, m in current_bindings().items():
        if normalize_mac(m) == norm:
            return {"status": "assigned", "mac": norm, "node_id": nid}

    now = int(time.time())
    with _conn() as c:
        c.execute(
            """
            INSERT INTO timing_unassigned
                (mac, kind, fw_major, fw_minor, nvs_node_id, last_seen, seen_count)
            VALUES (?,?,?,?,?,?,1)
            ON CONFLICT(mac) DO UPDATE SET
                kind=excluded.kind,
                fw_major=excluded.fw_major,
                fw_minor=excluded.fw_minor,
                nvs_node_id=excluded.nvs_node_id,
                last_seen=excluded.last_seen,
                seen_count=timing_unassigned.seen_count+1
            """,
            (norm, int(kind), int(fw_major), int(fw_minor), int(nvs_node_id), now),
        )
        c.commit()
    return {"status": "unassigned", "mac": norm}


def list_unassigned(*, max_age_s: int | None = None) -> list[dict]:
    """未割当ノード一覧（UIの割当候補）。max_age_s を渡すと古いものを除外。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM timing_unassigned ORDER BY last_seen DESC"
        ).fetchall()
    now = int(time.time())
    out = []
    for r in rows:
        if max_age_s is not None and now - int(r["last_seen"]) > max_age_s:
            continue
        out.append(
            {
                "mac": r["mac"],
                "kind": int(r["kind"]),
                "kind_name": NodeKind(int(r["kind"])).name,
                "fw": f'{r["fw_major"]}.{r["fw_minor"]}',
                "nvs_node_id": int(r["nvs_node_id"]),
                "last_seen": int(r["last_seen"]),
                "seen_count": int(r["seen_count"]),
            }
        )
    return out


# ---- 割当の確定（admin操作） ------------------------------------------------
def bind(*, node_id: int, mac: str, kind: int) -> dict:
    """MAC を node_id に確定する。ドメインで検証してから timing_devices.mac へ書く。
    成功したら未割当表から当該MACを消す。
    戻り値: {"accepted": bool, "reason": str, "node_id":..., "mac":...}
    """
    result = validate_bind(
        kind=NodeKind(int(kind)),
        node_id=int(node_id),
        mac=mac,
        current_bindings=current_bindings(),
    )
    if not result.ok:
        return {
            "accepted": False,
            "reason": BindReason(result.reason).name,
            "node_id": int(node_id),
            "mac": normalize_mac(mac) or mac,
        }

    norm = normalize_mac(mac)
    with _conn() as c:
        c.execute(
            "UPDATE timing_devices SET mac=? WHERE node_id=?",
            (norm, int(node_id)),
        )
        c.execute("DELETE FROM timing_unassigned WHERE mac=?", (norm,))
        c.commit()
    return {
        "accepted": True,
        "reason": "OK",
        "node_id": int(node_id),
        "mac": norm,
    }


def unbind(*, node_id: int) -> dict:
    """割当を外す（付け替え前に使う）。timing_devices.mac を空にする。"""
    with _conn() as c:
        c.execute("UPDATE timing_devices SET mac='' WHERE node_id=?", (int(node_id),))
        c.commit()
    return {"node_id": int(node_id), "mac": ""}
