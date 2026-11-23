"""Scores saved solutions under the current rule (all-or-nothing per group) and with an
in-group threshold (all cases -> 1, >= THRESH -> 0.5, else 0). DuckDB reference computed once."""
import json, os, pickle, subprocess, sys, concurrent.futures as cf
tests, out_dir = sys.argv[1], sys.argv[2]; impls = [a.split("=", 1) for a in sys.argv[3:]]
sys.path.insert(0, tests); import querygen, verify
g = querygen.generate(); cases = g["cases"]
ref_path = os.path.join(out_dir, "reference.pkl")
if os.path.exists(ref_path): want = pickle.load(open(ref_path, "rb"))
else: want = verify.reference_results(g["datasets"], cases); pickle.dump(want, open(ref_path, "wb"))
def run(item):
    name, path = item; res = os.path.join(out_dir, f"{name}.json")
    if not os.path.exists(res):
        subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "exec_cases.py"), tests, res],
                       env={**os.environ, "PYTHONPATH": path, "PYTHONDONTWRITEBYTECODE": "1"}, cwd=path,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3600)
    got = {int(k): v for k, v in json.load(open(res)).items()}
    tally = {}
    for c in cases:
        ok = isinstance(got.get(c["id"]), list) and verify.same_rows(got[c["id"]], want[c["id"]])
        t = tally.setdefault(c["tier"], [0, 0]); t[0] += ok; t[1] += 1
    return name, tally
with cf.ThreadPoolExecutor(4) as ex: results = dict(ex.map(run, impls))
json.dump(results, open(os.path.join(out_dir, "tallies.json"), "w"), indent=1)
print("done", len(results))
