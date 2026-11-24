"""Mistral Vibe CLI comme model-provider Hermes via ACP — M1 (mono-tour) + M2 (outillage).

Contrat 0.23.3 (codé contre /opt/hermes installé, pas contre une source périmée) :
- ``auth_type="external_process"`` : Hermes lance ``vibe-acp`` (stdio, ACP JSON-RPC
  newline-delimited). La clé appartient à vibe (env ``MISTRAL_API_KEY`` ou
  ``$VIBE_HOME/.env``) — rien n'est écrit dans auth.json.
- ``base_url="acp://vibe"`` : le marqueur ``acp://`` force le routage ACP
  (pas de streaming, pas de Responses) — voir agent/agent_init.py.
- ``ProviderProfile.create_client`` est la couture hors-arbre pour un transport
  non-HTTP (agent_runtime_helpers._provider_supplied_client) : on y renvoie le
  client ACP générique ``agent.acp_client.ACPClient`` avec le plancher de timeout
  pour le cold start sur stockage lent.

M2 — pont outillage, approvals, mapping modèles (pattern « junie » vérifié sur le
runtime réel) :
- OUTILLAGE entrant : vibe est un agent autonome qui exécute SES outils (édition,
  bash…) et les annonce en notifications ACP ``tool_call``/``tool_call_update``.
  Elles sont projetées en lignes d'historique « déjà accomplies »
  (``hermes_projected_messages`` + ``hermes_provider_tool_iterations``, consommés
  par agent/provider_projection.py) — JAMAIS en tool_calls pendants, sinon Hermes
  refait un travail fini.
- OUTILLAGE sortant : seuls les outils Hermes SANS équivalent vibe (mémoire,
  todo, skills) traversent le bridge texte ``agent/acp_openai_bridge`` ;
  l'allowlist est réglée par ``VIBE_ACP_TOOLS`` (défaut prudent, jamais bash).
- APPROVALS : ``session/request_permission`` entrant → ``VIBE_ACP_APPROVAL`` =
  ``cancel`` (défaut : répond cancelled — ne hang jamais) | ``allowlist``
  (kind/title de l'évènement suivi + patterns d'invocation ``_meta`` comparés à
  ``VIBE_ACP_APPROVAL_ALLOWLIST``) | ``allow`` (allow_once d'abord, allow_always
  seulement en repli).
- MODÈLES : ``session/new`` annonce ses modèles via ``configOptions`` (id/category
  « model ») ; ``discover_models`` les expose au picker Hermes (sinon repli
  ``fallback_models`` depuis l'env) ; un ``--model`` connu de vibe est appliqué
  pour de vrai par ``session/set_config_option`` (géré par ACPClient._run_prompt) ;
  ``VIBE_ACP_MODEL`` sert de défaut quand aucun modèle n'est demandé.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import threading
from types import SimpleNamespace
from typing import Any, Iterable

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

#: VIBE_HOME posé si l'opérateur n'en a pas défini (bootstrap.sh crée ce dossier).
#: Chemin propre à l'installation : le fournir via l'environnement VIBE_HOME.
DEFAULT_VIBE_HOME = os.environ.get("VIBE_HOME", "")

#: Cold start de vibe-acp sur stockage réseau lent ≈ 16 s pour ``initialize``
#: (references/runtime-vibe-acp.md §3). ACPClient applique le timeout à la session
#: entière (initialize + session/new + prompt) : on le planche à 30 s.
MIN_ACP_TIMEOUT_S = 30.0

#: Surpasses opérateur pour pointer un autre binaire/argv sans toucher au code
#: (champ réel 0.23.3 : process_command_env_vars / process_args_env_var).
COMMAND_ENV_VARS = ("HERMES_VIBE_ACP_COMMAND",)
ARGS_ENV_VAR = "HERMES_VIBE_ACP_ARGS"

#: Outils Hermes sans équivalent côté vibe (boucle d'auto-amélioration mémoire/
#: skills + todo). Vibe a son propre shell/édition/fouille : re-proposer les
#: outils Hermes qui les doublent ferait refaire à Hermes un travail déjà fini.
DEFAULT_FORWARDED_TOOLS = ("memory", "todo", "skill_manage", "skill_view", "skills_list")

#: Préfixe des noms d'outils projetés (vibe_execute, vibe_read, …) : une ligne
#: projetée ne doit jamais être prise pour un outil Hermes à dispatcher.
PROJECTED_TOOL_PREFIX = "vibe_"
_PROJECTED_RESULT_MAX_CHARS = 4000
_PROJECTED_EVENT_LIMIT = 40

#: Politique d'approbation (env VIBE_ACP_APPROVAL) et allowlist associée
#: (env VIBE_ACP_APPROVAL_ALLOWLIST, csv minuscule). Le défaut est prudent :
#: on ne répond « selected » qu'avec une correspondance explicite ou allow forcé.
_APPROVAL_CANCEL, _APPROVAL_ALLOWLIST, _APPROVAL_ALLOW = "cancel", "allowlist", "allow"
DEFAULT_APPROVAL_ALLOWLIST = ("read", "search", "fetch")
_ALLOW_OPTION_KINDS = ("allow_once", "allow_always")


def route_cli_billing() -> None:
    """Le CLI vibe facture sur le forfait Vibe, jamais l'API Studio.

    L'environnement d'un hôte peut porter ``MISTRAL_API_KEY`` = clé API (Studio) :
    le process ACP l'hériterait et consommerait le mauvais forfait. Si une clé
    dédiée CLI est présente (env ``VIBE_ACP_KEY_ENV``, défaut
    ``MISTRAL_VIBE_API_KEY``), on la fait passer pour ``MISTRAL_API_KEY`` dans
    TOUT futur spawn ACP (patch idempotent de ``_build_subprocess_env``, la fabrique
    d'env du client ACP générique). Appelé depuis ``create_client`` : jamais pendant
    la découverte des providers (agent.* n'est pas encore importable).
    """
    if getattr(route_cli_billing, "_done", False):  # type: ignore[attr-defined]
        return
    key_name = os.environ.get("VIBE_ACP_KEY_ENV", "MISTRAL_VIBE_API_KEY").strip()
    cli_key = os.environ.get(key_name, "").strip()
    if not cli_key or cli_key == os.environ.get("MISTRAL_API_KEY", ""):
        route_cli_billing._done = True  # type: ignore[attr-defined]
        return
    try:
        import agent.acp_client as acp
    except Exception:
        return
    original = getattr(acp, "_build_subprocess_env", None)
    if original is None or getattr(original, "_vibe_billing", False):
        return

    def _env_with_cli_billing():
        env = original()
        if cli_key:
            env["MISTRAL_API_KEY"] = cli_key
        return env

    _env_with_cli_billing._vibe_billing = True  # type: ignore[attr-defined]
    acp._build_subprocess_env = _env_with_cli_billing
    route_cli_billing._done = True  # type: ignore[attr-defined]
    logger.info("vibe-acp: facturation CLI route vers la clé %s (forfait Vibe).", key_name)


def _csv_env(name: str, default: tuple[str, ...]) -> frozenset[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return frozenset(default)
    return frozenset(p.strip().lower() for p in raw.replace(",", " ").split() if p.strip())


def resolve_forwarded_tools() -> frozenset[str] | None:
    """Quels outils Hermes sont décrits à vibe via le bridge texte (VIBE_ACP_TOOLS).

    ``all``/``*`` = tout le toolset Hermes (retourne None, pas de filtrage) ;
    ``none``/``off`` = rien ; sinon liste csv exacte. Défaut : l'allowlist
    agent-level (mémoire/todo/skills) — jamais bash/fichiers, déjà couverts par vibe.
    """
    raw = os.environ.get("VIBE_ACP_TOOLS", "").strip().lower()
    if not raw:
        return frozenset(DEFAULT_FORWARDED_TOOLS)
    if raw in ("all", "*"):
        return None
    if raw in ("none", "off", "-"):
        return frozenset()
    return frozenset(p.strip() for p in raw.replace(",", " ").split() if p.strip())


def approval_policy() -> str:
    """Politique d'approbation ``VIBE_ACP_APPROVAL`` : cancel (défaut) | allowlist | allow.

    Toute valeur inconnue retombe sur ``cancel`` : le mode headless ne doit jamais
    approuver par accident, et jamais ne hang (une réponse cancelled clôture le tour).
    """
    raw = os.environ.get("VIBE_ACP_APPROVAL", "").strip().lower()
    if raw in (_APPROVAL_ALLOW, "allow_once", "yes", "1", "true"):
        return _APPROVAL_ALLOW
    if raw in (_APPROVAL_ALLOWLIST, "whitelist"):
        return _APPROVAL_ALLOWLIST
    if raw not in ("", _APPROVAL_CANCEL, "deny"):
        logger.warning("VIBE_ACP_APPROVAL=%r inconnu — politique cancel appliquée.", raw)
    return _APPROVAL_CANCEL


def _filter_forwarded_tools(tools: list[dict[str, Any]] | None, allowed: frozenset[str] | None):
    if allowed is None or not tools:
        return tools
    kept = []
    for t in tools:
        fn = t.get("function") if isinstance(t, dict) else None
        name = str((fn or {}).get("name") or "").strip()
        if name and name in allowed:
            kept.append(t)
    return kept


def _truncate(text: str, limit: int = _PROJECTED_RESULT_MAX_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n… [tronqué, {len(text) - limit} caractères de plus]"


def _tool_update_text(update: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in update.get("content") or []:
        if not isinstance(block, dict):
            continue
        inner = block.get("content")
        if isinstance(inner, dict) and isinstance(inner.get("text"), str):
            parts.append(inner["text"])
        elif isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(p for p in parts if p.strip()).strip()


def _merge_tool_event(store: dict[str, dict[str, Any]], update: dict[str, Any]) -> None:
    """Fusionne tool_call / tool_call_update (même toolCallId) → état final ; le dernier champ non vide gagne."""
    tcid = str(update.get("toolCallId") or f"tool_{len(store)}")
    entry = store.setdefault(tcid, {"id": tcid})
    for field in ("title", "kind", "status"):
        if update.get(field):
            entry[field] = update[field]
    if (text := _tool_update_text(update)):
        entry["result"] = text
    if update.get("locations"):
        entry["locations"] = update["locations"]
    for field in ("rawInput", "rawOutput"):
        if update.get(field):
            entry[field] = update[field]


def _projected_tool_name(kind: Any) -> str:
    slug = "".join(c if c.isalnum() or c == "_" else "_" for c in str(kind or "other").strip().lower())
    return f"{PROJECTED_TOOL_PREFIX}{slug or 'other'}"


def _projected_call_id(session_id: str | None, tool_call_id: str, index: int) -> str:
    base = tool_call_id or f"idx{index}"
    if session_id:
        return f"{PROJECTED_TOOL_PREFIX}{session_id}_{base}"[:120]
    digest = hashlib.sha256(f"{base}|{index}".encode()).hexdigest()[:16]
    return f"{PROJECTED_TOOL_PREFIX}{digest}"


def project_tool_events(tool_events: dict[str, dict[str, Any]], session_id: str | None = None) -> list[dict[str, Any]]:
    """Projette l'activité outil native de vibe en paires assistant(tool_calls)+tool(résultat) achevés.

    Lignes d'historique uniquement (pattern junie / codex event projector) : épongées
    dans ``messages`` par agent/provider_projection.py, jamais rejouées par Hermes.
    """
    projected: list[dict[str, Any]] = []
    for index, ev in enumerate(tool_events.values()):
        if index >= _PROJECTED_EVENT_LIMIT:
            logger.info("vibe-acp : projection d'activité outil tronquée (%d évènements gardés)", _PROJECTED_EVENT_LIMIT)
            break
        call_id = _projected_call_id(session_id, str(ev.get("id") or ""), index)
        name = _projected_tool_name(ev.get("kind"))
        args: dict[str, Any] = {}
        if ev.get("title"):
            args["title"] = str(ev["title"])
        raw_input = ev.get("rawInput")
        if isinstance(raw_input, dict):
            args.update(raw_input)
        elif raw_input:
            args["input"] = raw_input
        if ev.get("locations"):
            args["locations"] = ev["locations"]
        try:
            arguments = json.dumps(args, ensure_ascii=False, default=str)
        except Exception:
            arguments = json.dumps({"title": str(ev.get("title") or "")}, ensure_ascii=False)

        result = ev.get("result")
        if not result and ev.get("rawOutput"):
            try:
                result = json.dumps(ev["rawOutput"], ensure_ascii=False, default=str)
            except Exception:
                result = str(ev["rawOutput"])
        status = str(ev.get("status") or "").strip()
        content = _truncate(str(result or "").strip())
        if status and status not in ("completed", "success"):
            content = f"[status {status}]\n{content}".strip()
        if not content:
            content = f"[status {status or 'inconnu'}] (aucune sortie rapportée)"

        projected.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}]})
        projected.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": content})
    return projected


def _choose_allow_option(options: list[dict[str, Any]]) -> str | None:
    """optionId à approuver : allow_once d'abord, allow_always seulement si aucun one-shot."""
    opts = [o for o in options if isinstance(o, dict)]
    for kind in _ALLOW_OPTION_KINDS:
        for opt in opts:
            if str(opt.get("kind", "")).lower() == kind and (opt.get("optionId") or opt.get("id")):
                return str(opt.get("optionId") or opt.get("id"))
    return None


def _permission_match_keys(params: dict[str, Any], options: list[dict[str, Any]],
                           tracked: dict[str, dict[str, Any]]) -> set[str]:
    """Tokens comparables d'une demande d'approbation : kind/title de l'évènement
    suivi (même toolCallId) + patterns d'invocation annoncés dans ``_meta.required_permissions``."""
    keys: set[str] = set()
    tcid = str((params.get("toolCall") or {}).get("toolCallId") or "")
    if (event := tracked.get(tcid)):
        for field in ("kind", "title"):
            if event.get(field):
                keys.add(str(event[field]).strip().lower())
    for opt in options:
        meta = opt.get("_meta") if isinstance(opt, dict) else None
        for perm in (meta or {}).get("required_permissions") or []:
            for field in ("invocation_pattern", "session_pattern"):
                pattern = str((perm or {}).get(field) or "").strip().lower()
                if pattern:
                    keys.add(pattern)
    return keys


def _allowlist_matches(keys: set[str], allowlist: Iterable[str]) -> bool:
    """kind exact dans l'allowlist, ou jeton de l'allowlist trouvé en sous-chaîne d'un titre/pattern."""
    allowed = {a.strip().lower() for a in allowlist if a.strip()}
    for key in keys:
        if key in allowed:
            return True
        for token in allowed:
            if len(token) > 2 and token in key:
                return True
    return False


class _TurnState:
    """État d'un tour, porté en thread-local : un tour = un appel synchrone
    create() → _run_prompt → boucle _handle_server_message, tout sur le même thread."""

    def __init__(self) -> None:
        self.tool_events: dict[str, dict[str, Any]] = {}
        self.approvals: list[dict[str, Any]] = []
        self.session_id: str = ""


class VibeACPProfile(ProviderProfile):
    """Mistral Vibe v3.x piloté en ACP (external process) — M1 mono-tour + M2."""

    def create_client(self, **client_kwargs: Any) -> Any:
        route_cli_billing()
        # vibe respecte VIBE_HOME (vibe/utils/vibe_home.py) et lit $VIBE_HOME/.env ;
        # ne pas décraser une valeur déjà posée par l'opérateur ou le wrapper de profil.
        os.environ.setdefault("VIBE_HOME", DEFAULT_VIBE_HOME)
        client_kwargs.setdefault("command", "vibe-acp")
        client_kwargs.setdefault("base_url", "acp://vibe")
        return _vibe_client_class()(**client_kwargs)

    def discover_models(self, **kwargs: Any) -> list[dict[str, Any]] | None:
        """Catalogue live du picker : les ``configOptions`` (configId « model ») annoncés
        par ``session/new`` de vibe — sans aucun appel d'inférence. ``None`` = repli sur
        ``fallback_models`` (VIBE_ACP_MODEL / VIBE_REVIEW_MODEL depuis l'env)."""
        try:
            from agent.acp_client import _model_config_option

            timeout = float(os.environ.get("VIBE_ACP_DISCOVER_TIMEOUT", "60"))
            command = (os.environ.get("HERMES_VIBE_ACP_COMMAND") or os.environ.get("VIBE_ACP_BIN")
                       or "vibe-acp").strip()
            client = self.create_client(command=command)
            rows: list[dict[str, Any]] = []
            seen: set[str] = set()
            with client._session(timeout, allow_file_requests=False) as (session, _):
                option = _model_config_option(session) or {}
                for entry in option.get("options") or []:
                    if not isinstance(entry, dict):
                        continue
                    value = str(entry.get("value") or "").strip()
                    if not value or value in seen:
                        continue
                    seen.add(value)
                    row: dict[str, Any] = {"id": value, "label": str(entry.get("name") or value)}
                    if entry.get("description"):
                        row["note"] = str(entry["description"])
                    rows.append(row)
            return rows or None
        except Exception as exc:  # pas de clé, binaire absent, cold start > timeout → repli
            logger.debug("vibe-acp discover_models indisponible: %s", exc)
            return None


def _vibe_client_class():
    """Construit la sous-classe d'ACPClient (import différé : plugins/* ne doit pas
    importer agent/* pendant la découverte des providers)."""
    from agent.acp_client import (
        ACPClient,
        _effective_timeout,
        _jsonrpc_result,
        _render_message_content,
        _ROLE_LABELS,
    )
    from agent.acp_openai_bridge import (
        completion_to_stream_chunks as _completion_to_stream_chunks,
        extract_tool_calls_from_text as _extract_tool_calls_from_text,
        render_tool_bridge_sections as _render_tool_bridge_sections,
    )

    # Mot-clé du bridge texte (balise OpenAI reconstituée pour éviter tout
    # littéral XML ambigu dans ce fichier).
    _TC_OPEN, _TC_CLOSE = "<" + "tool_call" + ">", "<" + "/" + "tool_call" + ">"

    # Préambule spécifique vibe : vibe est un agent autonome qui a SES outils ; on ne
    # lui demande de router par la balise texte QUE les outils Hermes qu'il ne peut
    # pas exécuter lui-même (allowlist VIBE_ACP_TOOLS pré-filtrée en amont).
    _PREAMBLE = (
        "You are being used as the active ACP coding agent backend for Hermes.",
        "Use YOUR OWN tools (file edit, bash, search) to complete the task directly.",
        "Hermes owns a few tools of its own that you cannot execute: to use one, emit "
        f"ONLY {_TC_OPEN}{{...}}{_TC_CLOSE} with one JSON object in OpenAI function-call "
        "shape (id/type/function{name,arguments}) and stop — Hermes runs it and sends "
        "you the result; do not redo work you already completed. Keep using your own "
        "tools for reading, editing and running code.",
        "If no Hermes-side tool is needed, answer normally.",
    )

    class VibeACPClient(ACPClient):
        default_command = "vibe-acp"
        default_args: tuple = ()
        command_env_vars = COMMAND_ENV_VARS
        args_env_var = ARGS_ENV_VAR
        marker_base_url = "acp://vibe"
        label = "Vibe ACP"
        # Message additionnel quand vibe répond l'erreur ACP -32000 (auth requise).
        auth_required_hint = (
            "Chemin headless : MISTRAL_API_KEY dans l'environnement du subprocess "
            "ou $VIBE_HOME/.env — la méthode ACP 'browser-auth' (OAuth Studio) exige un navigateur."
        )

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._tl = threading.local()

        # ── M2 : prompt façonné (défaut modèle env + allowlist outillage) ──────
        def _format_prompt(self, messages: list[dict[str, Any]] | None, model: str | None,
                           tools: list[dict[str, Any]] | None, tool_choice: Any) -> str:
            sections: list[str] = [*_PREAMBLE, *_render_tool_bridge_sections(tools, tool_choice)]
            transcript: list[str] = []
            for message in (m for m in messages or [] if isinstance(m, dict)):
                role = str(message.get("role") or "unknown").strip().lower()
                if rendered := _render_message_content(message.get("content")):
                    transcript.append(f"{_ROLE_LABELS.get(role, 'Context')}:\n{rendered}")
            if transcript:
                sections.append("Conversation transcript:\n\n" + "\n\n".join(transcript))
            sections.append("Continue the conversation from the latest user request.")
            return "\n\n".join(s.strip() for s in sections if s and s.strip())

        def _create_chat_completion(self, *, model: str | None = None,
                                    messages: list[dict[str, Any]] | None = None,
                                    timeout: Any = None, tools: list[dict[str, Any]] | None = None,
                                    tool_choice: Any = None, stream: bool = False, **_: Any) -> Any:
            # Cold start stockage lent : ne jamais laisser un timeout < MIN_ACP_TIMEOUT_S
            # tuer initialize (le champ ProviderProfile n'existe pas, on clamp ici).
            timeout_seconds = max(_effective_timeout(timeout), MIN_ACP_TIMEOUT_S)
            # Garde anti-boucle (VIBE_ACP_MAX_TURN_S, défaut 480 s) : un tour qui
            # tourne sans produire est tué par groupe (watchdog armé dans _handle_server_message).
            max_turn = float(os.environ.get("VIBE_ACP_MAX_TURN_S", "480"))
            timeout_seconds = min(timeout_seconds, max_turn) if max_turn > 0 else timeout_seconds
            # Défaut modèle : VIBE_ACP_MODEL quand Hermes ne demande rien (ou envoie le
            # placeholder). Un id demandé passe par session/set_config_option (base).
            requested = str(model or "").strip()
            if not requested or requested == self.placeholder_model:
                requested = os.environ.get("VIBE_ACP_MODEL", "").strip() or self.placeholder_model
            tools = _filter_forwarded_tools(tools, resolve_forwarded_tools())

            state = _TurnState()
            self._tl.vibe_turn = state
            try:
                prompt_text = self._format_prompt(messages, requested, tools, tool_choice)
                response_text, reasoning = self._run_prompt(
                    prompt_text, timeout_seconds=timeout_seconds,
                    model=None if requested == self.placeholder_model else requested)
            finally:
                t = getattr(state, "timer", None)
                if t is not None:
                    t.cancel()
                self._tl.vibe_turn = None

            tool_calls, cleaned_text = _extract_tool_calls_from_text(response_text)
            message = SimpleNamespace(
                content=cleaned_text, tool_calls=tool_calls,
                reasoning=reasoning or None, reasoning_content=reasoning or None,
                reasoning_details=None,
            )
            completion = SimpleNamespace(
                choices=[SimpleNamespace(message=message,
                                         finish_reason="tool_calls" if tool_calls else "stop")],
                usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0,
                                      prompt_tokens_details=SimpleNamespace(cached_tokens=0)),
                model=requested or self.placeholder_model,
                # Lignes d'historique « déjà accomplies » épongées par Hermes
                # (agent/provider_projection.splice_provider_projection).
                hermes_projected_messages=project_tool_events(state.tool_events, state.session_id or None),
                hermes_provider_tool_iterations=sum(
                    1 for ev in state.tool_events.values()
                    if str(ev.get("status")) in ("completed", "success")),
                # Journal local des décisions d'approbation du tour (preuve de test, sans clé).
                vibe_approval_decisions=list(state.approvals),
            )
            return _completion_to_stream_chunks(completion) if stream else completion

        # ── M2 : notifications entrantes + politique d'approbation ─────────────
        def _handle_server_message(self, msg, *, process, cwd, text_parts, reasoning_parts,
                                   allow_file_requests=True):
            method = msg.get("method")
            params = msg.get("params") or {}
            state = getattr(self._tl, "vibe_turn", None)
            if state is not None and process is not None and getattr(state, "proc", None) is None:
                state.proc = process
                limit = float(os.environ.get("VIBE_ACP_MAX_TURN_S", "480"))
                if limit > 0:
                    timer = threading.Timer(limit, self._abort_turn, args=(state, limit))
                    timer.daemon = True
                    timer.start()
                    state.timer = timer

            if method == "session/update":
                if state is not None:
                    if sid := str(params.get("sessionId") or "").strip():
                        state.session_id = sid
                    update = params.get("update") or {}
                    if update.get("sessionUpdate") in ("tool_call", "tool_call_update"):
                        _merge_tool_event(state.tool_events, update)
                # le super consomme agent_message_chunk / agent_thought_chunk
                return super()._handle_server_message(
                    msg, process=process, cwd=cwd, text_parts=text_parts,
                    reasoning_parts=reasoning_parts, allow_file_requests=allow_file_requests)

            if method == "session/request_permission":
                option_id = self._decide_permission(params, state)
                if option_id:
                    response = _jsonrpc_result(msg.get("id"),
                                               {"outcome": {"outcome": "selected", "optionId": option_id}})
                else:
                    # cancel est LA réponse propre headless : vibe enchaîne, jamais de hang.
                    response = _jsonrpc_result(msg.get("id"), {"outcome": {"outcome": "cancelled"}})
                if process.stdin is not None:
                    process.stdin.write(json.dumps(response) + "\n")
                    process.stdin.flush()
                return True

            return super()._handle_server_message(
                msg, process=process, cwd=cwd, text_parts=text_parts,
                reasoning_parts=reasoning_parts, allow_file_requests=allow_file_requests)

        def _abort_turn(self, state: "_TurnState", limit: float) -> None:
            proc = getattr(state, "proc", None)
            if proc is None or proc.poll() is not None:
                return
            logger.warning("vibe-acp: watchdog %ss atteint sans fin de tour — kill du groupe (évènements vus: %d)",
                           int(limit), len(state.tool_events))
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        def _decide_permission(self, params: dict[str, Any], state: "_TurnState | None") -> str | None:
            policy = approval_policy()
            options = [o for o in (params.get("options") or []) if isinstance(o, dict)]
            tracked = state.tool_events if state is not None else {}
            option_id: str | None = None
            if policy == _APPROVAL_ALLOW:
                option_id = _choose_allow_option(options)
            elif policy == _APPROVAL_ALLOWLIST:
                keys = _permission_match_keys(params, options, tracked)
                if _allowlist_matches(keys, _csv_env("VIBE_ACP_APPROVAL_ALLOWLIST", DEFAULT_APPROVAL_ALLOWLIST)):
                    option_id = _choose_allow_option(options)
            record = {"toolCallId": str((params.get("toolCall") or {}).get("toolCallId") or ""),
                      "policy": policy, "outcome": "selected" if option_id else "cancelled",
                      "optionId": option_id}
            logger.info("vibe-acp approval %s (policy=%s) → %s%s", record["toolCallId"], policy,
                        record["outcome"], f" option={option_id}" if option_id else "")
            if state is not None:
                state.approvals.append(record)
            return option_id

    return VibeACPClient


register_provider(
    VibeACPProfile(
        name="vibe-acp",
        display_name="Mistral Vibe (ACP)",
        description="Mistral Vibe CLI comme cerveau de code via ACP (JSON-RPC sur stdio)",
        auth_type="external_process",
        base_url="acp://vibe",
        env_vars=("MISTRAL_API_KEY", "VIBE_HOME"),
        process_command="vibe-acp",
        process_args=(),
        process_command_env_vars=COMMAND_ENV_VARS,
        process_args_env_var=ARGS_ENV_VAR,
        supports_health_check=False,    # pas d'endpoint HTTP derrière acp://
        supports_model_listing=False,   # catalogue live = configOptions vibe (discover_models)
        hidden=False,
        # Repli du picker quand le catalogue live est injoignable : ids propres à
        # chaque installation, fournis par l'environnement (VIBE_ACP_MODEL = défaut
        # économique, VIBE_REVIEW_MODEL = second). Sans ces vars, pas de repli —
        # vibe reste sur son défaut de session.
        fallback_models=tuple(m for m in (os.environ.get("VIBE_ACP_MODEL"),
                                          os.environ.get("VIBE_REVIEW_MODEL")) if m),
        model_aliases={},
    )
)
