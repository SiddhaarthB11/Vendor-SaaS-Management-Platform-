from pathlib import Path
import subprocess
from devtools.git_helpers import ensure_baseline_commit, has_commits

root = Path("/app")
ok, err = ensure_baseline_commit(root)
print("baseline ok:", ok, "err:", err, "has_commits:", has_commits(root))
r = subprocess.run(["git", "log", "-1", "--oneline"], cwd=root, capture_output=True, text=True)
print("log:", r.stdout.strip())
r2 = subprocess.run(["git", "status", "--short"], cwd=root, capture_output=True, text=True)
lines = (r2.stdout or "").splitlines()
print("status lines:", len(lines))
print("sample:", lines[:5])
