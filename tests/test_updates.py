"""'Update ready' logic without network (python3 tests/test_updates.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from updates import UpdateCheck  # noqa: E402

u = UpdateCheck({}, {"commit": "abc1234"})
assert u.enabled and not u.status()["available"]
u.latest = {"commit": "abc1234", "title": "", "date": ""}
assert not u.status()["available"]                  # running the latest build
u.latest = {"commit": "def5678", "title": "Merge pull request #14", "date": ""}
assert u.status()["available"]
assert not UpdateCheck({}, {"commit": "local"}).enabled          # local runs never check
assert not UpdateCheck({"updates": {"check": False}}, {"commit": "abc1234"}).enabled
print("ok")
