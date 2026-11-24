#!/usr/bin/env python3
"""Client de sonde ACP minimal (stdlib only) — reused across projects from
references/runtime-vibe-acp.md §7. Sends `initialize` over newline-delimited
JSON-RPC on stdio and optionally `session/new` to see what auth the agent
requests (session/new without a key => JSON-RPC -32000 "Missing API key for
mistral provider", the process survives - never wait for its death).

Never logs keys: nothing here reads an environment key; the report only contains
protocol frames.

Run:
    python3 tests/acp_probe.py --timeout 30 --then-session-new -- "$VIBE_ACP_BIN"
"""
import argparse, json, subprocess, sys, threading, time


def read_lines(pipe, sink):
    for line in iter(pipe.readline, b""):
        sink.append(line)
    pipe.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--then-session-new", action="store_true",
                    help="after a successful initialize, send session/new to see what auth is demanded")
    ap.add_argument("--cwd", default=".")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        ap.error("missing command after --")

    t0 = time.time()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out_lines, err_lines = [], []
    to = threading.Thread(target=read_lines, args=(proc.stdout, out_lines), daemon=True)
    te = threading.Thread(target=read_lines, args=(proc.stderr, err_lines), daemon=True)
    to.start(); te.start()

    init_req = {
        "jsonrpc": "2.0", "id": 0, "method": "initialize",
        "params": {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
            "clientInfo": {"name": "hermes-acp-probe", "title": "Hermes ACP probe", "version": "0.1.0"},
        },
    }
    frame = (json.dumps(init_req, separators=(",", ":")) + "\n").encode()
    send_err = None
    try:
        proc.stdin.write(frame); proc.stdin.flush()
    except Exception as e:
        send_err = repr(e)

    def wait_for(msg_id, deadline):
        while time.time() < deadline:
            for raw in list(out_lines):
                s = raw.decode("utf-8", "replace").strip()
                if not s:
                    continue
                try:
                    msg = json.loads(s)
                except json.JSONDecodeError:
                    other.append({"raw": s[:2000]}); continue
                if msg.get("id") == msg_id and ("result" in msg or "error" in msg):
                    return msg, s
            if proc.poll() is not None and not to.is_alive():
                break
            time.sleep(0.1)
        return None, None

    other = []
    resp, resp_line = wait_for(0, t0 + args.timeout)
    resp2 = resp2_line = None
    if args.then_session_new and resp and "result" in resp:
        sn = {"jsonrpc": "2.0", "id": 1, "method": "session/new",
              "params": {"cwd": args.cwd, "mcpServers": []}}
        try:
            proc.stdin.write((json.dumps(sn, separators=(",", ":")) + "\n").encode()); proc.stdin.flush()
        except Exception as e:
            other.append({"session_new_send_error": repr(e)})
        resp2, resp2_line = wait_for(1, time.time() + args.timeout)

    elapsed = time.time() - t0
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.terminate(); proc.wait(timeout=5)
    except Exception:
        proc.kill(); proc.wait(timeout=5)
    te.join(timeout=3)

    def pick(m):
        if not m:
            return None
        return m.get("result") if "result" in m else m.get("error")

    report = {
        "cmd": cmd,
        "protocol": "newline-delimited JSON-RPC 2.0 (ACP)",
        "send_error": send_err,
        "elapsed_s": round(elapsed, 2),
        "exit_code": proc.poll(),
        "initialize_response": pick(resp),
        "raw_response_line": resp_line[:4000] if resp_line else None,
        "session_new_response": pick(resp2),
        "session_new_raw_line": resp2_line[:4000] if resp2_line else None,
        "other_stdout_frames": other[:20],
        "stdout_lines_total": len(out_lines),
        "stderr": b"".join(err_lines).decode("utf-8", "replace")[:8000],
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if resp and "result" in resp else 1


if __name__ == "__main__":
    sys.exit(main())
