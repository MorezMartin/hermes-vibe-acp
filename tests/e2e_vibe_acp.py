#!/usr/bin/env python3
"""E2E provider du plugin vibe-acp (M1) — sans dépendre du gateway Hermes.

Le CLI `hermes -z` passe par le gateway du profil (attach/daemon). Sur un host
où le gateway du profil n'est pas supervisé, ce test exerce EXACTEMENT le même
chemin transport que le runtime :

  1. découverte du plugin ($HERMES_HOME/plugins/model-providers/vibe-acp)
  2. hermes_cli.auth.resolve_external_process_provider_credentials('vibe-acp')
     → la résolution réelle des creds external_process (command/args/base_url)
  3. ProviderProfile.create_client(...) → ACPClient vibe
  4. chat.completions.create(model=..., messages=[...]) → prompt ACP facturé

Modes :
  avec clé : MISTRAL_API_KEY dans l'env (ou $VIBE_HOME/.env) → doit répondre PONG
  sans clé : retirer les deux → doit échouer vite (< ~35 s) avec
             « Missing API key for mistral provider » (erreur -32000, pas un hang)

Ne loggue jamais la clé. Sortie : JSON {ok, mode, elapsed_s, response|error}.
"""
import json
import os
import sys
import time

sys.path.insert(0, "/opt/hermes")

MODEL = os.environ.get("E2E_MODEL", "")  # id modèle propre à l'installation
PROMPT = "Réponds UNIQUEMENT: PONG"


def main() -> int:
    from providers import get_provider_profile
    from hermes_cli.auth import resolve_external_process_provider_credentials

    profile = get_provider_profile("vibe-acp")
    if profile is None:
        print(json.dumps({"ok": False, "error": "provider 'vibe-acp' non enregistré (plugin non découvert)"}))
        return 1
    creds = resolve_external_process_provider_credentials("vibe-acp")
    client = profile.create_client(api_key=creds["api_key"], base_url=creds["base_url"],
                                   command=creds["command"], args=creds["args"])
    mode = "avec_clé" if (os.environ.get("MISTRAL_API_KEY") or
                          os.path.isfile(os.path.join(os.environ.get("VIBE_HOME", ""), ".env"))) else "sans_clé"
    t0 = time.time()
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": PROMPT}],
        )
        text = (resp.choices[0].message.content or "").strip()
        out = {"ok": text == "PONG", "mode": mode, "elapsed_s": round(time.time() - t0, 1),
               "response": text[:400]}
    except Exception as exc:  # noqa: BLE001 — rapporte l'erreur, sans jamais loguer l'env
        msg = str(exc)[:400]
        out = {"ok": "Missing API key" in msg, "mode": mode,
               "elapsed_s": round(time.time() - t0, 1), "error": msg}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
