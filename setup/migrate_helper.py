import sqlite3, os, sys

base = os.path.dirname(os.path.abspath(__file__))
db   = os.path.join(base, 'data', 'miniyonku.db')

if not os.path.exists(db):
    print('[ERROR] DB not found:', db)
    sys.exit(1)

print('DB:', db)
c = sqlite3.connect(db)

for col, typ in [
    ('point_1st',        'INTEGER DEFAULT 3'),
    ('point_2nd',        'INTEGER DEFAULT 2'),
    ('point_3rd',        'INTEGER DEFAULT 1'),
    ('point_co',         'INTEGER DEFAULT 0'),
    ('qual_round_count', 'INTEGER DEFAULT 1'),
]:
    try:
        c.execute(f'ALTER TABLE tournaments ADD COLUMN {col} {typ}')
        print('  added:', col)
    except:
        print('  skip :', col)

try:
    c.execute('ALTER TABLE heat_results ADD COLUMN is_co INTEGER DEFAULT 0')
    print('  added: heat_results.is_co')
except:
    print('  skip : heat_results.is_co')

c.commit(); c.close()
print('Migration done.')
