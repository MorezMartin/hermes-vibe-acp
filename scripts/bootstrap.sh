#!/usr/bin/env bash
# bootstrap.sh — bootstrap idempotent du runtime vibe-acp (M0) + installation du plugin (M1).
#
# Utilisable par le boot s6 du profil Hermes cible (ou un cron de vérif). Ne
# touche JAMAIS au config.yaml de ce profil. Exit non-zero au premier échec.
#
# Variables surchargeables :
#   F                    racine persistante (REQUISE — env de l'installation)
#   PROFILE              nom du profil Hermes qui consomme vibe (REQUIS)
#   VIBE_VENV            chemin du venv mistral-vibe
#   VIBE_PINNED_VERSION  version épinglée pour la reconstruction
#   VIBE_HOME            dossier de config vibe headless
set -euo pipefail

F="${F:?F (racine persistante) doit être défini}"
PROFILE="${PROFILE:?PROFILE (profil Hermes cible) doit être défini}"
VENV="${VIBE_VENV:-$F/profiles/$PROFILE/opt/venv-vibe}"
PINNED_VERSION="${VIBE_PINNED_VERSION:-3.1.4}"
BIN="$F/bin"
PROFILE_BIN="$F/profiles/$PROFILE/bin"
export VIBE_HOME="${VIBE_HOME:-$F/profiles/$PROFILE/.vibe}"

fail() { echo "bootstrap: ERREUR: $*" >&2; exit 1; }
log()  { echo "bootstrap: $*"; }

# ── 1. venv + `vibe --version` ───────────────────────────────────────────────
# Les shebangs du venv pointent le python embarqué de l'image
# (/opt/hermes/tools/python-<ver daté>) : un bump de toolchain les casse.
# Signe : le binaire existe mais ne s'exécute plus → on recrée le venv.
VIBE_BIN="$VENV/bin/vibe"
[ -x "$VIBE_BIN" ] || fail "venv manquant: $VIBE_BIN (recréer : python3 -m venv $VENV && pip install mistral-vibe==$PINNED_VERSION)"
if ! "$VIBE_BIN" --version >/dev/null 2>&1; then
  log "vibe --version échoue (shebangs cassés ?) → reconstruction du venv (~2-3 min sur FUSE)"
  command -v python3 >/dev/null || fail "python3 introuvable pour recréer le venv"
  python3 -m venv --clear "$VENV"
  "$VENV/bin/python" -m pip install --no-input --upgrade pip
  "$VENV/bin/python" -m pip install --no-input "mistral-vibe==$PINNED_VERSION"
  "$VIBE_BIN" --version >/dev/null || fail "venv reconstruit mais vibe --version échoue toujours"
fi
log "OK vibe : $("$VIBE_BIN" --version 2>&1)"

# ── 2. symlinks PATH (créés/réparés s'ils manquent ou sont cassés) ──────────
for bindir in "$BIN" "$PROFILE_BIN"; do
  mkdir -p "$bindir"
  for exe in vibe vibe-acp vibe-app-server; do
    target="$VENV/bin/$exe"
    link="$bindir/$exe"
    [ -f "$target" ] || fail "binaire manquant dans le venv : $target"
    if [ -L "$link" ] && [ -x "$link" ] && [ "$(readlink -f "$link")" = "$(readlink -f "$target")" ]; then
      continue  # déjà bon
    fi
    rm -f "$link"
    ln -s "$target" "$link"
    log "symlink : $link -> $target"
    [ -x "$link" ] || fail "symlink $link non fonctionnel"
  done
done

# ── 3. $VIBE_HOME + config.toml minimal (télémétrie off) ────────────────────
mkdir -p "$VIBE_HOME"
CONFIG="$VIBE_HOME/config.toml"
if [ ! -f "$CONFIG" ]; then
  cat > "$CONFIG" <<'TOML'
# Config vibe minimale — hermes-vibe-acp bootstrap (headless, jamais `vibe --setup`).
# Champ vérifié contre le schéma vibe 3.1.4 :
#   site-packages/vibe/core/config/vibe_schema.py → enable_telemetry: bool = True
# (et vibe/core/config/_telemetry_off_only.py : une config de profil peut éteindre
# la télémétrie, jamais l'allumer — donc `false` ici est respecté).
enable_telemetry = false
TOML
  log "créé : $CONFIG"
else
  log "existe : $CONFIG (non écrasée)"
fi

# ── 4. clé : MISTRAL_API_KEY de l'environnement → $VIBE_HOME/.env si présente ─
# La clé n'est JAMAIS affichée. Si l'env ne la porte pas, l'E2E l'injecte via le shell.
ENVF="$VIBE_HOME/.env"
if [ -n "${MISTRAL_API_KEY:-}" ]; then
  ( umask 077; touch "$ENVF" )
  chmod 600 "$ENVF" 2>/dev/null || true
  if grep -q '^MISTRAL_API_KEY=' "$ENVF" 2>/dev/null; then
    log "$ENVF contient déjà MISTRAL_API_KEY (conservée)"
  else
    printf 'MISTRAL_API_KEY=%s\n' "$MISTRAL_API_KEY" >> "$ENVF"
    log "MISTRAL_API_KEY copiée depuis l'env vers $ENVF (jamais affichée)"
  fi
else
  log "pas de MISTRAL_API_KEY dans l'env → $ENVF non créée (E2E l'injectera)"
fi

# ── 5. smoke : vibe-acp --help via le PATH $F/bin ───────────────────────────
PATH="$BIN:$PATH" vibe-acp --help >/dev/null 2>&1 || fail "vibe-acp --help échoue via $BIN"
log "OK vibe-acp résolu via $BIN"

# ── 6. installation du plugin (idempotent) dans chaque home qui le scanne ────
# $F/plugins/model-providers        : home HERMES_HOME à la racine F
# $F/profiles/$PROFILE/plugins/…    : profil cible côté F
# $D/profiles/$PROFILE/plugins/…    : profil cible côté $D (racine live des
#                                     process HERMES_HOME=/opt/data ; sync unidirectionnel
#                                     FUSE→ext4 possible → on y installe aussi)
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
D="${D:-/opt/data}"
if [ -f "$REPO/plugin.yaml" ] && [ -f "$REPO/__init__.py" ]; then
  for base in "$F" "$F/profiles/$PROFILE" "$D/profiles/$PROFILE"; do
    DEST="$base/plugins/model-providers/vibe-acp"
    mkdir -p "$DEST"
    for f in plugin.yaml __init__.py; do
      if ! cmp -s "$REPO/$f" "$DEST/$f" 2>/dev/null; then
        cp "$REPO/$f" "$DEST/$f"
        log "plugin installé : $DEST/$f"
      fi
    done
  done
else
  log "ATTENTION : plugin.yaml/__init__.py absents de $REPO — plugin non installé"
fi

log "bootstrap terminé."
