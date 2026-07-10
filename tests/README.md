# テスト（characterization / 回帰網）

以降のリファクタ（トランザクション化・モノリス解体）の安全網として、まず
DB・FastAPI 非依存の純関数から回帰テストを整備した。ここでの期待値は「現状こう
動く」を記録した golden 値であり、挙動が変われば失敗する＝変更に気づける。

## 実行方法

    # 依存（アプリ本体 + pytest）
    pip install -r setup/requirements.txt -r setup/requirements-dev.txt

    # プロジェクトルート（このファイルの2つ上 = app/ の親）で
    pytest -q

    # ドメインだけ（FastAPI 未導入でも動く）
    pytest tests/test_domain.py -q

## 対象と方針

- `tests/test_domain.py` … kana / day_type / deadline / regulation / finalists。
  DB・Web 非依存。
- `tests/test_schedule.py` … 予選スケジュール生成。qualifying ルーターが FastAPI に
  依存するため、未導入環境では `importorskip` でファイルごとスキップされる。
  - 決定的関数（calc_points, generate_heat_schedule, generate_roundrobin_schedule）
    → golden 値で固定。
  - 乱数を使う関数（generate_point_schedule, generate_heat_roundrobin_schedule）
    → random の出力列は Python バージョンで変わり得るため golden 値では固定せず、
      「各ラウンドが全 entry を1回ずつ含む」等の意味的不変条件＋同一シード再現性で固定。

## 環境依存を避けている点

- `day_type_of` の祝日判定は任意依存 `jpholiday` の有無で変わるため、祝日でない
  平日・土・日だけをアサートしている（祝日日付は使わない）。
- `deadline_passed` は現在時刻に対する相対判定のため、明確な過去/未来のみ使用。

## 次に増やすと良い対象

- 順位計算（`_calc_standings` 系）… ただし DB 行を組む fixture が要るため、
  最小のインメモリ SQLite fixture を用意してから着手するのが安全。
- トランザクション化を進める各ハンドラの「途中失敗で半端な状態が残らない」テスト。
