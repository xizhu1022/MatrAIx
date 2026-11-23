# Evidence for `executor-window-functions`

Logs and scripts behind the numbers in the task's `README.md`. Nothing here is used by the task
itself (the task directory is self-contained).

| File | What it shows |
|---|---|
| `selfcheck_final.txt` | `tools/selfcheck.py --repeats 2 -v` on the final task: oracle 1.0 and no-op 0.0 on every repeat, `RESULT: VALID`, per-group verifier output |
| `attacks_and_variants.txt` | 10 attack "solutions" (all 0) plus a timing attack (loses only the scale group, 0.9) and an oracle saved with mode 0600 files (1.0), each run through the full trial lifecycle, with the blocking error from the verifier log |
| `threshold_experiment.txt` | 19 saved solutions re-scored under the all-or-nothing rule and under in-group thresholds (why the threshold was not adopted) |
| `agent_runs/*.json` | the 9 agent trials on the final task (GPT-5.6 Luna, Terra, Sol × 3): reward, stop reason, turns, minutes, cost, the model's own summary, and the verifier log tail |
| `agent_runs_rescored_final.txt` | the 9 saved agent solutions re-scored under the final verifier: identical rewards |
| `or_agent.py` | the agent harness: runs a model through OpenRouter inside a container started from the environment image (`--network none`, task CPU/memory limits), then exports `/app` and scores it in a fresh verifier container, as in a trial. Reads the API key from the file named by `$OPENROUTER_KEY_FILE` |
| `run_variants.py` | runs variant `solve.sh` scripts through the same trial lifecycle (uses `selfcheck.py` from `$SELFCHECK_TOOLS`) |
| `exec_cases.py`, `rescore.py`, `report_rescore.py` | local re-scoring of saved solutions under alternative scoring rules |
| `summarize_runs.py` | the per-model result tables |
