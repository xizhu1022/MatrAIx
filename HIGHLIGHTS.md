# Highlights · executor-window-functions

English | [中文](HIGHLIGHTS_zh.md)

An RL training task that asks an agent to add SQL window functions to sqlglot's Python
query engine: a real capability gap that spans the planner, the executor and the optimizer.

## What is new

1. **Behaviour-based reward with a unique answer by construction.** Every query runs in both
   the agent's engine and DuckDB. The generator only emits queries with exactly one correct
   result (explicit NULL ordering, total-order keys), so the reward checks behaviour, not
   strings, and is fully deterministic.
2. **Independent capability groups.** The reward is the share of 10 capability groups that
   are completely correct. Concerns that every query touches (data types, NULLs) live in one
   group, so a bug costs only the group it belongs to and each failed group names a root cause.
3. **Correctness and efficiency together.** A 50,000-row group with a 20 s limit rejects
   quadratic implementations: the agent has to write prefix sums, sparse tables and binary
   search, not only get the semantics right.
4. **Cross-module difficulty.** Some subquery shapes fail in sqlglot's optimizer before the
   new code runs, so the agent has to trace a wrong result back through another module.
5. **A hardened, self-tested verifier.** The agent's code runs as an unprivileged user and
   the verifier never imports it; 8 attack strategies all scored 0. One-defect reference
   solutions score in steps from 0.3 to 0.9, and a coverage audit confirms every requirement
   is tested at least 3 times.

## Results (final version, 3 trials per model, offline container)

| Model | Rewards | Mean |
|---|---|---|
| GPT-5.6 Luna | 0.3 / 0.1 / 0.1 | 0.17 |
| GPT-5.6 Terra | 0.5 / 0.9 / 0.2 | 0.53 |
| GPT-5.6 Sol | 0.8 / 1.0 / 1.0 | 0.93 |

The reward rises with model capability, and all 9 trials kept every regression case passing.
The mid-tier model shows the widest spread, which is where RL learns the most.

## Insights

- **Partial credit should follow independent capabilities.** Then the reward measures progress
  and the failed groups explain why.
- **Difficulty is a property of a task and a model together.** Measuring a ladder of models
  shows where a task gives a useful signal.
- **With a fair, precise specification, strong agents can test themselves against it.**
  Difficulty has to come from semantics, efficiency and interactions between modules.
