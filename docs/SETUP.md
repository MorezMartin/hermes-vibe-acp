# SETUP

## Prérequis

- Hermes >= 0.23.3 (client ACP générique `ACPClient`).
- Python 3.12+ pour le venv du runtime vibe.
- Une clé API Mistral (Studio) : la **clé API classique** fonctionne dans les
  harnais tiers ; les clés d'abonnement Vibe sont restreintes à l'CLI officiel.

## 1. Installer le runtime vibe

```bash
export F=$HOME/.local/share/hermes-vibe-acp   # tout ce qui doit persister
export VIBE_PINNED_VERSION=3.1.4
bash scripts/bootstrap.sh
```

Idempotent : recrée le venv (`$VIBE_VENV`), répare les symlinks, crée
`$VIBE_HOME/config.toml`. À brancher dans ton mécanisme de boot (systemd unit,
entrypoint container, cron de vérification).

## 2. Configurer vibe (headless)

```bash
export VIBE_HOME=$F/.vibe
export VIBE_ACP_MODEL=<id-modele-economique>
export VIBE_REVIEW_MODEL=<id-modele-de-review>
export MISTRAL_API_KEY=***           # ou via $VIBE_HOME/.env
bash scripts/install-vibe-config.sh
```

`install-vibe-config.sh` résout les placeholders `__VIBE_*__` des templates
`assets/vibe/` vers `$VIBE_HOME/`. Aucun navigateur requis : l'ACP lit la clé
depuis l'environnement du subprocess ou `$VIBE_HOME/.env`.

## 3. Enregistrer le provider dans Hermes

Copier ce dossier dans `$HERMES_HOME/plugins/model-providers/vibe-acp/`
(ou `$HERMES_HOME/profiles/<profil>/plugins/model-providers/vibe-acp/` par profil).
Le provider apparaît au picker (`--provider vibe-acp`).

Variables consommées par le plugin : `VIBE_ACP_BIN` (ou `VIBE_ACP_COMMAND` +
`VIBE_ACP_ARGS`), `VIBE_HOME`, `MISTRAL_API_KEY` ; pont outillage/approvals :
`VIBE_ACP_TOOLS`, `VIBE_ACP_APPROVAL`, `VIBE_ACP_APPROVAL_ALLOWLIST`,
`VIBE_ACP_DISCOVER_TIMEOUT` (tableau complet dans SPEC.md).

Politique d'approbation (headless) : par défaut le plugin répond `cancelled` à
toute demande `session/request_permission` de vibe — un outil risqué ne s'exécute
jamais sans avis explicite, et le tour se clôt proprement (jamais de hang).
Pour laisser vibe travailler sans supervision : `VIBE_ACP_APPROVAL=allow`, ou
`allowlist` avec des jetons ciblés (`VIBE_ACP_APPROVAL_ALLOWLIST=read,search`).

## 4. Gate de review (optionnel)

```bash
bash scripts/revgate-init.sh        # cree le dossier criteria/verdicts
# edite criteria.md, lance vibe via Hermes ; VIBE_FAST=1 pour court-circuiter
```

Contrat complet et limites connues : docs/REVIEW-GATE.md et la section
« Faits vérifiés & pièges » de SPEC.md.
