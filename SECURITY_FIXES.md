# 適用済み修正メモ（本番対応パッチ）

コードレビューで指摘した「自己完結して安全に適用できる修正」のみを適用しています。
ディレクトリ構成・既存挙動は維持し、後方互換を保っています。全66ファイルの
`py_compile` は成功済みですが、**実行時の統合テストは未実施**のため、ステージングでの
動作確認後に本番適用してください。

## 適用済み（①〜⑦）

1. 認証Cookieに `Secure` 属性と `Referrer-Policy: no-referrer` を付与
   - `app/presentation/auth.py`
   - 既定で Secure 有効。HTTPで検証したい場合のみ環境変数 `COOKIE_SECURE=0`。

2. セキュリティヘッダ + クラウド時HTTPS強制ミドルウェアを追加・登録
   - 追加: `app/presentation/middleware/security.py`
   - 登録: `app/main.py`（最外周）
   - X-Frame-Options / X-Content-Type-Options / Referrer-Policy を全応答へ。
     クラウド時は HSTS 付与 + `X-Forwarded-Proto=http` を 308 で HTTPS 格上げ。

3. トランザクション原子化ヘルパを追加（＋適用指針）
   - 追加: `app/infrastructure/db/tx.py`（`async with transaction(db): ...`）
   - **注意**: 全 `commit` 箇所の一括置換は回帰リスクが高いため未実施。整合性が
     崩れやすい「組確定・ラウンド操作・結果一括更新」ハンドラから段階適用してください。
     ブロックへ移す際は内部の個別 `await db.commit()` を削除すること。

4. 公開エントリー `/entry` の入力境界 + トークンTTLクリーンアップ
   - `app/application/pre_entry_service.py`
   - 1送信あたり最大 `MAX_PARTY=12` 人、各フィールド長 `MAX_LEN=64` /
     連絡先 `MAX_CONTACT_LEN=254`。値は運用に合わせて調整可。
   - トークン発行時に1日より古い `entry_form_tokens` を削除。

5. レース画像配信の Content-Type を画像allowlistに制限（保存型XSS対策）
   - `app/presentation/routers/public_misc.py`
   - `image/png|jpeg|webp|gif` 以外は 404。`X-Content-Type-Options: nosniff` 付与。

6. 内部レンダリングヘッダ `x-internal-store-id` を共有シークレットで保護
   - 受信: `app/presentation/middleware/store_resolver.py`
   - 送信: `app/services/public_html.py`（2箇所）
   - 環境変数 `INTERNAL_RENDER_SECRET` を**両側で設定したときのみ**照合を強制。
     未設定なら従来挙動を維持（後方互換）。本番では設定し、かつリバースプロキシで
     `x-internal-store-id` / `x-internal-render-secret` を外部リクエストから除去すること。

7. httpx を `ASGITransport` へ移行（0.28以降での破壊回避）
   - `app/services/public_html.py`（2箇所）
   - 現行ピン `httpx==0.27.0` でも動作します。

## 未適用（要・別途対応。回帰リスク or 大規模改修のため）

- オンプレの `127.0.0.1` 既定バインド + LAN公開オプトイン
  → 起動は systemd / `make_exe.py` 生成のランチャ側で行われるため、アプリ内で
    完結できません。起動コマンドの `--host` を既定 `127.0.0.1` にし、LAN公開時のみ
    明示的に `0.0.0.0` へ切り替え、その際は簡易PIN/Basic認証を必須にしてください。

- 全書き込みハンドラのトランザクション化（③の全面適用）と `bracket_repair.py` の格下げ

- モノリス解体（`qualifying.py` / `bracket.py`）と `app/routers/*` シム削除

- `StoreResolverMiddleware` の本文正規表現置換の撤去（`root_path` ベースのURL生成へ）

- `logging` 導入による `print` / `traceback.print_exc` 置換

- 未固定依存（`jpholiday` / `tzdata` / `colorama`）への上限ピン付与
  → `setup/requirements.txt`。他は既に `==` で厳密固定済み。

- スケジュール生成・順位計算の回帰テスト整備

## 追加した環境変数（任意）

- `COOKIE_SECURE`（既定 `1`）: `0` で Secure Cookie を無効化（HTTP検証用）。
- `INTERNAL_RENDER_SECRET`（既定 空）: 設定時のみ内部ヘッダ照合を強制。
