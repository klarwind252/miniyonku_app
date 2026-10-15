# セキュリティ強化 その1：server_tokens off（nginx バージョン露出の抑止）

対象：本番2台とも（ConoHa / さくら）。nginx 設定の変更のみ。アプリ再起動は不要。

## 手順
1. 同梱の `m4laps_hardening.conf` を各サーバーの `/etc/nginx/conf.d/` に置く。
   ```
   sudo cp m4laps_hardening.conf /etc/nginx/conf.d/m4laps_hardening.conf
   ```
2. 設定の文法チェック（ここで必ず OK を確認）。
   ```
   sudo nginx -t
   ```
3. 反映（ダウンタイムなしの reload）。
   ```
   sudo systemctl reload nginx
   ```

## 確認（バージョンが消えたか）
```
curl -sI https://v133-117-77-69.sefs.static.cnode.jp/acecup/__nonexistent__ | grep -i '^Server:'
```
- 変更前：`Server: nginx/1.24.0 (Ubuntu)`
- 変更後：`Server: nginx`

404 本文の最下部に出ていた `nginx/1.24.0 (Ubuntu)` の表記も消えます
（本文からバージョンを消すのは `server_tokens off` の効果。文言「nginx」自体は残ります）。

## ロールバック
```
sudo rm /etc/nginx/conf.d/m4laps_hardening.conf
sudo nginx -t && sudo systemctl reload nginx
```

## 注意
- `nginx -t` が OK を返してから reload すること（NG のまま reload しても既存設定は保たれますが、
  必ず確認する習慣で）。
- このファイルは http コンテキスト前提。もし環境の nginx.conf が conf.d を include して
  いない特殊構成なら読み込まれません。その場合は `http { ... }` 直下に `server_tokens off;`
  を1行追加してください（要 `nginx -t` → reload）。
