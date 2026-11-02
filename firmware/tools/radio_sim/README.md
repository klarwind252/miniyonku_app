# 無線層シミュレーション（実機不要）
実ソース（gate/src/main.cpp・remote/src/main.cpp）から再送/ACK処理を抜き出し、パケットロスとACK遅延を乱数で与えて
偽LOST率・重複記録数・再送回数を数える。ロジック変更の前後比較に使う。

    python3 gen_and_run.py      # g++ が必要

結果（2026-10-31・A1修正後）：ACK遅延70ms/130msで偽LOST 100%→0%、重複3→1（GW側間引きで0）。
