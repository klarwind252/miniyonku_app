"""
複数店舗化（クラウド版のみ）で使う「現在の店舗」コンテキスト。

- Store: 1店舗ぶんの解決済み情報（スラッグ・DBパス・配信先・トークン等）。
- current_store: リクエスト処理中に「今どの店舗か」を保持する ContextVar。
    StoreResolverMiddleware が各リクエストの先頭でセットし、finally で必ずリセットする。
    これにより、引数を取り回さなくても export_current_html() 等が
    現在の店舗を参照できる（既存の呼び出し箇所を変更せずに済む）。

オンプレ版（IS_CLOUD=False）ではミドルウェアが登録されないため、
current_store は常に None のまま＝従来挙動。
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Optional


@dataclass
class Store:
    id: int
    slug: str           # 既定店舗（店舗1）は "" 。店舗2〜は "store2" 等
    name: str
    db_path: str        # その店舗のSQLiteファイル絶対パス
    public_dir: str     # 参加者向けHTMLの書き出し先（nginx 直接配信）
    admin_token: str
    view_token: str
    enabled: bool = True
    restrict_hours: bool = False        # 利用時間制限ON/OFF（店舗2〜5のみ有効）
    access_start: Optional[str] = None  # "HH:MM"
    access_end: Optional[str] = None    # "HH:MM"

    @property
    def prefix(self) -> str:
        """URL接頭辞。既定店舗は "" 、それ以外は "/store2" 等。"""
        return f"/{self.slug}" if self.slug else ""


# 現在のリクエストが属する店舗。未設定（オンプレ／解決前）は None。
current_store: contextvars.ContextVar[Optional[Store]] = contextvars.ContextVar(
    "current_store", default=None
)


def get_current_store() -> Optional[Store]:
    return current_store.get()
