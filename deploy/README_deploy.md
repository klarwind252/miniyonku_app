# クラウド版デプロイ手順（さくらのVPS / Ubuntu 24.04）

ミニ四駆レース管理システムを、さくらのVPS上に常設のクラウド版として構築する手順です。
オンプレ版（Windows PC）と**同一のコードベース**を、環境変数 `DEPLOY_MODE=cloud` で
切り替えて動作させます。データベースは引き続き **SQLite** を使用します。

構成: nginx（SSL終端・参加者向けHTML直接配信）→ uvicorn（FastAPIアプリ）→ SQLite

---

## 0. 前提

- さくらのVPS（Ubuntu 24.04 LTS / メモリ2GB推奨）
- 独自またはVPS標準のホスト名（https化のため）
- sudo 権限のあるユーザー

仮想3コア・メモリ2GBで、admin/view 数台 + 参加者向けHTML（静的配信）200名規模を想定。
参加者向けHTMLは nginx が静的配信するため、観覧人数が増えてもアプリ負荷は増えません。

---

## 1. パッケージ準備

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv nginx
```

## 2. アプリ配置

```bash
sudo mkdir -p /opt/miniyonku
sudo chown $USER:$USER /opt/miniyonku
# このリポジトリ（miniyonku_app）を /opt/miniyonku/miniyonku_app へ配置
cd /opt/miniyonku/miniyonku_app
python3.12 -m venv venv
./venv/bin/pip install -r setup/requirements.txt
```

## 3. 実行ユーザーと配信ディレクトリ

```bash
sudo useradd -r -s /usr/sbin/nologin miniyonku || true
sudo chown -R miniyonku:miniyonku /opt/miniyonku/miniyonku_app
# 参加者向けHTMLの書き出し先（PUBLIC_HTML_DIR）
sudo mkdir -p /var/www/miniyonku_public
sudo chown miniyonku:miniyonku /var/www/miniyonku_public
```

## 4. 環境変数

```bash
sudo mkdir -p /etc/miniyonku
sudo cp deploy/.env.example /etc/miniyonku/miniyonku.env
sudo nano /etc/miniyonku/miniyonku.env
```

`ADMIN_TOKEN` と `VIEW_TOKEN` は必ず推測困難な値へ変更します（別々の値にすること）:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # admin 用
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # view 用
```

`PUBLIC_BASE_URL` は実際のホスト名（https）に、`PUBLIC_HTML_DIR` は手順3で作成した
ディレクトリに合わせます。

## 5. systemd サービス

```bash
sudo cp deploy/miniyonku.service /etc/systemd/system/miniyonku.service
sudo systemctl daemon-reload
sudo systemctl enable --now miniyonku
journalctl -u miniyonku -f      # 「クラウド固定トークン認証 有効」と出ればOK
```

## 6. nginx

```bash
sudo cp deploy/nginx_miniyonku.conf /etc/nginx/sites-available/miniyonku
sudo nano /etc/nginx/sites-available/miniyonku     # server_name / root を実値に
sudo ln -s /etc/nginx/sites-available/miniyonku /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

## 7. SSL（Let's Encrypt）

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d example.vs.sakura.ne.jp    # ← 実ホスト名
```

---

## アクセス方法（クラウド版）

固定トークンは初回だけ URL に付け、以降は Cookie で自動認証されます。

| 画面 | URL | 認証 |
| --- | --- | --- |
| admin（管理者） | `https://<host>/admin/?key=<ADMIN_TOKEN>` | admin トークン |
| view（観覧・常設端末） | `https://<host>/view/?key=<VIEW_TOKEN>` | view トークン |
| html（参加者観覧） | `https://<host>/` | 認証なし |

- 初回アクセス時に `?key=...` を付けると、トークンが HttpOnly Cookie に保存され、
  URLから鍵を除いたアドレスへリダイレクトされます（URLバーに鍵が残りません）。
- view 用端末（キオスク等）は、初回に view URL で開けば以後は鍵入力不要です。
- admin トークンは書き換え権限を持つため、view とは別管理にし厳重に扱ってください。

---

## オンプレ版との関係

- 同一コードベースです。オンプレ版は環境変数を設定しない（`DEPLOY_MODE` 未設定 →
  `onprem` 既定）ため、認証ミドルウェアは無効、html配信は従来どおり（ローカル／GCSオプション）。
- クラウド版（`DEPLOY_MODE=cloud`）でのみ、固定トークン認証と
  「参加者向けHTMLをローカルへ書き出し→nginx直接配信」が有効になります。
- Windows固有処理（EXEランチャー・ブラウザ連動終了・システムトレイ）はオンプレ専用で、
  クラウドのアプリ稼働には関与しません。

## バックアップ

SQLite ファイル（`data/miniyonku.db`）をコピーするだけです。

```bash
sudo cp /opt/miniyonku/miniyonku_app/data/miniyonku.db ~/miniyonku_$(date +%Y%m%d).db
```
