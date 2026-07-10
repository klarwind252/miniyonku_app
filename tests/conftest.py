"""pytest 共通設定。

- プロジェクトルート（app/ の親）を sys.path へ追加し、どこから実行しても
  `import app...` が解決できるようにする。
- 乱数を使う関数のテストを安定させるため、各テスト前に固定シードを張る。
"""
import os
import sys
import random

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@pytest.fixture(autouse=True)
def _seed_random():
    random.seed(1234)
    yield
