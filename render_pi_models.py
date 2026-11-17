"""Render pi's `models.json` from the environment, at container start.

The previous version of this file was a checked-in JSON with one provider, one base URL and two model ids that
belonged to our cluster. Copied into someone else's installation it would have pointed mcbot at a service that
does not exist there - and, because `pi` resolves the provider from `--provider`, a wrong provider name is not a
loud failure either; it is a model that never answers.

So the shape lives here instead, and the values come from `config_table`:

- `MCBOT_PI_BASE_URL`  required. It is always somebody's endpoint; the repository does not have one to offer.
- `MCBOT_PI_API_KEY_ENV` the *name* of the variable holding the key. pi expands `$NAME` in `apiKey`, so the key
  itself never passes through this program, a log, or a file.
- `PI_PROVIDER` / `PI_MODEL` the names `pi` is later given on the command line, from the same source, so the
  provider in the JSON and the provider in the arguments cannot disagree. That disagreement is the failure mode
  this file exists to remove.
- context window, max tokens, reasoning: properties of the model rather than of the deployment, so they have
  defaults, and defaults that a llama.cpp server of almost any shape accepts.

Run it directly to see what a given set of environment variables produces:

    MCBOT_PI_BASE_URL=http://myhost:8080/v1 PI_MODEL=my-model python3 render_pi_models.py
"""

from __future__ import annotations

import json
import os
import sys

import config_table as cfg


def _setting(name: str):
    return cfg.value_of(name)


def build() -> dict:
    provider = _setting("PI_PROVIDER") or "local"
    base_url = _setting("MCBOT_PI_BASE_URL")
    key_env = _setting("MCBOT_PI_API_KEY_ENV") or "MCBOT_PI_API_KEY"
    context_window = int(_setting("MCBOT_PI_CONTEXT_WINDOW"))
    max_tokens = int(_setting("MCBOT_PI_MAX_TOKENS"))
    reasoning = bool(_setting("MCBOT_PI_REASONING"))

    # `PI_MODEL` may name several ids; pi is given one of them with --model and only needs the rest to exist in
    # the catalogue so a person can switch without editing the image.
    ids = _setting("PI_MODEL") or ("auto",)
    if isinstance(ids, str):
        ids = (ids,)

    models = []
    seen: set[str] = set()
    for mid in ids:
        mid = str(mid).strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        models.append({
            "id": mid,
            "name": f"{mid} ({provider})",
            "reasoning": reasoning,
            "thinkingLevelMap": {"off": "off", "low": "low", "medium": "medium", "high": "xhigh"},
            "contextWindow": context_window,
            "maxTokens": max_tokens,
            "input": ["text"],
        })
    if "auto" not in seen:
        # mcbot's own default model id is `auto`, meaning "whatever this provider fronts". Keeping the id valid
        # costs nothing and means PI_MODEL can name a specific model without the default id becoming invalid.
        models.append({
            "id": "auto",
            "name": f"auto ({provider}, provider picks the backend)",
            "reasoning": reasoning,
            "thinkingLevelMap": {"off": "off", "low": "low", "medium": "medium", "high": "xhigh"},
            "contextWindow": context_window,
            "maxTokens": max_tokens,
            "input": ["text"],
        })

    return {
        "providers": {
            provider: {
                "baseUrl": base_url,
                "api": "openai-completions",
                # A reference, not a value: pi reads the environment variable named here when it makes the call.
                "apiKey": f"${key_env}",
                "compat": {
                    "supportsDeveloperRole": True,
                    "supportsReasoningEffort": True,
                    "sendSessionAffinityHeaders": True,
                    "sessionAffinityFormat": "openai",
                },
                "models": models,
            }
        }
    }


def main() -> int:
    if not (os.environ.get("MCBOT_PI_BASE_URL", "").strip() or cfg.entry("MCBOT_PI_BASE_URL").default):
        print("render_pi_models: MCBOT_PI_BASE_URL is required; there is no default base URL to offer",
              file=sys.stderr)
        return 1
    print(json.dumps(build(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
