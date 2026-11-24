#!/usr/bin/env python3
"""Hook post_agent « revgate » — gate de review force pour hermes-vibe-acp.

Contrat vérifié vibe 3.1.4 (mistralai_vibe_local_harness/vibe/_foreign_hooks.py) :
- stdin : JSON {hook_event_name, session_id, transcript_path, cwd,
  parent_session_id, agent_name, turn_id, response_text}
- sortie : exit 0 + stdout JSON {"decision": "allow"|"deny", "reason", "system_message"}.
  decision=deny ⇒ le harness redemande une révision à l'agent (retry, budget 3/turn
  via _MAX_POST_AGENT_RETRIES, puis acceptation forcée).
- PIÈGE : une erreur d'exécution (exit != 0, timeout, stdout invalide) fait ACCEPTER
  le tour ("post_agent — always accepts the turn"). Le fail-closed est donc porté par
  ce script : toute exception interne produit un DENY avec exit 0. Ne jamais
  remonter un exit != 0. `strict=true` est interdit sur post_agent (le loader
  invaliderait le hook entier).

Deux modes :
- PAQUET UNIQUE : $REVGATE_DIR/criteria.md + review-verdict.json (comportement M3).
- MULTI-PAQUETS : $REVGATE_DIR/criteria-<slug>.md, un par paquet de plan ;
  chacun exige son verdict review-verdict-<slug>.json, frais et lié par hash.
  La gate n'ouvre que si TOUS les paquets sont approuvés.

Échappatoire : VIBE_FAST=1 dans l'environnement du hook ⇒ allow immédiat.
Inactif si aucun criteria n'existe dans $REVGATE_DIR (défaut /tmp/revgate).
"""
import hashlib
import json
import os
import sys
import time
from pathlib import Path

GATE_DIR = Path(os.environ.get("REVGATE_DIR", "/tmp/revgate"))
LOG = GATE_DIR / "hook.log"


def _log(line: str) -> None:
    try:
        GATE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
    except OSError:
        pass


def _emit(decision: str, reason: str, note: str = "") -> None:
    """Une seule décision sur stdout, exit 0 toujours (fail-closed par construction)."""
    _log(f"{decision}: {reason[:300]}")
    print(json.dumps(
        {"decision": decision, "reason": reason, "system_message": note or reason},
        ensure_ascii=False,
    ))


def _criteria_lines(path: Path) -> list[str]:
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def _packages() -> list[tuple[Path, Path]]:
    """(criteria, verdict) à satisfaire : mode mono, puis mode multi-paquets."""
    pairs: list[tuple[Path, Path]] = []
    mono = GATE_DIR / "criteria.md"
    if mono.is_file():
        pairs.append((mono, GATE_DIR / "review-verdict.json"))
    for crit in sorted(GATE_DIR.glob("criteria-*.md")):
        slug = crit.name[len("criteria-"):-len(".md")]
        pairs.append((crit, GATE_DIR / f"review-verdict-{slug}.json"))
    return pairs


def _deny_reason(crit: Path, verdict: Path, criteria_hash: str) -> str:
    return (
        "GATE REVIEW ACTIVE — ne conclus pas. Le verdict de review est absent, "
        "stale, ou ne couvre pas le critère. Procédure obligatoire : "
        "1) lance le sous-agent reviewer (outil task avec agent=\"reviewer\", ou "
        "agents.spawn type \"reviewer\") avec cette consigne exacte : « Vérifie "
        f"chaque ligne de {crit} contre l'état réel des fichiers ; "
        f"criteria_sha256_literal={criteria_hash} ; réponds UNIQUEMENT avec le "
        "JSON de verdict attendu. » "
        f"2) écris le JSON renvoyé par le reviewer MOT POUR MOT dans {verdict} ; "
        "3) réponds de nouveau. N'écris jamais un verdict toi-même."
    )


def _check_package(crit: Path, verdict: Path) -> str | None:
    """None = paquet approuvé ; sinon la raison du deny (auto-portée)."""
    criteria_hash = hashlib.sha256(crit.read_bytes()).hexdigest()
    stale = verdict.with_suffix(verdict.suffix + ".stale")
    if not verdict.is_file():
        return _deny_reason(crit, verdict, criteria_hash)
    try:
        if stale.exists() and verdict.stat().st_mtime < stale.stat().st_mtime:
            return _deny_reason(crit, verdict, criteria_hash)
        data = json.loads(verdict.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _deny_reason(crit, verdict, criteria_hash) + f" [verdict illisible: {exc}]"
    if data.get("criteria_sha256") != criteria_hash:
        return _deny_reason(crit, verdict, criteria_hash) + \
            " [verdict ne porte pas le hash des criteria actuels]"
    if data.get("verdict") != "approved":
        return (
            f"Le reviewer a rendu un verdict 'rejected' pour {crit.name}. "
            "Corrige les points signalés, relance le reviewer, puis réécris le verdict."
        )
    checks = {str(c.get("criterion", "")).strip(): bool(c.get("pass"))
              for c in data.get("checks", []) if isinstance(c, dict)}
    missing = [c for c in _criteria_lines(crit) if c not in checks]
    failed = [c for c, ok in checks.items() if not ok]
    if missing or failed:
        return _deny_reason(crit, verdict, criteria_hash) + (
            f" [criteria non couverts: {missing[:3]}]" if missing else
            f" [checks en échec: {failed[:3]}]"
        )
    return None


def evaluate(payload: dict) -> tuple[str, str]:
    event = payload.get("hook_event_name", "?")
    if os.environ.get("VIBE_FAST") == "1":
        return "allow", f"VIBE_FAST=1 — gate court-circuitée ({event})"
    packages = _packages()
    if not packages:
        return "allow", f"gate inactive — aucun criteria dans {GATE_DIR} ({event})"
    for crit, verdict in packages:
        reason = _check_package(crit, verdict)
        if reason:
            tag = f"[{crit.name}] " if crit.name != "criteria.md" else ""
            return "deny", tag + reason
    return "allow", f"review approuvé et à jour ({len(packages)} paquet(s))"


def main() -> None:
    raw = ""
    try:
        raw = sys.stdin.read()
    except OSError:
        pass
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        decision, reason = evaluate(payload)
    except Exception as exc:  # fail-closed : toute erreur interne ⇒ deny
        decision, reason = "deny", (
            f"[erreur hook: {exc!r}] — ne conclus pas, réessaie la procédure reviewer."
        )
    _emit(decision, reason, note=(
        f"[revgate] session={payload.get('session_id')} cwd={payload.get('cwd')}"
    ))


if __name__ == "__main__":
    main()
