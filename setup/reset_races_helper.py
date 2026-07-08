import sqlite3, os, sys

# setup/ フォルダにあるので、DBは一つ上のフォルダのdata/に存在する
base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
db_path = os.path.join(base, 'data', 'miniyonku.db')

if not os.path.exists(db_path):
    print('DBが見つかりません:', db_path)
    sys.exit(1)

c = sqlite3.connect(db_path)
tables = [
    'bracket_slot_ranks', 'bracket_results', 'bracket_slots',
    'bracket_groups', 'bracket_rounds',
    'ht_slot_ranks', 'ht_results', 'ht_slots', 'ht_groups', 'ht_rounds',
    'heat_results', 'heat_lanes', 'heats',
    'heat_finals',
    'entries',
    'tournaments',
]
for tbl in tables:
    try:
        c.execute(f'DELETE FROM {tbl}')
        print('  削除:', tbl)
    except Exception as e:
        print('  スキップ:', tbl)
c.commit()
c.close()
print()
print('完了：レーサーマスタを保持したままリセットしました')
