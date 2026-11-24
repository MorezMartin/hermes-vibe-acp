#!/usr/bin/env python3
"""E2E M2 du plugin vibe-acp : pont outillage, approvals, projection — client ACP stdlib.

Deux parties :

1. HORS-LIGNE (0 appel facturé) — pilote le vrai client du plugin contre
   ``tests/stub_vibe_agent.py``, un faux agent ACP qui rejoue le comportement
   observé de vibe 3.1.4 (tool_call/tool_call_update, request_permission avec
   ``_meta.required_permissions``). Vérifie :
   - allowlist outillage sortant (VIBE_ACP_TOOLS) : bash Hermes jamais proposé par
     défaut, outils mémoire/todo/skills proposés ; ``all`` les propose tous ;
   - politique approvals (VIBE_ACP_APPROVAL) : défaut cancel → réponse cancelled
     propre (jamais de hang), allow → allow_once, allowlist → match kind/pattern ;
   - projection des tool_calls entrants → hermes_projected_messages (paires
     assistant+tool « déjà accomplies ») et hermes_provider_tool_iterations.

2. RÉEL (--live, appels facturés) — avec VIBE_ACP_BIN + MISTRAL_API_KEY :
   - la tâche « écrire /tmp/hva-m2/hello.txt puis le relire » est EXECUTÉE par
     vibe avec SES outils (fichier existe, évènements tool_call vus et projetés),
     politique allow (sinon rien ne s'exécute) ;
   - discover_models() expose le catalogue live (configOptions vibe) — sans
     inférence ;
   - politique défaut cancel sur un outil risqué : le tour répond proprement,
     jamais de hang (journal des décisions + log INFO).

Noms/chemins propres à l'installation uniquement via l'environnement :
VIBE_ACP_BIN, MISTRAL_API_KEY, VIBE_HOME, VIBE_ACP_MODEL, HERMES_SRC (défaut /opt/hermes).
Ne loggue jamais la clé.
"""
import argparse
import importlib.util
import json
import logging
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
STUB = HERE / "stub_vibe_agent.py"
WORK = Path(os.environ.get("HVA_M2_WORKDIR", "/tmp/hva-m2"))

HERMES_SRC = os.environ.get("HERMES_SRC", "/opt/hermes")
if HERMES_SRC and Path(HERMES_SRC).is_dir():
    sys.path.insert(0, HERMES_SRC)

TOOLS = [
    {"type": "function", "function": {"name": "bash", "description": "Run a shell command",
                                      "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "write_file", "description": "Write a file",
                                      "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "memory", "description": "Save a memory",
                                      "parameters": {"type": "object", "properties": {"fact": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "todo", "description": "Manage the todo list",
                                      "parameters": {"type": "object", "properties": {}}}},
]


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} doit être défini dans l'environnement")
    return value


def load_plugin():
    spec = importlib.util.spec_from_file_location("vibe_acp_plugin", REPO / "__init__.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_client(mod, env_overrides: dict[str, str | None]):
    """Client du plugin pointé sur le stub ; env posé AVANT create() (lu au moment du tour)."""
    for key in ("VIBE_ACP_TOOLS", "VIBE_ACP_APPROVAL", "VIBE_ACP_APPROVAL_ALLOWLIST", "VIBE_ACP_MODEL"):
        os.environ.pop(key, None)
    for key, val in env_overrides.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val
    return mod._vibe_client_class()(command=sys.executable, args=[str(STUB)], acp_cwd=str(WORK))


def scenario_stub(mod, name: str, env: dict, check) -> dict:
    report = WORK / f"stub_report_{name}.jsonl"
    report.unlink(missing_ok=True)
    os.environ["STUB_REPORT"] = str(report)
    client = build_client(mod, env)
    t0 = time.time()
    resp = client.chat.completions.create(
        messages=[{"role": "user", "content": "scenario " + name}], tools=TOOLS, timeout=30)
    elapsed = round(time.time() - t0, 2)
    received = []
    if report.is_file():
        for line in report.read_text(encoding="utf-8").splitlines():
            try:
                received.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    prompt_text = next((r.get("prompt", "") for r in received if "prompt" in r), "")
    perms = [r.get("permission_response") for r in received if "permission_response" in r]
    out = {"scenario": name, "elapsed_s": elapsed,
           "content": (resp.choices[0].message.content or "").strip(),
           "approvals": list(getattr(resp, "vibe_approval_decisions", [])),
           "permission_replies": perms,
           "iterations": getattr(resp, "hermes_provider_tool_iterations", None),
           "projected_rows": getattr(resp, "hermes_projected_messages", None)}
    ok, why = check(prompt_text, out)
    out["ok"], out["why"] = bool(ok), why
    return out


def check_default(prompt, out):
    fwd = out["projected_rows"] or []
    ok = ("memory" in prompt and "todo" in prompt and '"bash"' not in prompt
          and '"write_file"' not in prompt
          and [a["outcome"] for a in out["approvals"]] == ["cancelled"]
          and [r.get("outcome", {}).get("outcome") for r in out["permission_replies"]] == ["cancelled"]
          and len(fwd) == 2 and fwd[0]["role"] == "assistant"
          and fwd[0]["tool_calls"][0]["function"]["name"] == "vibe_execute"
          and fwd[1]["role"] == "tool" and out["iterations"] == 1)
    return ok, "allowlist défaut (pas de bash/write_file), cancel propre, projection assistant+tool"


def check_tools_all(prompt, out):
    return '"bash"' in prompt and '"write_file"' in prompt, "VIBE_ACP_TOOLS=all propose tout le toolset"


def check_allow(prompt, out):
    return (out["approvals"][0]["outcome"] == "selected" and out["approvals"][0]["optionId"] == "allow_once"
            and out["iterations"] == 1), "allow → allow_once + iterations"


def check_allowlist_hit(prompt, out):
    return out["approvals"][0]["outcome"] == "selected", "allowlist execute → selected"


def check_allowlist_miss(prompt, out):
    return out["approvals"][0]["outcome"] == "cancelled", "allowlist défaut (read/search/fetch) → cancelled"


def check_allowlist_pattern(prompt, out):
    return out["approvals"][0]["outcome"] == "selected", "allowlist sur invocation_pattern → selected"


STUB_SCENARIOS = [
    ("default", {}, check_default),
    ("tools_all", {"VIBE_ACP_TOOLS": "all"}, check_tools_all),
    ("allow", {"VIBE_ACP_APPROVAL": "allow"}, check_allow),
    ("allowlist_hit", {"VIBE_ACP_APPROVAL": "allowlist", "VIBE_ACP_APPROVAL_ALLOWLIST": "execute"}, check_allowlist_hit),
    ("allowlist_miss", {"VIBE_ACP_APPROVAL": "allowlist"}, check_allowlist_miss),
    ("allowlist_pattern", {"VIBE_ACP_APPROVAL": "allowlist", "VIBE_ACP_APPROVAL_ALLOWLIST": "wipe-all"},
     check_allowlist_pattern),
]

PROMPT_LIVE = ("Crée dans le dossier courant le fichier hello.txt contenant exactement "
               "'bonjour M2', puis relis-le (cat hello.txt) et réponds 'OK'.")


def scenario_live(mod, name: str, approval: str | None, expect: str,
                  prompt: str = PROMPT_LIVE) -> dict:
    """expect: 'selected' (politique allow) | 'cancelled' (défaut headless)."""
    env = {"VIBE_ACP_APPROVAL": approval or "", "VIBE_FAST": "1",  # escape-hatch du gate M3, hors périmètre M2
           "MISTRAL_API_KEY": required_env("MISTRAL_API_KEY"),
           "VIBE_HOME": os.environ.get("VIBE_HOME", ""),
           "VIBE_ACP_MODEL": os.environ.get("VIBE_ACP_MODEL", "")}
    for key, val in env.items():
        if val:
            os.environ[key] = val
        else:
            os.environ.pop(key, None)
    for stale in (WORK / "hello.txt",):
        stale.unlink(missing_ok=True)
    client = mod._vibe_client_class()(command=required_env("VIBE_ACP_BIN"), acp_cwd=str(WORK))
    t0 = time.time()
    try:
        resp = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}], timeout=480)
        err = None
    except Exception as exc:  # noqa: BLE001 — preuve d'erreur, sans clé
        resp, err = None, str(exc)[:300]
    elapsed = round(time.time() - t0, 1)
    hello = WORK / "hello.txt"
    hello_text = hello.read_text(encoding="utf-8").strip() if hello.is_file() else None
    approvals = list(getattr(resp, "vibe_approval_decisions", [])) if resp is not None else []
    fwd = getattr(resp, "hermes_projected_messages", []) if resp is not None else []
    out = {"scenario": f"live_{name}", "elapsed_s": elapsed, "error": err,
           "hello": hello_text, "approvals": approvals, "projected_count": len(fwd),
           "iterations": getattr(resp, "hermes_provider_tool_iterations", 0) if resp is not None else 0,
           "content": (resp.choices[0].message.content or "").strip()[:200] if resp is not None else None}
    if expect == "selected":
        # vibe écrit parfois via son outil d'édition (auto-approuvé par le mode de
        # session) sans jamais demander d'approbation : approvals vides est valide,
        # toute approbation demandée doit en revanche avoir été selected.
        ok = (err is None and hello_text and "bonjour M2" in hello_text and out["projected_count"] >= 2
              and all(a["outcome"] == "selected" for a in approvals))
        out["why"] = "fichier écrit par vibe + tool events projetés + approvals selected (vides si auto-approuvé)"
    elif expect == "cancelled_required":
        # Prompt forçant bash : vibe DOIT demander une approbation, le défaut cancel
        # doit y répondre proprement (journal de décisions + pas de hang).
        ok = (err is None and out["approvals"]
              and all(a["outcome"] == "cancelled" for a in out["approvals"]))
        out["why"] = "approval risquée demandée → cancelled propre (prouvé par journal de décisions)"
    else:
        ok = err is None and not any(a["outcome"] == "selected" for a in approvals)
        out["why"] = "défaut cancel : tour clôt proprement (pas de hang), aucune approbation selected"
    out["ok"] = bool(ok)
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s", stream=sys.stderr)
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="scénarios réels (appels facturés)")
    args = ap.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    mod = load_plugin()

    reports = [scenario_stub(mod, n, e, c) for n, e, c in STUB_SCENARIOS]
    if args.live:
        discover: dict
        try:
            rows = mod.VibeACPProfile(name="vibe-acp").discover_models()
            discover = {"rows": len(rows or []), "ids": [r["id"] for r in (rows or [])][:8]}
        except Exception as exc:  # noqa: BLE001
            discover = {"rows": 0, "error": str(exc)[:200]}
        reports.append({"scenario": "live_discover_models", "ok": bool(discover.get("rows")), "discover": discover})
        reports.append(scenario_live(mod, "allow", "allow", "selected"))
        # Forcer une mutation via bash : c'est le cas où vibe 3.1.4 demande une
        # approbation (le mode de session auto-approuve l'édition d'outils et les
        # commandes de lecture). Le défaut cancel doit répondre proprement.
        reports.append(scenario_live(
            mod, "cancel_write", None, "cancelled_required",
            prompt="Crée le fichier /tmp/hva-m2/bash-test.txt contenant 'cancelled?' en "
                   "utilisant EXCLUSIVEMENT bash (par exemple "
                   "`printf 'cancelled?' > /tmp/hva-m2/bash-test.txt`). N'utilise PAS "
                   "l'outil d'édition de fichiers, seulement bash."))

    ok = all(r.get("ok") for r in reports)
    print(json.dumps({"ok": ok, "scenarios": reports}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
