# 変更まとめ（統合版）

このツリーは、コードレビューで合意した「安全に適用できる修正」を一括で反映した
統合成果物です。個別差分ZIPを順に当てる運用ミスを避けるため、1つにまとめています。
全 Python ファイルの `py_compile` と `tests/`（27件）は成功済みですが、**実行時の
統合テストは未実施**のため、ステージング確認のうえ本番へ適用してください。

関連ドキュメント:
  - SECURITY_FIXES.md … セキュリティ修正①〜⑦の詳細と未適用項目
  - deploy/HARDENING.md … デプロイ設定（オンプレ・バインド／クラウド・nginx）
  - tests/README.md … テストの実行方法・方針・環境依存の回避点

## 反映済みの変更

### セキュリティ
- 認証Cookieに `Secure` + `Referrer-Policy`（`app/presentation/auth.py`）
- セキュリティヘッダ + クラウド時HTTPS強制（`app/presentation/middleware/security.py`）
- 公開 `/entry` の入力上限 + トークンTTL（`app/application/pre_entry_service.py`）
- レース画像配信のContent-Type制限（`app/presentation/routers/public_misc.py`）
- 内部ヘッダ `x-internal-store-id` の共有シークレット保護
  （`store_resolver.py` / `services/public_html.py`）
- httpx を `ASGITransport` へ移行（`services/public_html.py`）
- オンプレ版の任意PIN認証（`app/presentation/onprem_auth.py`、`main.py` で登録）

### 整合性
- トランザクション原子化ヘルパ（`app/infrastructure/db/tx.py`）
- 主要な書き込み経路を `transaction()` で原子化：
  - `qualifying.py`：組確定 `qualifying_generate_heat` / `qualifying_generate_round` /
    `qualifying_generate`、結果保存 `heat_result_save`、結果リセット `heat_result_reset`、
    ヒート決勝生成 `heat_final_generate`、ヒート決勝トーナメント削除
    `heat_final_tournament_reset`。
  - `bracket.py`：決勝順位クリア `bracket_clear_final_result`、くじ確定
    `bracket_lottery_confirm`。
  ※ 見送り（多段コミット／内部コミット関数を含み、単純な囲みでは原子性が成立しない）：
    `heat_final_save`・`heat_final_rank`・`heat_final_tournament_generate`、
    `bracket_generate`・`bracket_save`・`next_round_generate`。
  ※ 未着手：その他リセット・レーン編集、`tournaments.py`。

### テスト（回帰網）
- ドメイン純関数の characterization（`tests/test_domain.py`）
- 予選スケジュール生成の golden/不変条件（`tests/test_schedule.py`）
- `transaction()` の commit/rollback 機能（`tests/test_tx.py`）
- 順位計算 `_calc_standings_rr` の characterization（`tests/test_standings.py`）
- 原子化した各ハンドラの機能・原子性（実DB）：組確定（`tests/test_generate_tx.py`）、
  結果保存（`tests/test_result_save_tx.py`）、リセット（`tests/test_reset_tx.py`）、bracket確定（`tests/test_bracket_tx.py`）
- 共通設定・実行手順（`tests/conftest.py` / `tests/README.md`）
- 開発用依存（`setup/requirements-dev.txt`：pytest）
- 合計 39 件、全パス。

### デプロイ資料
- `deploy/HARDENING.md`、`deploy/nginx.cloud.example.conf`

### その他
- 文字化けしていた同梱ファイル名（`#Uxxxx` 形式のPDF・.bat）を正しい日本語へ復元。

## 追加環境変数（すべて任意・未設定なら従来挙動）

- `COOKIE_SECURE`（既定 1）: 0 で Secure Cookie 無効（HTTP検証用）
- `INTERNAL_RENDER_SECRET`（既定 空）: 設定時のみ内部ヘッダ照合を強制
- `ONPREM_ADMIN_PIN` / `ONPREM_VIEW_PIN`: オンプレLAN公開時の簡易PIN保護

## 未適用（別途対応。詳細は SECURITY_FIXES.md）

- オンプレの `127.0.0.1` 既定バインド（起動コマンド側の対応。PIN機能は本ツリーに同梱済み）
- 残る「削除→再生成」ハンドラ／`bracket.py`・`tournaments.py` のトランザクション横展開
- モノリス解体（`qualifying.py`／`bracket.py`）と `app/routers/*` シム削除
- `StoreResolverMiddleware` の本文正規表現置換の撤去
- `logging` 導入による `print` 置換
- 未固定依存（jpholiday/tzdata/colorama）への上限ピン
- 順位計算 `_calc_standings` 系のテスト拡充
