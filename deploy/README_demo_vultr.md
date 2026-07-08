# デモ（お試し）予約 — Vultr 単体セットアップ手順

このサーバー（Vultr / Ubuntu 24.04）だけで完結する構成です。外部のメール配信
サービスや GCS は使いません（メールは同一サーバーの postfix から送信）。

## 0. 前提
- クラウド版として稼働済み（`DEPLOY_MODE=cloud`・nginx リバースプロキシ・
  systemd サービス `miniyonku` が起動している）。
- `PUBLIC_BASE_URL` が https の実URLに設定済み（案内メールのURL生成に使用）。

## 1. ファイル配置
本ZIPの `app/` 配下を、そのまま `/opt/miniyonku/miniyonku_app/app/` へ上書き
コピーします（ディレクトリ構成は維持）。新規: `app/demo.py` `app/emailer.py`
`app/routers/demo_reserve.py` `app/templates/demo_reserve/`。

## 2. 環境変数
`deploy/.env.demo.example` の内容を `/etc/miniyonku/miniyonku.env` へ追記し、
`SMTP_FROM` と各時間帯を自分の運用に合わせて編集します。

## 3. 送信専用メール（postfix）を Vultr に用意
```bash
sudo DEBIAN_FRONTEND=noninteractive apt install -y postfix
# セットアップで「Internet Site」を選択。system mail name はサーバーのFQDN。
sudo systemctl enable --now postfix
# 動作確認
echo "test body" | mail -s "test" you@example.com    # mailutils 未導入なら: sudo apt install -y mailutils
```
到達率のために **Vultr のコントロールパネルで rDNS(PTR) を FQDN に設定**し、
可能なら送信ドメインに SPF レコード（例 `v=spf1 a mx ~all`）を追加してください。
※ Vultr は新規アカウントで SMTP(25番) 送信が制限される場合があります。送信でき
ない場合はサポートで解除申請するか、`.env` を外部SMTPリレー設定に切り替えます
（ファイル自体の変更は不要・env のみ）。

## 4. 反映
```bash
sudo systemctl restart miniyonku
journalctl -u miniyonku -n 30 --no-pager   # 「デモ予約 有効 / デモ店舗=[...]」を確認
```
起動時に `demo1〜demo4` 店舗が自動作成されます（`data/control.db` と
`data/stores/demoN/` が生成）。デモ店舗は**予約がある時間帯だけ開く**ため、
予約前にURLへアクセスすると「ただいまご利用いただけません」（503）になります。

## 5. 運用
- 予約フォーム（参加者へ案内するURL）: `https://<PUBLIC_BASE_URL>/reserve`
- 参加者はカレンダーで日時を選び、メールアドレスのみ入力して送信。
- 送信すると demo1〜4 の空き1店舗を自動確保 →**その店舗の鍵を再生成**→ 案内
  メール（admin/view URL＋テンプレート文）を自動送信。
- 予約時間内のみアクセス可能。終了時刻を過ぎると自動クローズし、店舗は次の
  予約に再利用されます。

## 6. nginx について
`/reserve` も既存のリバースプロキシ（uvicorn へのプロキシ）でそのまま通ります。
追加設定は不要です。デモ店舗のスラッグURL（`/demo1/...` 等）も既存の
`/{slug}` プロキシ設定で配信されます。

## 7. 任意：各回クリーンなDBで配りたい場合
サンプルDBを用意し、`DEMO_SEED_DB=/opt/miniyonku/seed/demo_seed.db` を設定すると、
予約確定のたびにデモ店舗DBがその内容へ初期化されます（前回利用者のデータが残り
ません）。未設定なら初期化しません。
