"""Drive the installed Windows DSH SDK over the existing SSH connection."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

WORKSPACE = r"D:\TradingAgents"
WINDOWS_SCRIPT = WORKSPACE + r"\integrations\dsh-bridge\bridge.py"
WINDOWS_PYTHON = WORKSPACE + r"\.venv\Scripts\python.exe"
LAUNCHER = r"D:\DeepSeeHarness\DSH\resources\runtime\cli\bin\dsh.cmd"
DESKTOP_HOME = Path(r"C:\Users\thebl\.dsh")
SESSION_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "ssh://win-home/D:/TradingAgents/codex-dsh"))


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def worker(packet: dict) -> int:
    """Resolve credentials on Windows only and run one durable SDK turn."""
    import yaml  # Already provided by TradingAgents' Windows environment.

    runtime = Path(os.environ["LOCALAPPDATA"]) / "dsh-coordination" / "tradingagents"
    runtime.mkdir(parents=True, exist_ok=True)
    secrets: list[str] = []

    def scrub(value: object) -> object:
        if isinstance(value, str):
            for secret in secrets:
                value = value.replace(secret, "<redacted>")
            return value
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items()}
        return value

    def emit(value: dict) -> None:
        print(json.dumps(scrub(value), ensure_ascii=False), flush=True)

    desktop_patch = yaml.safe_load(
        (DESKTOP_HOME / "profiles" / "desktop" / "cordis.patch.yml").read_text(encoding="utf-8")
    )
    adapter = next(row for row in desktop_patch if row.get("id") == "llm-pi-ai")
    default = next(row for row in desktop_patch if row.get("id") == "agent-default-model")["config"]
    connection_file = runtime / "connection.json"
    connection = (
        json.loads(connection_file.read_text(encoding="utf-8"))
        if connection_file.exists()
        else default
    )
    provider, model = connection["provider"], connection["model"]
    provider_config = json.loads(json.dumps(adapter["config"]["providers"][provider]))
    key_name = provider_config["apiKeyEnv"]
    env = os.environ.copy()
    if not env.get(key_name):
        credentials = yaml.safe_load(
            (DESKTOP_HOME / ".credentials.yaml").read_text(encoding="utf-8")
        )
        key = credentials.get("refs", {}).get(key_name)
        if not isinstance(key, str) or not key:
            emit(
                {
                    "kind": "failure",
                    "reason": "Configured model credential is unavailable",
                }
            )
            return 1
        env[key_name] = key
    secrets.append(env[key_name])
    requested_model = packet.get("model", model)
    if requested_model is not None:
        if not isinstance(requested_model, str) or not requested_model.strip():
            raise ValueError("model must be a non-empty string")
        configured_models = provider_config.get("models", [])
        if not any(entry.get("id") == requested_model for entry in configured_models):
            request = urllib.request.Request(
                provider_config["baseURL"].rstrip("/") + "/models",
                headers={"Authorization": "Bearer " + env[key_name]},
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                offered = {entry["id"] for entry in json.load(response).get("data", [])}
            if requested_model not in offered:
                raise ValueError("Requested model is not listed by the configured gateway")
            provider_config["models"] = configured_models + [
                {"id": requested_model, "input": ["text"]}
            ]
        model = requested_model
    env.update(
        {
            "DSH_HOME": str(runtime / "home"),
            "DSH_PERMISSION_MODE": "read-only",
            "DSH_MAX_TOKENS_AS_SUCCESS": "false",
            "PYTHONUTF8": "1",
        }
    )
    # Only the selected provider's non-secret configuration is needed.
    patches = [
        {
            "id": "llm-pi-ai",
            "name": adapter["name"],
            "config": {"providers": {provider: provider_config}},
        },
        {
            "id": "sdk-jsonrpc-server",
            "disabled": True,
        },
        {
            "insert": [
                {
                    "id": "coordination-jsonrpc-server",
                    "name": Path(WINDOWS_SCRIPT).with_name("session-server.mjs").as_uri(),
                    "inject": ["sdkAppStartup", "loader"],
                    "config": {
                        "sessionId": SESSION_ID,
                        "workspace": WORKSPACE,
                        "moduleRoot": "file:///D:/DeepSeeHarness/DSH/resources/app.asar/dsh/node_modules/",
                    },
                }
            ],
        },
    ]
    # Read-only review entry: no shell, writes, delegation, web or package tools.
    for entry in [
        "llm-retry",
        "tool-pwsh",
        "tool-bash",
        "tool-jobs",
        "tool-subagent",
        "tool-subagent-fork",
        "tool-subagent-control",
        "tool-subagent-list-agents",
        "tool-workflow",
        "tool-web",
        "tool-cordis",
        "tool-ask-user",
        "tool-goal",
        "tool-ralph",
    ]:
        patches.append({"id": entry, "disabled": True})
    timeout = int(packet.get("timeout", 240))
    if not 15 <= timeout <= 900:
        raise ValueError("timeout must be between 15 and 900 seconds")
    run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    run_dir = runtime / "runs" / run_id
    run_dir.mkdir(parents=True)
    overlay = run_dir / "sdk-overlay.yml"
    overlay.write_text(
        yaml.safe_dump(patches, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    (run_dir / "request.json").write_text(
        json.dumps(scrub(packet), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # Exclusive lock prevents concurrent clients writing the same session.
    lock = runtime / "session.lock"
    try:
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        emit(
            {
                "kind": "failure",
                "reason": "Session is busy; inspect its lock before retrying",
            }
        )
        return 1
    os.close(lock_fd)
    process: subprocess.Popen | None = None
    try:
        process = subprocess.Popen(
            [
                "cmd.exe",
                "/d",
                "/c",
                LAUNCHER,
                "--profile",
                "sdk",
                "--patch",
                str(overlay),
            ],
            cwd=WORKSPACE,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        incoming: queue.Queue[tuple[str, str | None]] = queue.Queue()

        def collect(stream, name: str) -> None:
            for line in stream:
                incoming.put((name, line.rstrip("\r\n")))
            incoming.put((name, None))

        for stream, name in [(process.stdout, "stdout"), (process.stderr, "stderr")]:
            threading.Thread(target=collect, args=(stream, name), daemon=True).start()

        def send(identifier: int, method: str, params: dict | None = None) -> None:
            frame = {"jsonrpc": "2.0", "id": identifier, "method": method}
            if params is not None:
                frame["params"] = params
            process.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
            process.stdin.flush()

        send(
            1,
            "initialize",
            {
                "cwd": WORKSPACE,
                "provider": provider,
                "model": model,
                "maxTokens": 4096,
            },
        )
        emit(
            {
                "kind": "started",
                "session_id": SESSION_ID,
                "run_id": run_id,
                "provider": provider,
                "model": model,
                "permission": "read-only",
            }
        )
        deadline = time.monotonic() + timeout
        running = False
        acknowledged = False
        completed = False
        shutdown_sent = False
        failed = False
        turn_reason: str | None = None
        assistant_text = ""
        stderr_tail: list[str] = []
        with (run_dir / "events.jsonl").open("w", encoding="utf-8") as log:
            while time.monotonic() < deadline:
                try:
                    name, line = incoming.get(timeout=1)
                except queue.Empty:
                    if process.poll() is not None:
                        break
                    continue
                if line is None:
                    if name == "stdout":
                        break
                    continue
                if name == "stderr":
                    stderr_tail.append(str(scrub(line)))
                    stderr_tail = stderr_tail[-20:]
                    continue
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    stderr_tail.append("Non-JSON stdout: " + str(scrub(line))[:500])
                    continue
                safe_frame = scrub(frame)
                log.write(json.dumps(safe_frame, ensure_ascii=False) + "\n")
                log.flush()
                if "error" in frame:
                    emit({"kind": "rpc_error", "frame": safe_frame})
                    failed = True
                    break
                if frame.get("id") == 1:
                    emit({"kind": "initialized", "result": frame.get("result")})
                    send(
                        2,
                        "session/prompt",
                        {
                            "sessionId": SESSION_ID,
                            "contentBlocks": [{"type": "text", "text": packet["prompt"]}],
                        },
                    )
                elif frame.get("id") == 2:
                    acknowledged = True
                    emit({"kind": "accepted", "result": frame.get("result")})
                elif frame.get("id") == 3:
                    break
                elif frame.get("method") == "session.status":
                    params = frame.get("params", {})
                    if params.get("sessionId") == SESSION_ID:
                        running |= params.get("status") == "running"
                        if acknowledged and running and params.get("status") == "idle":
                            completed = True
                            send(3, "shutdown")
                            shutdown_sent = True
                        emit({"kind": "status", "params": params})
                elif frame.get("method") == "coordination.session":
                    emit({"kind": "session_opened", "params": frame.get("params")})
                elif frame.get("method") == "session.event":
                    params = frame.get("params", {})
                    event = params.get("event", {})
                    if params.get("sessionId") == SESSION_ID:
                        if event.get("type") == "turn/end":
                            turn_reason = event.get("data", {}).get("reason", {}).get("kind")
                        if event.get("type") == "assistant/message":
                            data = event.get("data", {})
                            message = data.get("message", data)
                            text = "".join(
                                block.get("text", "")
                                for block in message.get("content", [])
                                if block.get("type") == "text"
                            )
                            if text.strip():
                                assistant_text = text
                    emit({"kind": "event", "params": safe_frame.get("params")})
        if not shutdown_sent and process.poll() is None:
            with contextlib.suppress(OSError, ValueError):
                send(3, "shutdown")
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # Only this bridge-owned process tree is stopped, never Desktop.
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            process.wait(timeout=5)
        result = {
            "kind": "finished",
            "completed": completed
            and not failed
            and turn_reason in {"completed", "end-turn"}
            and bool(assistant_text.strip())
            and process.returncode == 0,
            "turn_reason": turn_reason,
            "assistant_text": scrub(assistant_text),
            "exit_code": process.returncode,
            "run_id": run_id,
            "session_id": SESSION_ID,
            "stderr_tail": stderr_tail,
            "remote_evidence": str(run_dir),
            "at": utc_now(),
        }
        (run_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if result["completed"] and process.returncode == 0:
            connection_file.write_text(
                json.dumps(
                    {"provider": provider, "model": model, "session_id": SESSION_ID},
                    indent=2,
                ),
                encoding="utf-8",
            )
        emit(result)
        return 0 if result["completed"] and process.returncode == 0 else 1
    finally:
        if process is not None:
            if process.poll() is None:
                process.stdin.close()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
            for stream in [process.stdin, process.stdout, process.stderr]:
                if stream is not None and not stream.closed:
                    stream.close()
        lock.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument(
        "--model", help="Explicit model override for this coordination process only"
    )
    args = parser.parse_args()
    if args.worker:
        sys.stdin.reconfigure(encoding="utf-8", errors="strict")
        sys.stdout.reconfigure(encoding="utf-8")
        return worker(json.load(sys.stdin))
    if args.prompt_file is None:
        parser.error("--prompt-file is required")
    packet = {
        "prompt": args.prompt_file.read_text(encoding="utf-8"),
        "timeout": args.timeout,
    }
    if args.model:
        packet["model"] = args.model
    ssh = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=3",
        "win-home",
        f"{WINDOWS_PYTHON} -u -B {WINDOWS_SCRIPT} --worker",
    ]
    process = subprocess.Popen(
        ssh,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        text=True,
        encoding="utf-8",
    )
    process.stdin.write(json.dumps(packet, ensure_ascii=True))
    process.stdin.close()
    evidence = Path(__file__).parent / ".runtime"
    evidence.mkdir(exist_ok=True)
    record = evidence / (dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ") + ".jsonl")
    with record.open("x", encoding="utf-8") as output:
        for line in process.stdout:
            output.write(line)
            output.flush()
            frame = json.loads(line)
            if frame.get("kind") != "event":
                print(line, end="", flush=True)
                continue
            event = frame.get("params", {}).get("event", {})
            if event.get("type") in {
                "turn/end",
                "assistant/message",
                "tool/start",
                "tool/end",
            }:
                print(line, end="", flush=True)
    return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
