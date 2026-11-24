# Gate de review M3 — builder / reviewer / hook post_agent

## Mécanique réelle vérifiée (vibe 3.1.4)

- **Config** (`$VIBE_HOME/config.toml`, installée par `scripts/install-vibe-config.sh`
  depuis les templates `assets/vibe/`, ids de modèles fournis par l'env
  `VIBE_ACP_MODEL` / `VIBE_REVIEW_MODEL`) : `[[providers]]`/`[[models]]`,
  `active_model`, `default_agent`, `default_approval_policy`, `enable_telemetry`.
- **Builder** : agent principal (`default_agent = "builder"`,
  `$VIBE_HOME/agents/builder.toml`) ; ses `instructions` portent le protocole de
  fin de tâche (review obligatoire, y compris l'attente de la fin du subagent).
- **Reviewer** : `$VIBE_HOME/agents/reviewer.toml` avec `agent_type = "subagent"`
  → exposé au parent comme type de subagent (`vibe/app_server/_agent_types.py`),
  lancé via l'outil de spawn (`task`/`agents.spawn`). Lecture seule :
  `enabled_tools = ["read_file", "grep"]` → le ceiling (`rust_agent_tool_ceiling`)
  dénie write/edit/bash/subagents à l'enfant. Il n'existe pas d'outil `ls` autonome
  en 3.1.4 (`ls` est une commande bash) ; l'exploration passe par read_file/grep.
- **Gate** : `$VIBE_HOME/hooks.toml`, hook `post_agent`, `match = "main"`
  (l'agent racine, jamais les subagents). stdin = JSON `{hook_event_name,
  session_id, transcript_path, cwd, agent_name, turn_id, response_text}` ;
  réponse = exit 0 + stdout `{"decision": "allow"|"deny", "reason", …}`.
  `deny` ⇒ le harness demande une révision (retry, budget **3 par tour**,
  `_MAX_POST_AGENT_RETRIES`, puis acceptation forcée).

## Pitfalls vérifiés dans le code 3.1.4

1. **`strict = true` est interdit sur `post_agent`** (`STRICT_HOOK_TYPES` dans
   `vibe/core/hooks/models.py` : pre_tool/post_tool/pre_agent/pre_llm/post_llm).
   Un hook post_agent marqué strict est **invalidé et ignoré** par le loader.
2. **post_agent ne peut pas échouer en blocking** : toute erreur d'exécution du
   hook (exit≠0, timeout, stdout non-JSON) fait **accepter** le tour.
   Le fail-closed est donc porté par `hooks/revgate-hook.py` : toute exception
   interne produit `deny` avec exit 0 ; le script ne sort jamais en non-zéro.
3. Après 3 retries d'un même tour, le tour est **forcé** : le gate bloque la
   conclusion mais ne peut pas wedge une session. Un modèle peut donc épuiser le
   budget en refusant d'appeler le reviewer (cas testé) ; un durcissement
   éventuel passe par un hook `post_llm` strict ou un contrôle hors vibe.

## Utilisation

```bash
VIBE_HOME=… VIBE_ACP_MODEL=<model-id> VIBE_REVIEW_MODEL=<model-id> \
  scripts/install-vibe-config.sh          # idempotent (validation TOML incluse)
scripts/revgate-init.sh "critère 1" "critère 2"   # (ré)arm le gate (REVGATE_DIR, défaut /tmp/revgate)
VIBE_ACP_BIN=<vibe-acp> MISTRAL_API_KEY=*** python3 tests/e2e_review_gate.py --scenario A
# scenarios : A (hostile, gate fermée) | A2 (coopératif, chemin complet) | B (VIBE_FAST=1)
```

Preuves : `$REVGATE_DIR/hook.log` (décisions horodatées), `review-verdict.json`
(sha256 des criteria + checks par critère + verdict), frames ACP brutes
(`tests/frames-m3-*.jsonl`, hors git).

## Escape-hatch

`VIBE_FAST=1` dans l'environnement de `vibe-acp` (donc du hook) ⇒ allow immédiat,
0 appel reviewer. Pour les one-shots où le review n'est pas voulu.

## Limite connue

Le verdict est écrit par le parent (le reviewer read-only ne peut pas écrire) :
le hook vérifie fraîcheur, couverture et hash des criteria, pas l'identité de
l'auteur du JSON. Contrôle procédural, pas une sécurité cryptographique.
