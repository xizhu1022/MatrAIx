"""Runs every generated case with the sqlglot on sys.path; writes {id: rows | "ERR ..."} as JSON."""
import json, logging, signal, sys
logging.disable(logging.WARNING)
sys.path.insert(0, sys.argv[1]); import querygen
from sqlglot.executor import execute
def _to(*_): raise TimeoutError("time limit")
signal.signal(signal.SIGALRM, _to)
g = querygen.generate(); out = {}
for c in g["cases"]:
    schema = json.loads(json.dumps(querygen.SCHEMA)); tables = json.loads(json.dumps(g["datasets"][c["dataset"]]))
    signal.alarm(c.get("timeout", 10))
    try:
        rows = [list(r) for r in execute(c["sql"], schema=schema, tables=tables).rows]
        out[c["id"]] = [[v if v is None or type(v) in (bool, int, float, str) else repr(v) for v in r] for r in rows]
    except BaseException as e:
        out[c["id"]] = f"ERR {type(e).__name__}: {e}"[:200]
    finally:
        signal.alarm(0)
json.dump(out, open(sys.argv[2], "w"))
