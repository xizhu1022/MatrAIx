#!/bin/bash
# Reference solution: adds a Window step to the planner (sqlglot/planner.py), teaches the Python
# executor to evaluate it (sqlglot/executor/python.py), and stops the optimizer's IN-subquery
# unnesting from grouping windowed subqueries (sqlglot/optimizer/unnest_subqueries.py).
# See README.md for the design.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "$HERE/files/planner.py" /app/sqlglot/planner.py
cp "$HERE/files/executor/python.py" /app/sqlglot/executor/python.py
cp "$HERE/files/optimizer/unnest_subqueries.py" /app/sqlglot/optimizer/unnest_subqueries.py

cd /app
python - <<'PY'
import logging
logging.disable(logging.WARNING)
from sqlglot.executor import execute

rows = execute(
    "SELECT id, SUM(x) OVER (ORDER BY id ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS s FROM t",
    tables={"t": [{"id": 1, "x": 1}, {"id": 2, "x": 2}, {"id": 3, "x": 3}]},
).rows
assert sorted(rows) == [(1, 1), (2, 3), (3, 5)], rows
print("window functions OK")
PY
