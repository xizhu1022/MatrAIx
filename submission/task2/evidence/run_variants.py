"""Runs each variant's solve.sh through the exact selfcheck trial lifecycle; prints rewards."""
import os, sys, json, tomllib, concurrent.futures as cf
from pathlib import Path
sys.path.insert(0, os.environ.get("SELFCHECK_TOOLS", "tools"))  # directory containing selfcheck.py
import selfcheck
task = Path(sys.argv[1]); variants = [Path(p) for p in sys.argv[2:]]
meta = tomllib.loads((task / "task.toml").read_text())
env_img, ver_img = "wfvar-env", "wfvar-verifier"
for ctx, img in ((task / "environment", env_img), (task / "tests", ver_img)):
    err = selfcheck.build(ctx, img, 900)
    assert err is None, err
def run(v):
    t = selfcheck.trial(v, env_img, ver_img, meta, "oracle")
    return v.name, t
results = {}
with cf.ThreadPoolExecutor(3) as ex:
    for name, t in ex.map(run, variants):
        results[name] = t
        print(f"{name:22s} reward={t['reward']}  {t['error'] or ''}", flush=True)
out = Path(sys.argv[1]).parent / "variant_results.json"
Path(os.environ.get("OUT_DIR", ".")) / "variant_results.json".write_text(json.dumps(results, indent=1))
