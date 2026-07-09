# tools/route_dump.py — ルート集合が変わっていないことの機械検証用
from app.main import app

for r in sorted(app.routes, key=lambda r: getattr(r, "path", "")):
    if hasattr(r, "methods"):
        print(sorted(r.methods), r.path)