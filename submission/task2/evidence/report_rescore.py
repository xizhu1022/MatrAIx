"""Prints rewards under the all-or-nothing rule and under in-group thresholds."""
import json
import sys

tallies = json.load(open(sys.argv[1]))
GROUPS = ["types_nulls", "ranking", "aggregates", "rows_frames", "range_frames", "groups_exclude",
          "navigation", "composition", "subqueries", "scale"]


def score(tally, thresh):
    reg_ok = tally["regression"][0] == tally["regression"][1]
    pts, full, half, failed = 0.0, 0, 0, []
    for g in GROUPS:
        ok, tot = tally[g]
        if ok == tot:
            pts += 1
            full += 1
        elif thresh is not None and ok / tot >= thresh:
            pts += 0.5
            half += 1
            failed.append(f"{g}({ok}/{tot})½")
        else:
            failed.append(f"{g}({ok}/{tot})")
    return (round(pts / len(GROUPS), 3) if reg_ok else 0.0), full, half, failed, reg_ok


print(f"{'solution':18s} {'cases':>9s}  {'all-or-nothing':>14s}  {'>=90% -> 0.5':>12s}  {'>=80% -> 0.5':>12s}  groups below full (threshold 90%)")
for name, tally in tallies.items():
    win = [tally[g] for g in GROUPS]
    ok = sum(a for a, _ in win)
    tot = sum(b for _, b in win)
    a = score(tally, None)
    b = score(tally, 0.9)
    c = score(tally, 0.8)
    reg = "" if a[4] else "  [regression FAIL -> 0]"
    print(f"{name:18s} {ok:4d}/{tot:<4d}  {a[0]:14.2f}  {b[0]:12.2f}  {c[0]:12.2f}  {', '.join(b[3]) or '-'}{reg}")
