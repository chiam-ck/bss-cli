# Runbook — LiteLLM proxy in front of the BSS LLM calls

Since 2026-06-06 (compose change `9a35104`) every BSS LLM call — REPL,
cockpit browser, customer chat — goes through a **LiteLLM proxy** that
lives in the *separate* `~/agentic` compose project:

```
host CLI / REPL      → http://localhost:4000/v1      ┐
portal containers    → http://litellm-proxy:4000/v1  ├→ litellm-config.yaml routes
                                                     ┘   (OpenRouter / native APIs)
```

`BSS_LLM_API_KEY` in `.env` is the **proxy key**, not a provider key.
Provider keys live in `~/agentic/litellm-config.yaml`.

## Model naming — the part that bites

`BSS_LLM_MODEL` must name a **route in litellm-config.yaml**, and
LiteLLM treats the first path segment as a *provider prefix*:

- `openrouter/google/gemma-4-31b-it` → OpenRouter (correct for BSS:
  OpenRouter accepts our dotted tool names like `customer.get`).
- `deepseek/deepseek-v4-pro` → **DeepSeek native API**, which rejects
  dotted tool names (`Invalid 'tools[0].function.name'`, pattern
  `^[a-zA-Z0-9_-]+$`) — every tool-call turn 400s. This is exactly what
  broke the cockpit between 2026-06-06 and 2026-06-10.

If you ever point `BSS_LLM_BASE_URL` straight back at
`https://openrouter.ai/api/v1`, drop the `openrouter/` prefix — the
OpenRouter slug is `google/gemma-4-31b-it`.

Current setting (2026-06-10): `BSS_LLM_MODEL=openrouter/google/gemma-4-31b-it`.

## Network attachment — re-attach after proxy recreate

The proxy belongs to the `agentic` compose project; the BSS portals can
only resolve `litellm-proxy` because the container is **manually
attached** to the BSS network:

```sh
docker network connect bss-cli_bss litellm-proxy
```

**This attachment is lost every time the litellm container is
recreated** (`docker compose up` in ~/agentic, image bump, etc.) and
the failure mode is silent from the proxy's side: portals log
`openai.APIConnectionError: Connection error` and the cockpit shows
"Sorry — something went wrong" on every turn. Re-run the command above.

Verify from inside a portal:

```sh
docker exec bss-cli-portal-csr-1 python -c \
  "import urllib.request; print(urllib.request.urlopen('http://litellm-proxy:4000/health/liveliness', timeout=5).status)"
```

## End-to-end smoke (one tool-call turn)

```sh
SES=$(curl -s -X POST localhost:9002/cockpit/new -o /dev/null -w '%{redirect_url}' | xargs basename)
curl -s -X POST "localhost:9002/cockpit/$SES/turn" -d "message=show me customer CUST-..." -o /dev/null
timeout 120 curl -s -N "localhost:9002/cockpit/$SES/events" | grep -c "chat-tool-pill"   # expect ≥1
```

A `chat-bubble-error` instead means: check this runbook top-to-bottom —
network attach first, then model route, then `docker logs litellm-proxy`.
