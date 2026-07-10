# デプロイ・ハードニング手順

先に適用したアプリ側パッチを「実際に有効化」するための環境設定と、
アプリ内で完結できないオンプレのバインド対策をまとめる。

---

## 1. オンプレ版：無認証LAN公開の是正（最重要）

オンプレ版は既定で無認証。**単一PC運用なら 127.0.0.1 に固定**し、外部から届かない
状態を既定にする。参加者端末へ /view/ を見せるために LAN 公開する場合のみ、
明示的に 0.0.0.0 へ切り替え、**必ず PIN 認証を併用する**。

### 1-a. 起動バインド

uvicorn の起動コマンド（systemd / make_exe.py 生成ランチャ）で host を指定する。

    # 既定（安全）：ローカルのみ
    uvicorn app.main:app --host 127.0.0.1 --port 8000

    # LAN公開が必要なときだけ（PIN必須）
    ONPREM_ADMIN_PIN=xxxxxx uvicorn app.main:app --host 0.0.0.0 --port 8000

### 1-b. PIN 認証（本パッチで追加済み）

環境変数を設定したときだけ有効化される（未設定なら従来どおり素通し）。

    ONPREM_ADMIN_PIN   … /admin/* を保護（LAN公開時は必須）
    ONPREM_VIEW_PIN    … 任意。設定すると /view/* も保護（未設定なら観覧は公開のまま）
    COOKIE_SECURE=0    … HTTP(非TLS)のLANで検証する場合のみ。既定は Secure 有効

配布・利用フロー:

    管理者は初回だけ  http://<PC-IP>:8000/admin/?pin=<ADMIN_PIN>  を開く
      → PIN が HttpOnly Cookie に保存され、以降は ?pin なしでアクセス可能

参加者向け /view /entry /health /static /logo /race-asset は従来どおり公開のまま。

---

## 2. クラウド版：内部ヘッダ保護と HTTPS

### 2-a. 共有シークレット

    INTERNAL_RENDER_SECRET=<ランダム文字列>

を**アプリのプロセス環境に設定**する。これで内部レンダリングヘッダ
（x-internal-store-id）は x-internal-render-secret の一致時のみ信頼される。
未設定なら従来挙動（ヘッダをそのまま信頼）のままなので、本番では必ず設定する。

### 2-b. リバースプロキシ

`deploy/nginx.cloud.example.conf` を参照。要点は2つ:

  - x-internal-store-id / x-internal-render-secret を外部リクエストから除去する
  - X-Forwarded-Proto https を付与する（アプリの HTTPS 強制がこれを見る）

注意: プロキシが誤って X-Forwarded-Proto: http を常時送るとリダイレクトループに
なる。未設定時はアプリ側で https 扱い（安全側）にフォールバックする。

---

## 3. 反映後のスモークテスト（最低限）

  - アプリ起動 → GET /health が 200
  - オンプレ: PIN無しで /admin/ が 401 / ?pin=正 で 303→Cookie付与→操作可
  - オンプレ: /entry でフォーム送信→登録できる（公開のまま）
  - クラウド: ?key= 認証フロー（Cookie設定→鍵なしURLへ303）が従来どおり
  - クラウド: 参加者向けHTML書き出し（public_html 自己呼び出し）が成功する
    ※ INTERNAL_RENDER_SECRET 設定後は送受信で同一値になっているか確認
