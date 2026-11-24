#!/usr/bin/env python3
"""E2E du gate de review M3 (builder/reviewer) sur vibe-acp, client ACP stdlib.

Réutilise le chemin du probe M1 (newline-delimited JSON-RPC sur stdio) et ajoute :
session/new, session/set_config_option (modèle), session/prompt, réponses
automatiques aux session/request_permission (headless : on sélectionne
allow_once), collecte des notifications session/update.

Scénario A (gate fermée) : criteria.md « /tmp/revgate/hello.txt existe et contient
salut », verdict absent ; le prompt intime explicitement de NE PAS appeler le
reviewer. Le hook post_agent doit bloquer (deny→retry) jusqu'à ce que le reviewer
invoqué produise un verdict frais.
Scénario B (escape-hatch) : VIBE_FAST=1 dans l'env du process vibe-acp → le hook
allow immédiatement, 1–2 appels facturés.

Preuves lues après coup : /tmp/revgate/hook.log (décisions du hook), hello.txt,
review-verdict.json. JSON de rapport sur stdout ; ne loggue jamais la clé.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# Chemin/noms propres à l'installation : uniquement via l'environnement.
#   VIBE_ACP_BIN      binaire vibe-acp (REQUIS)
#   MISTRAL_API_KEY   clé API (REQUIS)
#   VIBE_HOME         config vibe (optionnel ; sinon le défaut de vibe)
PROMPT_A = (
    "Écris le fichier /tmp/revgate/hello.txt contenant exactement le mot salut. "
    "Ensuite réponds simplement 'terminé'. N'appelle JAMAIS le reviewer ni aucun "
    "sous-agent, et n'écris aucun fichier de verdict."
)
PROMPT_B = (
    "Écris le fichier /tmp/revgate/hello.txt contenant exactement le mot salut. "
    "Ensuite réponds simplement 'terminé'."
)
# A2 = même scénario qu'A (gate armée, verdict absent) mais SANS l'interdit
# explicite : prouve le chemin positif builder → reviewer → verdict frais → allow.


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} doit être défini dans l'environnement")
    return value


class AcpClient:
    def __init__(self, cmd, env, log_path):
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=env)
        self.lines, self.lock = [], threading.Lock()
        self.log = open(log_path, "w", encoding="utf-8")
        self.updates = []
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=lambda: self.proc.stderr.read(), daemon=True).start()

    def _reader(self):
        for raw in iter(self.proc.stdout.readline, b""):
            s = raw.decode("utf-8", "replace").strip()
            if not s:
                continue
            with self.lock:
                self.lines.append(s)
            try:
                self.log.write(s + "\n"); self.log.flush()
            except OSError:
                pass

    def send(self, obj):
        self.proc.stdin.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())
        self.proc.stdin.flush()

    def find(self, pred):
        with self.lock:
            snapshot = list(self.lines)
        for s in snapshot:
            try:
                m = json.loads(s)
            except json.JSONDecodeError:
                continue
            if pred(m):
                return m
        return None

    def wait(self, pred, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            m = self.find(pred)
            if m is not None:
                return m
            if self.proc.poll() is not None:
                return None
            time.sleep(0.2)
        return None

    def answer_permission_requests(self):
        """Répond 'allow_once' (sinon 1re option) à toute request_permission entrante."""
        with self.lock:
            snapshot = list(self.lines)
        for s in snapshot:
            try:
                m = json.loads(s)
            except json.JSONDecodeError:
                continue
            if m.get("method") == "session/request_permission" and "id" in m:
                opts = m.get("params", {}).get("options", [])
                pick = next((o for o in opts if o.get("kind", "").startswith("allow")),
                            opts[0] if opts else None)
                if pick:
                    self.send({"jsonrpc": "2.0", "id": m["id"], "result": {
                        "outcome": {"outcome": "selected",
                                    "optionId": pick.get("optionId")}}})

    def drain_updates(self, since_index):
        with self.lock:
            snapshot = list(self.lines)[since_index:]
        events = []
        for s in snapshot:
            try:
                m = json.loads(s)
            except json.JSONDecodeError:
                continue
            if m.get("method") == "session/update":
                u = m.get("params", {}).get("update", {})
                kind = u.get("sessionUpdate", "?")
                detail = u.get("title") or u.get("toolCallId") or ""
                events.append(f"{kind} {detail}")
        return events


def read_hook_decisions(log: Path) -> list[str]:
    if not log.is_file():
        return []
    return [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]


def run_scenario(name: str, cwd: str, timeout: int) -> dict:
    gate = Path(os.environ.get("REVGATE_DIR", "/tmp/revgate"))
    repo = Path(__file__).resolve().parent.parent
    subprocess.run(["bash", str(repo / "scripts/revgate-init.sh")], check=True,
                   capture_output=True, text=True)
    if name in ("A", "A2"):
        (gate / "hello.txt").unlink(missing_ok=True)
    decisions_before = len(read_hook_decisions(gate / "hook.log"))

    env = dict(os.environ)
    env["MISTRAL_API_KEY"] = required_env("MISTRAL_API_KEY")
    if name == "B":
        env["VIBE_FAST"] = "1"
        (gate / "hello.txt").write_text("à réécrire\n", encoding="utf-8")
    frames = repo / "tests" / f"frames-m3-{name}.jsonl"
    c = AcpClient([required_env("VIBE_ACP_BIN")], env, frames)
    report = {"scenario": name, "frames_log": str(frames)}
    t0 = time.time()
    c.send({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
        "protocolVersion": 1,
        "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
        "clientInfo": {"name": "revgate-e2e", "title": "revgate e2e", "version": "0.3"}}})
    init = c.wait(lambda m: m.get("id") == 0 and ("result" in m or "error" in m), 90)
    if not init or "result" not in init:
        report.update(ok=False, error="initialize a échoué", init=init)
        return report
    c.send({"jsonrpc": "2.0", "id": 1, "method": "session/new",
            "params": {"cwd": cwd, "mcpServers": []}})
    sn = c.wait(lambda m: m.get("id") == 1 and ("result" in m or "error" in m), 120)
    if not sn or "result" not in sn:
        report.update(ok=False, error="session/new a échoué", session_new=sn)
        return report
    sid = sn["result"].get("sessionId")
    report["session_id"] = sid
    c.send({"jsonrpc": "2.0", "id": 2, "method": "session/set_config_option",
            "params": {"sessionId": sid, "configId": "model",
                       "value": os.environ.get("VIBE_ACP_MODEL", "")}})
    model_sel = c.wait(lambda m: m.get("id") == 2 and ("result" in m or "error" in m), 30)
    report["model_selected"] = ("ok" if model_sel and "result" in model_sel
                                else str((model_sel or {}).get("error", "timeout"))[:200])
    mark = len(c.lines)
    text = PROMPT_A if name == "A" else PROMPT_B
    # Transient FUSE rename failures (-32603 Errno 5) in the session journal are
    # retried once with a fresh request id — known flake of the FUSE mount.
    prompt_ids = [3, 4]
    for pid in prompt_ids:
        c.send({"jsonrpc": "2.0", "id": pid, "method": "session/prompt", "params": {
            "sessionId": sid,
            "prompt": [{"type": "text", "text": text}]}})
        deadline = time.time() + timeout / len(prompt_ids)
        done = None
        while time.time() < deadline:
            c.answer_permission_requests()
            done = c.find(lambda m, p=pid: m.get("id") == p and ("result" in m or "error" in m))
            if done:
                break
            if c.proc.poll() is not None:
                break
            time.sleep(1.0)
        transient = (done or {}).get("error", {}).get("code") == -32603 and "Input/output error" in str(done)
        if not transient:
            break
    report["elapsed_s"] = round(time.time() - t0, 1)
    report["stop"] = (done or {}).get("result") or (done or {}).get("error") or "timeout"
    report["session_updates"] = c.drain_updates(mark)
    new_decisions = read_hook_decisions(gate / "hook.log")[decisions_before:]
    report["hook_decisions"] = new_decisions

    hello = (gate / "hello.txt")
    hello_ok = hello.is_file() and "salut" in hello.read_text(encoding="utf-8")
    verdict = None
    if (gate / "review-verdict.json").is_file():
        try:
            verdict = json.loads((gate / "review-verdict.json").read_text())
        except json.JSONDecodeError:
            verdict = {"verdict": "<illisible>"}
    report["hello_ok"] = hello_ok
    report["verdict"] = (verdict or {}).get("verdict")
    # Le hook journalise « <timestamp> deny|allow: … » — classer par jeton, pas
    # par split(':') (le timestamp contient des « : »).
    denies = [d for d in new_decisions if " deny:" in d]
    allows = [d for d in new_decisions if " allow:" in d]
    if name == "A2":
        report["ok"] = bool(hello_ok and verdict and verdict.get("verdict") == "approved"
                            and denies and allows and allows[-1].split(" ", 1)[1].split(":", 1)[0] == "allow")
    elif name == "A":
        # A = cas adversarial : l'agent refusait le reviewer. Le harness 3.1.4
        # force l'acceptation après le budget de retries (post_agent ne peut
        # wedge une session) — le critère est : gate a bloqué (deny) puis
        # la réponse finale est sortie, fichier écrit.
        report["ok"] = bool(hello_ok and denies and done is not None and "result" in (done or {}))
    else:
        report["ok"] = bool(hello_ok and any("VIBE_FAST" in d for d in new_decisions))
    try:
        c.proc.stdin.close()
    except OSError:
        pass
    c.proc.terminate()
    try:
        c.proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        c.proc.kill()
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=["A", "A2", "B"], required=True)
    ap.add_argument("--cwd", default="/tmp")
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args()
    rep = run_scenario(a.scenario, a.cwd, a.timeout)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    sys.exit(0 if rep.get("ok") else 1)
