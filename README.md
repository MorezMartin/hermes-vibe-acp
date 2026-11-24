# hermes-vibe-acp

Plugin [Hermes](https://hermes-agent.nousresearch.com) **model-provider** qui utilise
**Mistral Vibe CLI** comme cerveau de code via l'**ACP** : Hermes orchestre
(sessions, mémoire, prompts), Vibe exécute (édition, grep, skills, hooks), et un
gate de review optionnel force une relecture lecture-seule avant de conclure.

Invariable : Hermes ↔ ACP ↔ Vibe. Modèles, chemins, clés = variables d'env
(voir le tableau dans [SPEC.md](SPEC.md)).

## Setup rapide

```bash
# 1. Runtime vibe (venv persistant + symlinks) — F = racine persistante
F=$HOME/.local/share/hermes-vibe-acp bash scripts/bootstrap.sh

# 2. Config vibe headless (remplace les placeholders par vos modeles)
export VIBE_ACP_MODEL=<modele-economique> VIBE_REVIEW_MODEL=<modele-de-review>
export MISTRAL_API_KEY=<cle-api>
bash scripts/install-vibe-config.sh

# 3. Enregistrer le provider dans Hermes
cp -r . $HERMES_HOME/plugins/model-providers/vibe-acp/
hermes -p <profil> --provider vibe-acp --model <modele-economique> -z "ping"
```

Détails : [docs/SETUP.md](docs/SETUP.md) · Gate de review : [docs/REVIEW-GATE.md](docs/REVIEW-GATE.md) · Multi-paquets/parallelisme : [docs/MULTI-PACKET.md](docs/MULTI-PACKET.md) ·
Spécification & pièges connus : [SPEC.md](SPEC.md)

## Tests

```bash
python tests/acp_probe.py            # sonde handshake ACP (sans appel facture)
python tests/e2e_tools.py            # M2 outillage/approvals sur stub ACP (sans appel facture)
python tests/e2e_tools.py --live     # idem sur le vrai vibe-acp (appels facturés)
python tests/e2e_vibe_acp.py         # E2E mono-tour (1 appel facture)
python tests/e2e_review_gate.py --scenario B   # gate court-circuite (VIBE_FAST)
```

## Licence

MIT
