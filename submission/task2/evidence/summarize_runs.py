"""Summarise agent runs as per-model result tables.

    python summarize_runs.py <prefix> [<prefix> ...]     e.g. luna_run terra_run v31_sol_run
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).parent / "or_runs"
for prefix in sys.argv[1:]:
    rows, rewards = [], []
    for d in sorted(ROOT.glob(prefix + "*")):
        f = d / "result.json"
        if not f.exists():
            continue
        r = json.loads(f.read_text())
        lines = r["verifier_log"].splitlines()
        tiers = [l.split() for l in lines if l.startswith("tier")]
        failed = [f"{t[1]} ({t[2]})" for t in tiers if t[-1] != "PASS"]
        reg = next((l.split()[1] for l in lines if l.startswith("regression")), "?")
        rewards.append(r["reward"])
        rows.append((d.name[-1], r["reward"], f"{len(tiers) - len(failed)}/{len(tiers)}", failed, r, reg))
    if not rows:
        continue
    full = sum(1 for x in rewards if x == 1.0)
    print(f"\n### {rows[0][4]['model']}: {len(rows)} runs, {full} full marks, mean {sum(rewards) / len(rewards):.2f}")
    print("| 试验 | 得分 | 通过组数 | 未通过的组 | 时间 / 花费 / 轮数 | 回归 | 结束原因 |")
    print("|---|---|---|---|---|---|---|")
    for trial, reward, passed, failed, r, reg in rows:
        print(f"| {trial} | {reward} | {passed} | {'、'.join(failed) or '—'} | {r['minutes']} min / ${r['cost_usd']} / {r['turns']} | {reg} | {r['stop_reason']} |")
