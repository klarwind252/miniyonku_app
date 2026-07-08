# ミニ四駆レース管理システム ― GitHub更新マニュアル（保存版）

クラウド版（VPS）の更新を、GitHub 経由で行うための手順書です。
時間が経って忘れても、これ1枚で更新できるようにまとめてあります。

作成日：2026年7月

---

## 0. 全体像（何が変わったか）

これまでの「PCからSCPでファイルを丸ごと転送」を、**GitHub 経由の更新**に切り替えました。

```
[自分のPC] --push--> [GitHub] --pull--> [VPS(本番/テスト)] --> 再起動で反映
  編集             置き場・履歴           取り込み
```

メリット：変わったところだけ反映される／履歴が残り、おかしくなったら過去の状態に戻せる／どのPCからでも同じ手順で更新できる。

---

## 1. 環境一覧（自分の設定の控え）

| 区分 | 内容 |
|------|------|
| 編集する場所（PC） | `C:\Users\user\Desktop\miniyonku_app` |
| PCの管理ツール | GitHub Desktop |
| GitHub リポジトリ | `https://github.com/klarwind252/miniyonku_app` （公開） |
| **本番（さくら）** | `ubuntu@133.167.84.93` ／ アプリ：`/home/ubuntu/miniyonku_app` ／ SSH鍵：`id_ed25519` |
| **テスト（Vultr）** | `linuxuser@66.245.220.187` ／ アプリ：`/home/linuxuser/miniyonku_app` ／ SSH鍵：`id_ed25519_vps` |

- 公開URL（トークン等の秘密）は各サーバーの `/etc/miniyonku/miniyonku.env` にあり、GitHub には**含めません**。
- 更新用バッチ：`Vultr更新.bat`（テスト用）、`さくら本番更新.bat`（本番用・確認プロンプト付き）。

---

## 2. 普段の更新手順（これだけ覚えればOK）

おすすめの流れは「先にテスト（Vultr）で試す → 問題なければ本番（さくら）へ」です。

### 手順

1. **編集**：PCで `Desktop\miniyonku_app` の中身（主に `app/`）を修正する。
2. **GitHubへ上げる**：GitHub Desktop を開く → 左下に変更が出るので、Summary 欄に一言（例：`予選画面の修正`）→ **Commit to main** → 上の **Push origin**。
3. **テストへ反映**：`Vultr更新.bat` をダブルクリック → `--- 更新完了 ---` を確認 → ブラウザで `http://66.245.220.187/` を見て動作確認。
4. **本番へ反映**：`さくら本番更新.bat` をダブルクリック → 「本当に本番を更新しますか？」に **`yes`** と入力 → `--- 本番 更新完了 ---` を確認 → 本番URLで最終確認。

> 本番だけ更新したい場合は 4 だけ、テストだけなら 3 だけでもOKです。

---

## 3. バッチが使えないとき：手動更新

SSHで各サーバーに入って、3行を打つだけです。

**本番（さくら）：**
```
cd /home/ubuntu/miniyonku_app
git pull
sudo systemctl restart miniyonku
```

**テスト（Vultr）：**
```
cd /home/linuxuser/miniyonku_app
git pull
sudo systemctl restart miniyonku
```

パス（`/home/ubuntu/` と `/home/linuxuser/`）が違うだけで、あとは同じです。

---

## 4. 秘密ファイルの扱い（重要）

次の2つは、絶対に GitHub に上げてはいけません（公開リポジトリのため）。

- `deploy/miniyonku.env` … 本番トークン（ADMIN_TOKEN / VIEW_TOKEN）
- `setup/アクセス用.txt` … ホスト名・IP・実トークンの控え

これらは `.gitignore` で除外済みなので、通常は自動的に守られます。
**新しく秘密ファイルを追加するときは、必ず先に `.gitignore` に追記**してから commit してください。迷ったら、そのファイル名を私に相談してください。

---

## 5. バックアップと「元に戻す」（ロールバック）

### 今日取ったバックアップ（当面は消さない）

| 場所 | 内容 |
|------|------|
| さくら `/home/ubuntu/miniyonku_app_bkp_before_git` | GitHub化前のフォルダ全体 |
| さくら `/home/ubuntu/data_bkp_before_git_20260708` | 本番DB（control.db / miniyonku.db / stores）単体 |
| Vultr `/home/linuxuser/miniyonku_app_bkp_before_git` | GitHub化前のフォルダ全体 |

動作が安定して不要と判断できたら、後で削除して構いません。

### コードを前の版に戻したいとき（gitで）

サーバーに入り、履歴を確認して、戻したいコミットに合わせます（本番なら `ubuntu@...`）。
```
cd /home/ubuntu/miniyonku_app
git log --oneline          # 一覧から戻したい版のID（先頭7文字）を確認
git reset --hard <ID>      # 例: git reset --hard 1cede95
sudo systemctl restart miniyonku
```
`data`（DB）は `.gitignore` で除外されているため、この操作では変わりません。

### データ（DB）を戻したいとき

大会データが壊れた等でDBを戻す場合は、サーバー停止中にバックアップを上書きします。
```
sudo systemctl stop miniyonku
cp -a /home/ubuntu/data_bkp_before_git_20260708/. /home/ubuntu/miniyonku_app/data/
sudo systemctl start miniyonku
```

### 最終手段（フォルダ丸ごと戻す）

うまくいかないときは、フォルダ全体バックアップと入れ替えれば、GitHub化前の状態に完全に戻せます。手順に迷ったら、この段階で私に相談してください。

---

## 6. 困ったとき（トラブル対処）

| 症状 | 原因と対処 |
|------|------|
| `There is no tracking information for the current branch` | `git pull` の追跡設定が無い。設定済みだが再発したら、サーバーで `git branch --set-upstream-to=origin/main main` を一度実行 |
| `sudo: a password is required` | 再起動のsudo例外が無い。設定済み（`/etc/sudoers.d/miniyonku-restart`）。再発したら私に相談 |
| `git pull` で `Your local changes would be overwritten` | サーバー側でファイルが変更されている。**まず私に画面を見せる**のが安全。急ぎなら `git stash` → `git pull`（詳細は要相談） |
| バッチがすぐ閉じる／`Permission denied (publickey)` | SSH鍵が見つからない。バッチ内の鍵パス（本番＝`id_ed25519`、テスト＝`id_ed25519_vps`）と、`C:\Users\<ユーザー>\.ssh\` の鍵の有無を確認 |
| 画面が古いまま更新されない | ブラウザのキャッシュ。`Ctrl + F5` で強制リロード |
| 更新後に動きがおかしい | 第5章のロールバックで前の版へ。判断に迷えば相談 |

---

## 7. 別のPCから更新できるようにするには

新しいPCで更新するには、次の準備が必要です。

1. **GitHub Desktop** をインストールし、GitHub（`klarwind252`）にログイン → `miniyonku_app` を Clone。
2. **SSH鍵をコピー**：今のPCの `C:\Users\<ユーザー>\.ssh\` から `id_ed25519`（本番用）と `id_ed25519_vps`（テスト用）を、新PCの同じ場所（`.ssh` フォルダ）へコピー。
3. `Vultr更新.bat` と `さくら本番更新.bat` を新PCにも置く。

> SSH鍵は「新PCから各サーバーに入るための鍵」です。これが無いとバッチが動きません。鍵ファイルは秘密情報なので、USBメモリ等で安全に移し、他人に渡さないでください。

---

## 8. 付録：関連ファイル一覧

| ファイル | 役割 |
|------|------|
| `Vultr更新.bat` | テスト（Vultr）をワンクリック更新 |
| `さくら本番更新.bat` | 本番（さくら）をワンクリック更新（`yes` 確認付き） |
| `.gitignore` | 秘密ファイル・DB・venv 等を GitHub から除外 |
| `README.md` | GitHub リポジトリの表紙 |
| `LICENSE` | ライセンス（The Unlicense＝著作権フリー） |

---

*本手順は 2026年7月時点の構成に基づきます。環境（IP・パス・鍵名）を変更した場合は、第1章の表もあわせて書き換えてください。*
