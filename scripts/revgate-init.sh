#!/usr/bin/env bash
# Initialise (ou réinitialise) le gate de review M3 dans REVGATE_DIR (défaut /tmp/revgate).
# Usage :
#   scripts/revgate-init.sh                       # criteria par défaut (démo hello.txt)
#   scripts/revgate-init.sh "critère ligne 1" "critère ligne 2"
#   CRITERIA_FILE=/chemin/criteria.md scripts/revgate-init.sh
# Effets : écrit criteria.md, supprime review-verdict.json, (re)crée le marqueur
# review-verdict.json.stale → tout verdict plus ancien est refusé par le hook.
set -euo pipefail
GATE_DIR="${REVGATE_DIR:-/tmp/revgate}"
mkdir -p "$GATE_DIR"

if [ -n "${CRITERIA_FILE:-}" ]; then
  cp "$CRITERIA_FILE" "$GATE_DIR/criteria.md"
elif [ "$#" -gt 0 ]; then
  printf '%s\n' "$@" > "$GATE_DIR/criteria.md"
else
  printf '%s\n' "le fichier /tmp/revgate/hello.txt existe et contient le mot salut" > "$GATE_DIR/criteria.md"
fi

rm -f "$GATE_DIR/review-verdict.json"
date -u +%Y-%m-%dT%H:%M:%SZ > "$GATE_DIR/review-verdict.json.stale"
echo "revgate initialisé dans $GATE_DIR :"
sed 's/^/  - /' "$GATE_DIR/criteria.md"
