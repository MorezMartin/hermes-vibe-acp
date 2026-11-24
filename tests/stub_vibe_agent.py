#!/usr/bin/env python3
"""Faux agent ACP (stdlib only) pour les tests hors-ligne du plugin vibe-acp.

Même fil que vibe 3.1.4 sur stdio (newline-delimited JSON-RPC), sans clé ni appel
facturé. Sur session/prompt il émet : tool_call (in_progress, rawInput echoing the
received prompt), session/request_permission (allow_once / allow_always avec
_meta.required_permissions.type), puis attend la réponse du client, tool_call_update
(completed) et agent_message_chunk. Chaque session process écrit ses preuves
(prompt reçu, réponses d'approbation) en JSON-lines dans $STUB_REPORT.
"""
import json
import os
import sys
from pathlib import Path

REPORT = Path(os.environ.get("STUB_REPORT", "stub_report.jsonl"))
SESSION_ID = "stub-session-1"


def emit(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def record(obj):
    with REPORT.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


def update(update_obj):
    emit({"jsonrpc": "2.0", "method": "session/update",
          "params": {"sessionId": SESSION_ID, "update": update_obj}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, rid = msg.get("method"), msg.get("id")
        if method == "initialize":
            emit({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": 1,
                "agentCapabilities": {"loadSession": False},
                "authMethods": []}})
        elif method == "session/new":
            emit({"jsonrpc": "2.0", "id": rid, "result": {
                "sessionId": SESSION_ID,
                "configOptions": [{"id": "model", "name": "Model", "category": "model",
                                   "type": "select", "currentValue": "stub-model-eco",
                                   "options": [{"value": "stub-model-eco", "name": "Stub Eco"},
                                               {"value": "stub-model-b", "name": "Stub B"}]}]}})
        elif method == "session/set_config_option":
            record({"set_config_option": msg.get("params")})
            emit({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "session/prompt":
            text = ((msg.get("params") or {}).get("prompt") or [{}])[0].get("text", "")
            record({"prompt": text})
            update({"sessionUpdate": "tool_call", "toolCallId": "stub-1",
                    "title": "stub: write hello.txt", "kind": "execute",
                    "status": "in_progress", "rawInput": {"prompt_sha": len(text)}})
            emit({"jsonrpc": "2.0", "id": 9001, "method": "session/request_permission",
                  "params": {"sessionId": SESSION_ID, "toolCall": {"toolCallId": "stub-1"},
                             "options": [
                                 {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                                 {"optionId": "allow_always", "name": "Allow for remainder",
                                  "kind": "allow_always",
                                  "_meta": {"required_permissions": [
                                      {"scope": "command_pattern",
                                       "invocation_pattern": "dangerous-command --wipe-all"}]}}]}})
            # Attend la réponse du client (le test échoue par timeout si le client hang).
            for reply in sys.stdin:
                try:
                    rsp = json.loads(reply)
                except json.JSONDecodeError:
                    continue
                if rsp.get("id") == 9001:
                    record({"permission_response": rsp.get("result") or rsp.get("error")})
                    break
            update({"sessionUpdate": "tool_call_update", "toolCallId": "stub-1",
                    "kind": "execute", "status": "completed"})
            update({"sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "stub: fait"}})
            emit({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
