# セキュリティ強化 その2：POSTボディ上限＋低速接続タイムアウト

対象：本番2台（ConoHa / さくら）。nginx のみ。アプリ再起動不要。

## 手順
```
sudo cp m4laps_limits.conf /etc/nginx/conf.d/m4laps_limits.conf
sudo nginx -t
sudo systemctl reload nginx
```

## 確認
- 通常のレース作成・画像アップロードが今まで通りできること（数MBまでOK）。
- 観覧ページ・事前告知ページが今まで通り表示・更新されること（SSE/ポーリング含む）。
- 極端に大きいPOST（例：30MB超）は 413 (Request Entity Too Large) になる。

## もしアップロードが 413 になったら
`client_max_body_size 25m;` の値を上げる（例 `50m`）→ `nginx -t` → `reload`。
（レース画像は長辺667pxリサイズのため通常は数MB以内。超える運用なら調整可）

## ロールバック
```
sudo rm /etc/nginx/conf.d/m4laps_limits.conf
sudo nginx -t && sudo systemctl reload nginx
```

## 備考
- 既存の `sites-available/miniyonku` に `client_max_body_size` 等が個別に書かれている場合、
  その location/server の値が優先されます（このドロップインは既定値の底上げのみで、既存を壊しません）。
- 「その1」(server_tokens) と併用可能。両方 `/etc/nginx/conf.d/` に置けます。
