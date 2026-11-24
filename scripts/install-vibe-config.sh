#!/usr/bin/env bash
# Installe (idempotent) la config vibe M3 du repo dans $VIBE_HOME.
# Variables d'environnement (aucun chemin/id interne en dur dans le repo) :
#   VIBE_HOME          dossier de config vibe (REQUIS ; défaut vibe : $HOME/.vibe)
#   VIBE_ACP_MODEL     id du modèle économique, défaut de session (REQUIS)
#   VIBE_REVIEW_MODEL  id du second modèle exposé au picker (REQUIS)
#   VIBE_REVIEW_AGENT_MODEL  id utilisé par l'agent reviewer (défaut : VIBE_ACP_MODEL)
# Copie : config.toml, agents/{builder,reviewer}.toml, hooks/revgate-hook.py ;
# hooks.toml avec les placeholders remplacés par les valeurs d'env.
# Pas de redémarrage requis : vibe relit config/hooks/agents à chaque session.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${VIBE_HOME:?"VIBE_HOME requis (dossier de config vibe)"}"
: "${VIBE_ACP_MODEL:?"VIBE_ACP_MODEL requis (modèle économique, défaut)"}"
: "${VIBE_REVIEW_MODEL:?"VIBE_REVIEW_MODEL requis (modèle reviewer)"}"
SRC="$REPO/assets/vibe"

[ -d "$VIBE_HOME" ] || { echo "VIBE_HOME introuvable : $VIBE_HOME" >&2; exit 1; }
mkdir -p "$VIBE_HOME/agents" "$VIBE_HOME/hooks"

RV_MODEL="${VIBE_REVIEW_AGENT_MODEL:-$VIBE_ACP_MODEL}"
sub() { sed -e "s|__VIBE_HOME__|$VIBE_HOME|g" \
           -e "s|__VIBE_ACP_MODEL__|$VIBE_ACP_MODEL|g" \
           -e "s|__VIBE_REVIEW_MODEL__|$VIBE_REVIEW_MODEL|g" \
           -e "s|__VIBE_REVIEW_AGENT_MODEL__|$RV_MODEL|g" "$1"; }

sub "$SRC/config.toml" > "$VIBE_HOME/config.toml"
sub "$SRC/agents/builder.toml" > "$VIBE_HOME/agents/builder.toml"
sub "$SRC/agents/reviewer.toml" > "$VIBE_HOME/agents/reviewer.toml"
sub "$SRC/agents/worker.toml" > "$VIBE_HOME/agents/worker.toml"
cp "$SRC/hooks/revgate-hook.py" "$VIBE_HOME/hooks/revgate-hook.py"
sub "$SRC/hooks.toml" > "$VIBE_HOME/hooks.toml"

# Validation TOML locale (tomllib, comme vibe) — un dépôt invalide est visible ici,
# pas au premier session/new.
python3 - "$VIBE_HOME" <<'PY'
import sys, tomllib
root = sys.argv[1]
for f in (f"{root}/config.toml", f"{root}/hooks.toml",
          f"{root}/agents/builder.toml", f"{root}/agents/reviewer.toml",
          f"{root}/agents/worker.toml"):
    with open(f, "rb") as fh:
        tomllib.load(fh)
    print("TOML OK :", f)
PY
echo "Config vibe M3 installée dans $VIBE_HOME"
