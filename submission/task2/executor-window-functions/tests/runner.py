"""Runs the cases against the sqlglot in /app. Executed as an unprivileged user.

Protocol: reads one JSON document from stdin ({"nonce", "datasets", "cases"}), writes one
line per case to the original stdout: "<nonce> <json>". Everything else the code under
test prints goes to stderr, so it cannot corrupt the protocol by accident.
"""

import json
import os
import signal
import sys

DEFAULT_CASE_SECONDS = 10


def main() -> None:
    request = json.load(sys.stdin)
    nonce = request["nonce"]

    protocol = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)  # stray prints from the code under test go to stderr

    import logging

    logging.disable(logging.WARNING)

    # Appended, like an editable install: the standard library wins over stray files in /app
    # (e.g. a scratch `random.py`), and sqlglot can only come from /app.
    sys.path.append("/app")

    try:
        import sqlglot
        from sqlglot.executor import execute
    except BaseException as e:  # noqa: BLE001 - report import failures as case failures
        for case in request["cases"]:
            protocol.write(f"{nonce} " + json.dumps({"id": case["id"], "error": f"import: {e!r}"}) + "\n")
        return

    protocol.write(f"{nonce} " + json.dumps({"sqlglot_file": getattr(sqlglot, "__file__", None)}) + "\n")

    def timeout(*_):
        raise TimeoutError("case exceeded its time limit")

    signal.signal(signal.SIGALRM, timeout)
    schema = request["schema"]

    for case in request["cases"]:
        dataset = request["datasets"][case["dataset"]]
        record = {"id": case["id"]}
        try:
            # fresh copies, made before the clock starts, so one case cannot affect another
            schema_copy = json.loads(json.dumps(schema))
            tables_copy = json.loads(json.dumps(dataset))
            signal.alarm(int(case.get("timeout", DEFAULT_CASE_SECONDS)))
            try:
                result = execute(case["sql"], schema=schema_copy, tables=tables_copy)
                rows = [list(row) for row in result.rows]
            finally:
                signal.alarm(0)
            record["rows"] = encode(rows)
        except BaseException as e:  # noqa: BLE001
            record = {"id": case["id"], "error": f"{type(e).__name__}: {str(e)[:300]}"}
        protocol.write(f"{nonce} " + json.dumps(record) + "\n")


def encode(rows):
    """Only plain values survive; anything else is reported as a type error."""
    out = []
    for row in rows:
        encoded = []
        for value in row:
            if value is None or type(value) in (bool, int, float, str):
                encoded.append(value)
            else:
                raise TypeError(f"unsupported result value type {type(value).__name__}")
        out.append(encoded)
    return out


if __name__ == "__main__":
    main()
