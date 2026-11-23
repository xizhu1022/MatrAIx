"""Minimal coding-agent harness: an OpenRouter model solves a Harbor task inside the real,
network-less environment container, then /app is exported and scored exactly like
tools/selfcheck.py does (fresh verifier container, test.sh, reward.txt).

    python or_agent.py <task_dir> <model> <run_name> [--max-turns 150] [--max-cost 20] [--max-minutes 90]

The API key is read from the file named by $OPENROUTER_KEY_FILE and never printed or logged.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, os.environ.get("SELFCHECK_TOOLS", "tools"))  # directory containing selfcheck.py
import selfcheck  # noqa: E402

API = "https://openrouter.ai/api/v1/chat/completions"
PRICE_IN, PRICE_OUT = 4e-6, 20e-6  # upper-bound fallback if the API does not report cost
OUTPUT_LIMIT = 8000
COMMAND_TIMEOUT = 300

SYSTEM = """You are an autonomous software engineering agent working inside a Linux container.
The repository you must change is at /app (your working directory). The container has no
internet access; Python 3.12 and the packages already installed are all you have.

Tools:
- bash: run a shell command in the container (each call is a fresh `bash -lc`; it times out
  after 300 seconds; output is truncated to the first and last few thousand characters).
- write_file: create or overwrite a file with the given content.
- finish: call this once, when the task is completely done.

Work autonomously: there is no human to answer questions. Read the relevant code, implement
the change, and test it thoroughly yourself before calling finish."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command in the container (bash -lc, cwd /app, 300 s timeout).",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file in the container.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Declare the task complete.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        },
    },
]


def truncate(text: str) -> str:
    if len(text) <= OUTPUT_LIMIT:
        return text
    head, tail = text[: OUTPUT_LIMIT // 4], text[-(3 * OUTPUT_LIMIT) // 4 :]
    return f"{head}\n... [{len(text) - OUTPUT_LIMIT} characters omitted] ...\n{tail}"


def run_bash(container: str, command: str) -> str:
    try:
        r = subprocess.run(
            ["docker", "exec", "-w", "/app", container, "timeout", str(COMMAND_TIMEOUT), "bash", "-lc", command],
            capture_output=True,
            timeout=COMMAND_TIMEOUT + 30,
        )
        out = (r.stdout + r.stderr).decode("utf-8", "replace")
        note = "\n[command timed out after 300 s]" if r.returncode == 124 else ""
        return truncate(out) + note + f"\n[exit code {r.returncode}]"
    except subprocess.TimeoutExpired:
        return "[command did not return; killed]"


def write_file(container: str, path: str, content: str) -> str:
    r = subprocess.run(
        ["docker", "exec", "-i", "-w", "/app", container, "sh", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"', "sh", path],
        input=content.encode(),
        capture_output=True,
        timeout=60,
    )
    if r.returncode != 0:
        return "write failed: " + r.stderr.decode("utf-8", "replace")[-500:]
    return f"wrote {len(content)} characters to {path}"


def call_model(key: str, model: str, messages: list, reasoning: str | None) -> dict:
    body = {"model": model, "messages": messages, "tools": TOOLS, "usage": {"include": True}}
    if reasoning:
        body["reasoning"] = {"effort": reasoning}
    req = urllib.request.Request(
        API,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "X-Title": "task-difficulty-check"},
    )
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=900) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if e.code in (429, 500, 502, 503, 504) and attempt < 5:
                time.sleep(10 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {e.code}: {detail}") from None
        except Exception as e:  # noqa: BLE001 - URLError, SSL errors, resets, truncated JSON: retry
            if attempt < 5:
                time.sleep(10 * (attempt + 1))
                continue
            raise RuntimeError(f"network error: {e}") from None
    raise RuntimeError("unreachable")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("task_dir")
    ap.add_argument("model")
    ap.add_argument("run_name")
    ap.add_argument("--max-turns", type=int, default=150)
    ap.add_argument("--max-cost", type=float, default=20.0)
    ap.add_argument("--max-minutes", type=float, default=90.0)
    ap.add_argument("--reasoning", default="high")
    a = ap.parse_args()

    key = Path(os.environ["OPENROUTER_KEY_FILE"]).read_text().strip()
    task = Path(a.task_dir).resolve()
    meta = tomllib.loads((task / "task.toml").read_text())
    out_dir = Path(__file__).parent / "or_runs" / a.run_name
    out_dir.mkdir(parents=True, exist_ok=True)
    log = open(out_dir / "transcript.jsonl", "w")

    env_img, ver_img = f"orrun-{a.run_name}-env", f"orrun-{a.run_name}-verifier"
    for ctx, img in ((task / "environment", env_img), (task / "tests", ver_img)):
        err = selfcheck.build(ctx, img, 900)
        assert err is None, err

    container = f"orrun-{a.run_name}-{uuid.uuid4().hex[:6]}"
    env = meta["environment"]
    subprocess.run(
        ["docker", "create", "--name", container, "--network", "none", "-w", "/app", "--user", "root",
         "--cpus", str(env["cpus"]), "--memory", f"{env['memory_mb']}m", "--entrypoint", "sleep", env_img, "infinity"],
        check=True, capture_output=True,
    )
    subprocess.run(["docker", "start", container], check=True, capture_output=True)

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": (task / "instruction.md").read_text()},
    ]
    cost, turns, started, stop_reason, nudges = 0.0, 0, time.time(), None, 0
    summary = ""
    try:
        while True:
            if turns >= a.max_turns:
                stop_reason = f"max turns ({a.max_turns})"
                break
            if cost >= a.max_cost:
                stop_reason = f"max cost (${a.max_cost})"
                break
            if (time.time() - started) / 60 >= a.max_minutes:
                stop_reason = f"max time ({a.max_minutes} min)"
                break
            resp = call_model(key, a.model, messages, a.reasoning)
            turns += 1
            usage = resp.get("usage") or {}
            step_cost = usage.get("cost")
            if step_cost is None:
                step_cost = usage.get("prompt_tokens", 0) * PRICE_IN + usage.get("completion_tokens", 0) * PRICE_OUT
            cost += float(step_cost)
            msg = resp["choices"][0]["message"]
            log.write(json.dumps({"turn": turns, "usage": usage, "message": msg}) + "\n")
            log.flush()
            assistant = {"role": "assistant", "content": msg.get("content") or ""}
            if msg.get("tool_calls"):
                assistant["tool_calls"] = msg["tool_calls"]
            if msg.get("reasoning_details"):
                assistant["reasoning_details"] = msg["reasoning_details"]
            messages.append(assistant)

            calls = msg.get("tool_calls") or []
            if not calls:
                nudges += 1
                if nudges > 3:
                    stop_reason = "model stopped calling tools"
                    break
                messages.append({"role": "user", "content": "Continue working with the tools. Call finish when the task is completely done."})
                continue

            finished = False
            for call in calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                if name == "bash":
                    result = run_bash(container, str(args.get("command", "")))
                elif name == "write_file":
                    result = write_file(container, str(args.get("path", "")), str(args.get("content", "")))
                elif name == "finish":
                    summary = str(args.get("summary", ""))
                    result = "ok"
                    finished = True
                else:
                    result = f"unknown tool {name}"
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
                log.write(json.dumps({"turn": turns, "tool": name, "args": args if name != "write_file" else {"path": args.get("path"), "chars": len(str(args.get("content", "")))}, "result": result[:2000]}) + "\n")
                log.flush()
            print(f"[turn {turns:3d}] ${cost:6.2f}  {', '.join(c['function']['name'] for c in calls)}", flush=True)
            if finished:
                stop_reason = "finished"
                break
    except Exception as e:  # noqa: BLE001 - report harness/API failures and still score /app
        stop_reason = f"harness error: {e}"
    minutes = (time.time() - started) / 60

    # stop the agent container (kills every process), export /app, score in a fresh verifier
    subprocess.run(["docker", "kill", container], capture_output=True)
    app = subprocess.run(["docker", "cp", f"{container}:/app", "-"], capture_output=True, timeout=1800).stdout
    subprocess.run(["docker", "rm", "-f", container], capture_output=True)
    (out_dir / "app.tar").write_bytes(app)  # keep the agent's final /app for re-scoring

    with selfcheck.Container(ver_img, "verifier", a.run_name, meta) as v:
        v.copy_dir_in(task / "tests", "/tests")
        v.import_tar(app)
        v.start()
        rc, vlog = v.exec(["bash", "/tests/test.sh"], float(meta["verifier"]["timeout_sec"]))
        v.stop()
        raw, err = v.read_reward()
        reward = selfcheck.parse_reward(raw)[0] if raw is not None else 0.0

    result = {
        "model": a.model, "run": a.run_name, "reward": reward, "stop_reason": stop_reason,
        "turns": turns, "cost_usd": round(cost, 2), "minutes": round(minutes, 1), "summary": summary,
        "verifier_log": vlog[-6000:],
    }
    (out_dir / "result.json").write_text(json.dumps(result, indent=1))
    print(json.dumps({k: v for k, v in result.items() if k != "verifier_log"}, indent=1))
    print("\n".join(l for l in vlog.splitlines() if l.startswith(("tier", "regression", "reward"))))
    subprocess.run(["docker", "rmi", "-f", env_img, ver_img], capture_output=True)


if __name__ == "__main__":
    main()
