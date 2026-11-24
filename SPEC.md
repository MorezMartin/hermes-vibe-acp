# SPEC — hermes-vibe-acp

Plugin Hermes (model-provider) qui pilote **Mistral Vibe CLI** comme cerveau de code
via l'**ACP** (Agent Client Protocol, stdio JSON-RPC).

Invariants : **Hermes** (orchestrateur) ↔ **ACP** (transport) ↔ **Vibe** (exécuteur).
Tout le reste est configurable : modèles, chemins, politique de review.

## Architecture

```
Hermes profile                      vibe-acp (subprocess stdio)
  provider `vibe-acp`  ─ JSON-RPC ─►  ACP agent = Vibe CLI
  - System prompt injecte                - agents builder/reviewer (TOML)
    dans le bloc System ACP              - hooks.toml (gate de review)
  - outils agent-level bridges           - skills / MCP eventuels
  - picker modeles <- configOptions      - auth par env (cle API)
```

## Interface de configuration (variables d'environnement)

| Variable | Défaut | Rôle |
|---|---|---|
| `VIBE_ACP_BIN` | `vibe-acp` | binaire ACP lancé en subprocess |
| `VIBE_ACP_COMMAND` / `VIBE_ACP_ARGS` | — | override complet commande/args |
| `VIBE_HOME` | `~/.vibe` | dossier de config Vibe (key: `$VIBE_HOME/.env`) |
| `MISTRAL_API_KEY` | — | clé API (celle du subprocess fait foi) |
| `VIBE_ACP_MODEL` | — | id du modèle économique (défaut des sessions) |
| `VIBE_REVIEW_MODEL` | — | id du modèle de review |
| `VIBE_ACP_TOOLS` | allowlist agent-level | outils Hermes exposés à vibe via le bridge texte : `all`, `none` ou liste csv (`memory,todo,…`) ; jamais bash par défaut (vibe a son propre shell) |
| `VIBE_ACP_APPROVAL` | `cancel` | politique sur `session/request_permission` entrant : `cancel` (répond cancelled, ne hang jamais), `allowlist` (via `VIBE_ACP_APPROVAL_ALLOWLIST`), `allow` (allow_once d'abord) |
| `VIBE_ACP_APPROVAL_ALLOWLIST` | `read,search,fetch` | jetons comparés au kind/title de l'évènement suivi et aux patterns d'invocation `_meta.required_permissions` |
| `VIBE_ACP_DISCOVER_TIMEOUT` | `60` | plafond (s) du catalogue live du picker (`configOptions` via `session/new`, sans inférence) |
| `VIBE_VENV` / `VIBE_PINNED_VERSION` | — | cible du bootstrap d'install |
| `VIBE_FAST` | unset | `1` = court-circuite le gate de review |
| `F` | requis | racine persistante pour `scripts/bootstrap.sh` |

Aucun nom de modèle, chemin d'installation ou choix d'infra n'est codé en dur :
les templates `assets/vibe/` utilisent des placeholders `__VIBE_*__` résolus par
`scripts/install-vibe-config.sh`.

## Configuration Vibe fournie (templates)

- `assets/vibe/config.toml` : provider custom + deux modèles (éco / review) via
  `[[providers]]`/`[[models]]`, `enable_telemetry=false`.
- `assets/vibe/agents/builder.toml` : agent principal (écriture).
- `assets/vibe/agents/reviewer.toml` : subagent **lecture seule**
  (`enabled_tools = [read_file, grep]`).
- `assets/vibe/hooks.toml` + `assets/vibe/hooks/revgate-hook.py` : gate de review
  `post_agent` (voir `docs/REVIEW-GATE.md`).
- `scripts/revgate-init.sh` : initialise le dossier de critères/verdicts.

## Milestones & critères d'acceptation (périmètre plugin)

- **M1 — Provider & mono-tour** ✅ : plugin `kind: model-provider`
  (`external_process`, `base_url: acp://vibe`) enregistré et visible au picker ;
  prompt mono-tour via ACP répond ; sans clé → erreur explicite citant
  `-32000 Missing API key` en ~5 s (pas de hang). `tests/e2e_vibe_acp.py`.
- **M2 — Pont outillage & sessions** ✅ : allowlist d'outils agent-level
  forwarding vers vibe (`VIBE_ACP_TOOLS`, défaut = mémoire/todo/skills, jamais
  bash — vibe a son propre shell) ; projection des `tool_call`/`tool_call_update`
  entrants en lignes d'historique « déjà accomplies »
  (`hermes_projected_messages` + compteur d'itérations, pattern junie/codex) ;
  politique d'approbations `VIBE_ACP_APPROVAL` = `cancel` (défaut headless :
  répond `cancelled`, ne hang jamais) | `allowlist` | `allow` ; mapping
  `configOptions` (configId `model`) → picker via `discover_models` (live, sans
  inférence), repli `fallback_models` env, et `session/set_config_option` appliqué
  quand `--model` passe un id connu de vibe. `tests/e2e_tools.py` (stub hors-ligne
  + scénario réel `--live`).
- **M3 — Templates builder/reviewer + gate** ✅ : config + agents + hooks livrés en
  templates, validés par le chargeur vibe (`load_hooks_file`, `AgentProfile.from_toml`),
  test ACP : `VIBE_FAST=1` passe sans review ; sans verdict, le hook dénie et force
  la review (limite : 3 retries/turn, voir pièges). `tests/e2e_review_gate.py`.
- **M4 — Doc & packaging** (partiel) : README/SETUP/REVIEW-GATE ✅ ; CI + tests
  auto sur matrix de versions vibe = à faire.
- **M5 — Compat** (à faire) : test contre ≥2 versions mineures de vibe ; politique
  d'upgrading documentée.

## Faits vérifiés & pièges (vibe 3.1.4, ACP)

- Framing : JSON-RPC newline-delimited sur stdio (pas de headers Content-Length).
- `initialize` réussit **sans clé** ; `session/new` sans clé → `-32000 Missing API
  key for mistral provider` et le **process survit** (ne jamais attendre sa mort).
- `authMethods` n'annonce que `browser-auth` (inadapté headless) → la voie headless
  est `MISTRAL_API_KEY` (env du subprocess ou `$VIBE_HOME/.env`).
- Sélection de modèle : uniquement `session/set_config_option` `configId="model"`.
- Hooks : stdin JSON `{hook_event_name, session_id, transcript_path, cwd,
  agent_name, turn_id, response_text}` ; réponse exit 0 + `{decision, reason,
  system_message}`.
- **`strict=true` est refusé sur `post_agent`** par le loader → fail-closed porté
  par le script du hook (toute exception interne = deny, jamais exit≠0 qui ferait
  *accepter*).
- `post_agent` ne peut pas bloquer indéfiniment : **3 retries/tour** puis
  acceptation forcée (`_MAX_POST_AGENT_RETRIES`). Un durcissement anti-contournement
  passe par un hook `post_llm`.
- Pas d'outil `ls` en 3.1.4 (bash-only) → reviewer limité à `read_file`+`grep`.
- Cold start sur stockage réseau lent : timeout `initialize` ≥ 30 s recommandé.
- Le plugin cible Hermes >= 0.23.3 (`ACPClient` générique, seam `create_client`
  pour providers ACP externes).

## Layout

```
plugin.yaml  __init__.py          # provider Hermes (M1)
scripts/bootstrap.sh              # install runtime idempotente (venv + symlinks)
scripts/install-vibe-config.sh    # resout les placeholders __VIBE_*__ -> $VIBE_HOME
scripts/revgate-init.sh           # init criteria/verdicts du gate
assets/vibe/                      # templates config vibe (agents, hooks)
docs/REVIEW-GATE.md               # contrat du gate
tests/acp_probe.py                # sonde ACP stdlib (no deps)
tests/stub_vibe_agent.py          # faux agent ACP (preuves M2 hors-ligne, no deps)
tests/e2e_vibe_acp.py             # E2E mono-tour
tests/e2e_tools.py                # E2E M2 : outillage/approvals/projection (stub + --live)
tests/e2e_review_gate.py          # E2E gate builder/reviewer
```
