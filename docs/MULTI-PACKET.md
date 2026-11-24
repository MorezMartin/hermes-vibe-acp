# Multi-paquets : plan parallèle builder + workers + reviewers

Comment un plan découpé en paquets est construit **en parallèle** puis passé par la
gate de review, avec un workspace partagé sans conflit.

## Rôles — qui décide quoi

| Décision | Qui | Nature |
|---|---|---|
| Découpage du plan en paquets indépendants, un `criteria-<slug>.md` par paquet | Architecte (agent planifieur) | intellectuel |
| Dépendances entre paquets → séquencer ou paralléliser | Architecte | intellectuel |
| Ordre de revue et acceptation finale (hash + verdict + relecture des fichiers) | Architecte | intellectuel |
| Créer les worktrees/branches, spawner les workers, attendre, fusionner, lancer les reviewers | Builder (agent principal vibe) | mécanique |
| Exiger que **tous** les verdicts existent, sont frais, et portent le hash des criteria | Hook `revgate` | **algorithmique** |
| Implémentation d'un paquet dans son worktree, commits sur `pkg/<slug>` | Worker (subagent) | intellectuel local |
| Vérification ligne à ligne des critères, JSON de verdict | Reviewer (subagent lecture seule) | intellectuel local |

Le seul verrou *algorithmique* est la gate : sans un verdict approuvé **lié par
sha256** à chaque criteria, l'agent principal ne peut pas conclure (deny en boucle,
puis retries plafonnés). Tout le reste est de la discrétion des agents, cadrée par
leurs instructions.

## Contrat de fichiers (`REVGATE_DIR`)

```
REVGATE_DIR/
├── criteria.md                    # mode mono-paquet (existant)
├── review-verdict.json
├── criteria-<slug>.md             # mode multi-paquets : 1 fichier par paquet
├── review-verdict-<slug>.json     #   + son verdict, obligatoire pour chacun
└── hook.log                       # journal des décisions (visualisation)
```

Règles du hook (`hooks/revgate-hook.py`) :
- Aucun criteria → gate inactive (les tâches triviales passent).
- `criteria-<slug>.md` présent → exige `review-verdict-<slug>.json` dont :
  `criteria_sha256` == sha256 actuel du fichier, `verdict == "approved"`,
  et chaque ligne non vide du criteria présente dans `checks` avec `pass: true`.
- Modifier un criteria après coup **périme son verdict** (hash) → re-review forcée.
- La gate ne s'ouvre que lorsque **tous** les paquets sont approuvés.

## Déroulé type (workspace partagé multi-agents)

```bash
# — Architecte : écrit N criteria-<slug>.md dans REVGATE_DIR, consigne les sha256.

# — Builder (principal) : pour chaque paquet, un worktree + une branche dédiés.
git -C <repo> worktree add <base>-worktrees/<slug> -b pkg/<slug>
# spawn worker(slug, worktree, criteria) × N  — en parallèle
# ... chaque worker livre : PACKAGED/BRANCH/COMMIT/TESTS/NOTES

# — Builder : fusion séquentielle dans le checkout principal, tests globaux.
git -C <repo> merge --ff-only pkg/<slug>   # ou rebase, au choix du builder

# — Builder : un reviewer par paquet (parallèle, lecture seule), verdict copié
#   mot pour mot dans review-verdict-<slug>.json. La gate vérifie le tout.

# — Architecte : vérifie hashes + verdicts, relit les fichiers réels, accepte.

# — Nettoyage des worktrees fusionnés :
git -C <repo> worktree remove <base>-worktrees/<slug>
```

## Pourquoi des worktrees sur un repo partagé

Un même dépôt ne peut pas avoir deux branches différentes checkoutées au même
endroit : sans worktrees, deux agents qui se partagent le répertoire se marchent
sur l'index git, leurs locks se télescopent (et sur certains filesystems réseau,
les locks git cassent — stocker le `.git` ou les worktrees sur un disque local
rapide le règle). Avec un worktree par paquet :

- chaque worker a **son propre index, sa propre HEAD** → zéro conflit ;
- le point de fusion unique (le checkout principal) reste séquentiel et traçable ;
- les autres agents de l'écosystème (planification, revue) lisent le checkout
  principal, qui ne bouge qu'aux fusions ;
- `git worktree list` donne l'état du parallélisme en une commande.

Exemple concret de criteria pour un paquet :

```markdown
- le module <base>/notify.py existe et expose send(msg) -> bool
- send() retourne False et journalise si le transport échoue (test: notify_test.py)
- aucune dépendance ajoutée hors requirements.txt
```

## Échappatoires et garde-fous

- `VIBE_FAST=1` dans l'environnement du hook court-circuite la gate (usage
  humain ponctuel, jamais dans un flux delegué).
- La concurrence des subagents est bornée par `max_concurrent_subagents`
  (défaut 32) — pour un forfait à quota, quelques paquets simultanés suffisent.
- Le hook est **fail-closed** : toute exception interne produit un deny ;
  ne jamais le faire sortir sur un code retour ≠ 0 (le harness interprète
  une erreur comme une acceptation).
