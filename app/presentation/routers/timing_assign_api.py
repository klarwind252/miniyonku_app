"""M4LAPS ノード割当API（既存 timing_api とは別ルーターで追加）

役割は2系統。
  (A) GW向け（デバイストークン認証）:
      POST /api/timing/join        … GWが拾った未割当ノードを報告
      GET  /api/timing/assignments … MAC->node_id 表を取得（GWがキャッシュしJOIN_ACKに使う）
  (B) admin向け（既存のadmin認証で保護する）:
      GET  /admin/timing/unassigned      … 未割当ノード一覧（割当UIが叩く）
      POST /admin/timing/devices/bind    … MAC を node_id に確定
      POST /admin/timing/devices/unbind  … 割当を外す

⚠ 認証は既存実装に合わせること。ここでは:
   - GW系は X-Timing-Token（既存 timing_api と同じ環境変数 TIMING_TOKEN）を流用
   - admin系は既存の admin 依存（get_current_admin 等）に置き換える TODO を明示
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.infrastructure.db.repositories import timing_assignment_repository as repo

router = APIRouter(prefix="/api/timing", tags=["m4laps-assign"])
admin_router = APIRouter(prefix="/admin/timing", tags=["m4laps-assign-admin"])

_TIMING_TOKEN = os.environ.get("TIMING_TOKEN", "")


def _auth_gw(token: str | None) -> None:
    # 既存 timing_api と同じ方針：TOKEN未設定ならローカル運用として素通し。
    if _TIMING_TOKEN and token != _TIMING_TOKEN:
        raise HTTPException(status_code=401, detail="invalid timing token")


# ============================================================================
#  (A) GW向け
# ============================================================================
class JoinIn(BaseModel):
    mac: str = Field(..., description="ノードのSTA MAC")
    kind: int = Field(..., ge=0, le=3, description="NodeKind (0=GW,1=SQ,2=RC,3=SG)")
    fw_major: int = 0
    fw_minor: int = 0
    nvs_node_id: int = 0xFE


@router.post("/join")
def post_join(body: JoinIn, x_timing_token: str | None = Header(None)) -> dict:
    """GWが拾った未割当ノードを報告する。
    既に割当済みなら {"status":"assigned","node_id":N} を返し、GWはそのまま
    JOIN_ACK(node_id) を返せる。未割当なら台帳に積み、adminの割当を待つ。
    """
    _auth_gw(x_timing_token)
    return repo.record_unassigned(
        mac=body.mac,
        kind=body.kind,
        fw_major=body.fw_major,
        fw_minor=body.fw_minor,
        nvs_node_id=body.nvs_node_id,
    )


@router.get("/assignments")
def get_assignments(x_timing_token: str | None = Header(None)) -> dict:
    """MAC->node_id の一覧。GWはオンライン時にこれを取得してキャッシュし、
    オフラインでも JOIN_ACK を返せるようにする。
    """
    _auth_gw(x_timing_token)
    return {"assignments": repo.assignments_map()}


# ============================================================================
#  (B) admin向け（⚠ 既存のadmin認証に載せ替えること）
# ============================================================================
class BindIn(BaseModel):
    node_id: int = Field(..., ge=0, le=11)
    mac: str
    kind: int = Field(..., ge=0, le=3)


class UnbindIn(BaseModel):
    node_id: int = Field(..., ge=0, le=11)


@admin_router.get("/unassigned")
def get_unassigned(max_age_s: int | None = None) -> dict:
    """未割当ノード一覧（割当UIが表示する）。max_age_s で古いものを隠せる。"""
    return {"unassigned": repo.list_unassigned(max_age_s=max_age_s)}


@admin_router.post("/devices/bind")
def post_bind(body: BindIn) -> dict:
    """MAC を node_id に確定する。ドメインで検証し、拒否理由を返す。"""
    result = repo.bind(node_id=body.node_id, mac=body.mac, kind=body.kind)
    if not result["accepted"]:
        # 400で理由を返す（UIがトーストに出す）
        raise HTTPException(status_code=400, detail=result)
    return result


@admin_router.post("/devices/unbind")
def post_unbind(body: UnbindIn) -> dict:
    """割当を外す（付け替え前）。"""
    return repo.unbind(node_id=body.node_id)
