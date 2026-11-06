"""Check every benchmark bug manifests: oracle FAILS on buggy tree and PASSES on fixed tree."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks" / "bugsinpy"))
from oracle_checks import ORACLES  # noqa: E402

WORK = ROOT / "benchmarks" / "bugsinpy" / "work"
ok = 0
for bid, code in ORACLES.items():
    res = {}
    for v in ("buggy", "fixed"):
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(
            [str(WORK / bid / v), str(ROOT / "benchmarks" / "bugsinpy" / "compat_shims")]))
        r = subprocess.run([sys.executable, "-c", code], cwd=WORK / bid / v, env=env,
                           capture_output=True, text=True, timeout=180)
        last = (r.stderr.strip().splitlines() or [""])[-1][:100]
        res[v] = (r.returncode == 0, last)
    valid = (not res["buggy"][0]) and res["fixed"][0]
    ok += valid
    print(f"{'VALID ' if valid else 'BROKEN'} {bid:15} buggy={'pass' if res['buggy'][0] else 'FAIL'} "
          f"fixed={'pass' if res['fixed'][0] else 'FAIL'}  {res['buggy'][1] if not valid else ''} {res['fixed'][1] if not res['fixed'][0] else ''}",
          flush=True)
print(f"{ok}/{len(ORACLES)} bugs valid")
