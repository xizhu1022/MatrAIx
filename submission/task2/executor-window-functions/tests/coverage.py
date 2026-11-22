"""Reviewer tool (not used by the verifier): how often each requirement of the instruction is
exercised by the generated cases.

    python tests/coverage.py            # needs sqlglot importable, e.g. PYTHONPATH=environment/sqlglot

Every window feature and clause named in instruction.md is counted per group from the parsed
SQL of the generated cases, and features exercised fewer than MIN_CASES times are flagged.
"""

from __future__ import annotations

import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402

import querygen  # noqa: E402

MIN_CASES = 3

FUNCTIONS = {
    exp.RowNumber: "ROW_NUMBER",
    exp.Rank: "RANK",
    exp.DenseRank: "DENSE_RANK",
    exp.Ntile: "NTILE",
    exp.PercentRank: "PERCENT_RANK",
    exp.CumeDist: "CUME_DIST",
    exp.Sum: "SUM",
    exp.Avg: "AVG",
    exp.Min: "MIN",
    exp.Max: "MAX",
    exp.Lag: "LAG",
    exp.Lead: "LEAD",
    exp.FirstValue: "FIRST_VALUE",
    exp.LastValue: "LAST_VALUE",
    exp.NthValue: "NTH_VALUE",
}


def bound_kind(value, side) -> str:
    if value == "UNBOUNDED":
        return f"UNBOUNDED {side}"
    if value == "CURRENT ROW":
        return "CURRENT ROW"
    return f"n {side}"


def features(sql: str) -> set[str]:
    tree = sqlglot.parse_one(sql)
    found: set[str] = set()

    for window in tree.find_all(exp.Window):
        if window.parent_select is None or isinstance(window.parent, exp.Select) and window in (
            window.parent.args.get("windows") or []
        ):
            continue  # a WINDOW clause definition
        func = window.this
        if isinstance(func, exp.Identifier):
            continue
        if isinstance(func, exp.IgnoreNulls):
            found.add("IGNORE NULLS")
            func = func.this
            found.add(f"IGNORE NULLS: {FUNCTIONS.get(type(func), type(func).__name__)}")
        if isinstance(func, exp.Count):
            found.add("COUNT(*)" if isinstance(func.this, exp.Star) else "COUNT(expr)")
        elif type(func) in FUNCTIONS:
            found.add(FUNCTIONS[type(func)])
        if isinstance(func, (exp.Lag, exp.Lead)):
            if func.args.get("offset"):
                found.add("LAG/LEAD offset")
            if func.args.get("default"):
                d = func.args["default"]
                found.add("LAG/LEAD default (expression)" if d.find(exp.Column) else "LAG/LEAD default (constant)")
        if isinstance(func, (exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Count)) and isinstance(
            func.this, (exp.Add, exp.Sub, exp.Mul)
        ):
            found.add("aggregate over an expression")

        if window.args.get("alias"):
            found.add("named window reference")
        has_order = window.args.get("order") is not None
        if window.args.get("partition_by"):
            found.add("PARTITION BY")
        if not window.args.get("partition_by") and not has_order and not window.args.get("alias"):
            found.add("OVER ()")
        if has_order:
            keys = window.args["order"].expressions
            if len(keys) > 1:
                found.add("ORDER BY with several keys")
            for k in keys:
                if k.args.get("desc"):
                    found.add("ORDER BY ... DESC")
        spec = window.args.get("spec")
        if spec is None:
            found.add("default frame (with ORDER BY)" if has_order else "default frame (no ORDER BY)")
        else:
            kind = spec.args.get("kind")
            found.add(f"{kind} frame")
            if spec.args.get("end") is None:
                found.add(f"{kind} short form")
            for v, side in ((spec.args.get("start"), spec.args.get("start_side")), (spec.args.get("end"), spec.args.get("end_side"))):
                if v is not None:
                    found.add(f"{kind} bound {bound_kind(v, side)}")
            exclude = spec.args.get("exclude")
            if exclude is not None:
                found.add(f"EXCLUDE {exclude.name if isinstance(exclude, exp.Expr) else exclude}")
            if kind == "RANGE" and any(isinstance(spec.args.get(b), exp.Literal) for b in ("start", "end")):
                found.add("RANGE value offset" + (" (DESC)" if window.args["order"].expressions[0].args.get("desc") else " (ASC)"))
        if not isinstance(window.parent, exp.Alias):
            found.add("window inside a larger expression")

    for select in tree.find_all(exp.Select):
        if select.args.get("qualify"):
            q = select.args["qualify"].this
            found.add("QUALIFY on a window expression" if q.find(exp.Window) else "QUALIFY on an alias")
        if select.args.get("windows"):
            found.add("WINDOW clause")
            if any(w.args.get("alias") for w in select.args["windows"]):
                found.add("WINDOW clause inheriting another window")
        if select.args.get("group") and select.find(exp.Window):
            found.add("windows over GROUP BY")
            if select.args.get("having"):
                found.add("windows over GROUP BY ... HAVING")
        if select.args.get("distinct") and select.find(exp.Window):
            found.add("DISTINCT with windows")
        order = select.args.get("order")
        if order and select.find(exp.Window):
            if any(o.find(exp.Window) for o in order.expressions):
                found.add("ORDER BY a window expression")
            else:
                found.add("ORDER BY with windows")
            if select.args.get("limit"):
                found.add("ORDER BY ... LIMIT with windows")
        if select.args.get("where") and select.find(exp.Window) and select.args.get("from_") and not isinstance(select.args["from_"].this, exp.Subquery):
            found.add("WHERE before windows")
        for join in select.args.get("joins") or []:
            if select.find(exp.Window):
                found.add("LEFT JOIN with windows" if join.side == "LEFT" else "JOIN with windows")
    if tree.args.get("with_") and tree.find(exp.Window):
        found.add("CTE with windows")
    if any(isinstance(s.this, exp.Select) and s.this.find(exp.Window) for s in tree.find_all(exp.Subquery)):
        found.add("window inside a subquery")
    for node in tree.find_all(exp.In, exp.Exists):
        if node.find(exp.Window):
            found.add("window inside IN / EXISTS subquery")
    return found


def main() -> None:
    generated = querygen.generate()
    counts: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for case in generated["cases"]:
        if case["tier"] == "regression":
            continue
        for f in features(case["sql"]):
            counts[f][case["tier"]] += 1

    tiers = [t for t in querygen.TIERS]
    print(f"{'feature':48s} " + " ".join(f"{t[:6]:>6s}" for t in tiers) + "  total")
    weak = []
    for feature in sorted(counts):
        row = counts[feature]
        total = sum(row.values())
        print(f"{feature:48s} " + " ".join(f"{row.get(t, 0):6d}" for t in tiers) + f"  {total:5d}")
        if total < MIN_CASES:
            weak.append(feature)
    print("\nexercised fewer than", MIN_CASES, "times:", ", ".join(weak) or "none")


if __name__ == "__main__":
    main()
