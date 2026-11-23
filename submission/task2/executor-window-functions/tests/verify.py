"""Verifier for the executor-window-functions task.

Runs as root with the reference interpreter (/opt/ref, which has DuckDB). It never imports
anything from /app: the code under test only runs in a child process as `nobody`, with no
access to this directory, the reference interpreter, or /logs. Reference results are
computed with DuckDB after the child has exited.
"""

from __future__ import annotations

import decimal
import json
import math
import os
import secrets
import selectors
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import duckdb  # noqa: E402

import querygen  # noqa: E402

LOG_DIR = "/logs/verifier"
CHILD_PYTHON = "/usr/local/bin/python3"
CHILD_TIMEOUT = 600
NOBODY = 65534


def log(msg: str) -> None:
    print(msg, flush=True)


def write_reward(value: float, details: dict) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, "reward.txt")
    tmp = os.path.join(LOG_DIR, ".reward.tmp")
    text = "1" if value >= 1 else ("0" if value <= 0 else f"{value:.4f}")
    with open(tmp, "w") as f:
        f.write(text + "\n")
    if os.path.lexists(path):
        os.remove(path)
    os.replace(tmp, path)
    with open(os.path.join(LOG_DIR, "details.json"), "w") as f:
        json.dump(details, f, indent=1)
    log(f"reward = {text}")


# ---------------------------------------------------------------------- integrity


def check_app() -> str | None:
    pkg = "/app/sqlglot"
    init = os.path.join(pkg, "__init__.py")
    if os.path.islink(pkg) or not os.path.isdir(pkg):
        return "/app/sqlglot must be a real directory"
    if not os.path.isfile(init):
        return "/app/sqlglot/__init__.py is missing"
    real = os.path.realpath(init)
    if not real.startswith("/app/sqlglot/"):
        return f"/app/sqlglot/__init__.py resolves outside /app: {real}"
    return None


def strip_setid_bits(root: str) -> None:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                continue
            if st.st_mode & (stat.S_ISUID | stat.S_ISGID):
                os.chmod(path, st.st_mode & ~(stat.S_ISUID | stat.S_ISGID))


# ---------------------------------------------------------------------- execution


def make_app_readable(root: str) -> None:
    """The code under test runs as `nobody`; make /app readable regardless of how it was saved."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in [None] + dirnames + filenames:
            path = dirpath if name is None else os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                continue
            extra = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
            if stat.S_ISDIR(st.st_mode) or st.st_mode & stat.S_IXUSR:
                extra |= stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            if st.st_mode & extra != extra:
                os.chmod(path, stat.S_IMODE(st.st_mode) | extra)


def native_code_files(root: str) -> list[str]:
    """Compiled native code under /app (e.g. a copied _sqlite3 extension) is not allowed."""
    found = []
    for dirpath, _, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                with open(path, "rb") as f:
                    if f.read(4) == b"\x7fELF":
                        found.append(path)
            except OSError:
                continue
    return found


IMPORT_SECONDS = 60
GRACE_SECONDS = 5


def run_child(datasets, cases) -> tuple[dict, str | None, str]:
    """Runs the cases in a sandboxed child. Time limits are enforced here, in the parent: if a
    case does not answer in time (or the child dies), that case fails and a fresh child
    continues with the remaining cases."""
    results: dict[int, dict] = {}
    stderr_tail = ""
    deadline = time.time() + CHILD_TIMEOUT
    pending = list(cases)
    while pending and time.time() < deadline:
        batch, stuck_id, err, bad_file = _run_batch(datasets, pending, deadline)
        stderr_tail = (stderr_tail + err)[-4000:]
        if bad_file:
            return {}, f"sqlglot was not imported from /app ({bad_file})", stderr_tail
        results.update(batch)
        if stuck_id is not None:
            results[stuck_id] = {"id": stuck_id, "error": "no answer within the case's time limit"}
        pending = [c for c in pending if c["id"] not in results]
    problem = None
    if pending:
        problem = f"total budget of {CHILD_TIMEOUT}s exhausted; {len(pending)} cases fail"
    return results, problem, stderr_tail


def _run_batch(datasets, cases, deadline):
    nonce = secrets.token_hex(16)
    workdir = tempfile.mkdtemp(prefix="wf-runner-")
    os.chmod(workdir, 0o755)
    runner = os.path.join(workdir, "runner.py")
    shutil.copyfile(os.path.join(os.path.dirname(__file__), "runner.py"), runner)
    os.chmod(runner, 0o644)

    request = json.dumps(
        {"nonce": nonce, "schema": querygen.SCHEMA, "datasets": datasets, "cases": cases}
    )
    argv = [
        "setpriv",
        f"--reuid={NOBODY}",
        f"--regid={NOBODY}",
        "--clear-groups",
        "--no-new-privs",
        "--inh-caps=-all",
        "--bounding-set=-all",
        CHILD_PYTHON,
        "-I",
        "-B",
        runner,
    ]
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8"}
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=workdir,
        env=env,
    )

    def feed():
        try:
            proc.stdin.write(request.encode())
            proc.stdin.close()
        except OSError:
            pass

    threading.Thread(target=feed, daemon=True).start()

    results: dict[int, dict] = {}
    prefix = nonce.encode() + b" "
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ, "out")
    sel.register(proc.stderr, selectors.EVENT_READ, "err")
    out_buf, err_buf = b"", b""
    index, imported, bad_file, stuck_id = 0, False, None, None
    case_deadline = time.time() + IMPORT_SECONDS
    open_streams = 2

    while open_streams and index < len(cases):
        wait = min(case_deadline, deadline) - time.time()
        if wait <= 0:
            stuck_id = cases[index]["id"]
            break
        for key, _ in sel.select(timeout=wait):
            chunk = os.read(key.fileobj.fileno(), 65536)
            if not chunk:
                sel.unregister(key.fileobj)
                open_streams -= 1
                continue
            if key.data == "err":
                err_buf = (err_buf + chunk)[-8000:]
                continue
            out_buf += chunk
            while b"\n" in out_buf and index < len(cases):
                line, out_buf = out_buf.split(b"\n", 1)
                if not line.startswith(prefix):
                    continue
                try:
                    record = json.loads(line[len(prefix):])
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                if "sqlglot_file" in record and not imported:
                    imported = True
                    f = record["sqlglot_file"]
                    if f is not None and not str(f).startswith("/app/"):
                        bad_file = str(f)
                elif record.get("id") == cases[index]["id"]:  # answers arrive in order
                    results[record["id"]] = record
                    index += 1
                else:
                    continue
                if index < len(cases):
                    case_deadline = time.time() + cases[index].get("timeout", 10) + GRACE_SECONDS
        if bad_file:
            break

    if stuck_id is None and index < len(cases) and not bad_file:
        stuck_id = cases[index]["id"]  # the child exited without answering this case
    proc.kill()
    proc.wait()
    sel.close()
    shutil.rmtree(workdir, ignore_errors=True)
    return results, stuck_id, err_buf.decode("utf-8", "replace"), bad_file


def reference_results(datasets, cases) -> dict[int, list]:
    connections = []
    for data in datasets:
        con = duckdb.connect(":memory:")
        for table, columns in querygen.SCHEMA.items():
            cols = ", ".join(f"{c} {ty}" for c, ty in columns.items())
            con.execute(f"CREATE TABLE {table} ({cols})")
            rows = [tuple(r[c] for c in columns) for r in data[table]]
            if rows:
                marks = ", ".join("?" for _ in columns)
                con.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)
        connections.append(con)

    expected = {}
    for case in cases:
        rows = connections[case["dataset"]].execute(case["sql"]).fetchall()
        expected[case["id"]] = [[normalize(v) for v in row] for row in rows]
    return expected


# ---------------------------------------------------------------------- comparison


def normalize(value):
    if isinstance(value, decimal.Decimal):
        return float(value)
    return value


def sort_key(row):
    return tuple(
        (0, "") if v is None else (1, round(float(v), 6)) if isinstance(v, (int, float)) and not isinstance(v, bool) else (2, repr(v))
        for v in row
    )


def values_equal(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    return type(a) is type(b) and a == b


def same_rows(got, want) -> bool:
    if not isinstance(got, list) or len(got) != len(want):
        return False
    if any(not isinstance(r, list) or len(r) != len(w) for r, w in zip(got, want)):
        return False
    try:
        got_sorted = sorted(got, key=sort_key)
    except (TypeError, ValueError):
        return False
    want_sorted = sorted(want, key=sort_key)
    return all(
        all(values_equal(a, b) for a, b in zip(r, w)) for r, w in zip(got_sorted, want_sorted)
    )


# ---------------------------------------------------------------------- main


def main() -> None:
    started = time.time()
    details: dict = {"tiers": {}, "failures": []}

    problem = check_app()
    if problem:
        details["error"] = problem
        log(problem)
        write_reward(0.0, details)
        return

    native = native_code_files("/app")
    if native:
        details["error"] = f"native code is not allowed under /app: {native[:5]}"
        log(details["error"])
        write_reward(0.0, details)
        return

    strip_setid_bits("/app")
    make_app_readable("/app")
    generated = querygen.generate()
    datasets, cases = generated["datasets"], generated["cases"]
    log(f"{len(cases)} cases over {len(datasets)} datasets")

    # regression cases first, so a slow solution cannot lose them to the total time budget
    ordered = [c for c in cases if c["tier"] == "regression"] + [c for c in cases if c["tier"] != "regression"]
    results, child_problem, child_stderr = run_child(datasets, ordered)
    if child_problem:
        details["child_problem"] = child_problem
        log(child_problem)
    details["child_stderr_tail"] = child_stderr[-2000:]

    expected = reference_results(datasets, cases)

    per_tier: dict[str, list[int]] = {}
    for case in cases:
        record = results.get(case["id"], {"error": "no result"})
        ok = "error" not in record and same_rows(record.get("rows"), expected[case["id"]])
        tally = per_tier.setdefault(case["tier"], [0, 0])
        tally[1] += 1
        if ok:
            tally[0] += 1
        elif len(details["failures"]) < 60:
            details["failures"].append(
                {
                    "tier": case["tier"],
                    "sql": case["sql"],
                    "dataset_rows": len(datasets[case["dataset"]]["t"]),
                    "got": record.get("error") or record.get("rows"),
                    "want": expected[case["id"]],
                }
            )

    passed_tiers = 0
    for tier in querygen.TIERS:
        good, total = per_tier.get(tier, [0, 0])
        tier_ok = total > 0 and good == total
        passed_tiers += tier_ok
        details["tiers"][tier] = {"passed": good, "total": total, "complete": tier_ok}
        log(f"tier {tier:12s} {good:4d}/{total:<4d} {'PASS' if tier_ok else 'fail'}")

    good, total = per_tier.get("regression", [0, 0])
    regression_ok = total > 0 and good == total
    details["regression"] = {"passed": good, "total": total, "complete": regression_ok}
    log(f"regression        {good:4d}/{total:<4d} {'PASS' if regression_ok else 'FAIL'}")

    reward = passed_tiers / len(querygen.TIERS) if regression_ok else 0.0
    if not regression_ok:
        log("queries without window functions regressed: reward is 0")
    details["seconds"] = round(time.time() - started, 1)
    write_reward(reward, details)

    if child_stderr.strip():
        log("--- stderr of the code under test (tail) ---\n" + child_stderr[-1500:])
    for failure in details["failures"][:8]:
        log(f"FAIL [{failure['tier']}] {failure['sql']}\n    got:  {str(failure['got'])[:300]}\n    want: {str(failure['want'])[:300]}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 - any verifier crash must still produce reward 0
        log(f"verifier error: {type(e).__name__}: {e}")
        try:
            write_reward(0.0, {"error": f"{type(e).__name__}: {e}"})
        except OSError:
            pass
