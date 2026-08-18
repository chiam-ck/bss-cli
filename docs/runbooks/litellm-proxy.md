# Runbook — BSS LLM calls (OpenRouter direct)

Since 2026-08-18 every BSS LLM call — REPL, cockpit browser, customer
chat — goes **directly to OpenRouter**. The LiteLLM proxy was
decommissioned (cost tracking off, peak/off-peak flipper removed,
container + DB dropped). No proxy, no network attach, no model-route
table.

```
host CLI / REPL      → https://openrouter.ai/api/v1
portal containers    → https://openrouter.ai/api/v1
```

`BSS_LLM_API_KEY` in `.env` is the OpenRouter key (shared homelab key,
same as Hermes/other apps). Model id is the bare OpenRouter slug:

- `BSS_LLM_MODEL=google/gemma-4-31b-it` (no `openrouter/` prefix —
  that prefix was a LiteLLM route artifact, and OpenRouter direct
  rejects it).

## Smoke test

```sh
curl -s https://openrouter.ai/api/v1/chat/completions \
  -H "Authorization: Bearer $BSS_LLM_API_KEY" \
  -d '{"model":"google/gemma-4-31b-it","messages":[{"role":"user","content":"hi"}],"max_tokens":10}'
```

## History (why the docs said proxy)

Before 2026-06-06 → 2026-08-18, calls went through a LiteLLM proxy in
`~/agentic`. `openrouter/`-prefixed model names and the
`docker network connect bss-cli_bss litellm-proxy` dance were artifacts
of that setup. Both are gone; do not reintroduce them.
