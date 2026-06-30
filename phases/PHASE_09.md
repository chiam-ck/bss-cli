# Phase 9 — CLI + LLM Orchestrator + ASCII Renderers

> **The product face.** Everything before this phase is plumbing. This phase is what people see, so it's what breaks or sells the demo. Build the direct CLI first, then the LLM layer, then the renderers. Do not invert this order.
>
> **This is also the phase where the semantic layer gets built.** The LLM can only be as good as the docstrings, type hints, system prompt, and structured errors we give it. If the semantic layer is sloppy, the LLM fabricates IDs and picks the wrong tools. If it's tight, a small cheap model like MiMo v2 Flash can chain 5+ tool calls correctly. Invest in the semantic layer before tuning the model.

## Goal

The `bss` command. Typer CLI, LangGraph orchestrator, ~62 tools, the first set of ASCII renderers, and **a rigorous semantic layer that makes the LLM reliable**. By end of phase:

```
$ bss "create Ck on plan M with card 4242 4242 4242 4242"
$ bss subscription show SUB-007
$ bss case list --customer CUST-007
$ bss ticket show TKT-101
$ bss                 # drops into REPL
```

## Deliverables

### 1. `cli/` — Typer CLI

```
cli/
├── bss_cli/
│   ├── __init__.py
│   ├── main.py              # Typer root
│   ├── config.py            # service URLs, LLM config — uses _REPO_ROOT pattern
│   ├── context.py           # channel/actor injection
│   ├── commands/
│   │   ├── customer.py      # create, list, show, update-contact
│   │   ├── case.py          # open, list, show, note, close
│   │   ├── ticket.py        # open, list, show, assign, ack, start, resolve, close
│   │   ├── catalog.py       # list, show
│   │   ├── payment.py       # add-card (dev tokenizer), list, show
│   │   ├── order.py         # create, list, show, cancel
│   │   ├── som.py           # service list, service show, service-order show
│   │   ├── subscription.py  # show, list, balance, vas, renew, terminate
│   │   ├── usage.py         # simulate, history
│   │   ├── billing.py       # bills, bill show, account
│   │   ├── prov.py          # tasks, resolve, retry, fault
│   │   ├── clock.py         # now, freeze, unfreeze, advance
│   │   ├── trace.py         # events (proxy to audit.domain_event for now)
│   │   ├── scenario.py      # runner stubs (populated in Phase 10)
│   │   ├── admin.py         # reset, force-*, release-msisdn
│   │   └── ask.py           # LLM entry point
│   ├── renderers/
│   │   ├── _utils.py
│   │   ├── subscription.py  # bundle bars, state, countdown (HERO)
│   │   ├── customer.py      # 360 view (HERO)
│   │   ├── case.py          # case with child tickets (HERO)
│   │   ├── ticket.py        # single ticket with history
│   │   ├── catalog.py       # plan comparison table (HERO)
│   │   ├── order.py         # order state + service decomposition tree (HERO)
│   │   └── esim.py          # eSIM activation card with QR ASCII (HERO)
│   └── repl.py              # interactive LLM REPL
├── pyproject.toml
└── README.md
```

Entry point: `bss = bss_cli.main:app`

`config.py` uses the `_REPO_ROOT` pattern from the Phase 3 chore fix so CLI reads `.env` regardless of invocation directory.

### 2. Command shape

Direct commands use explicit subcommands:

```
bss customer create --name Ck --email ck@example.com --card 4242424242424242
bss customer list --state active
bss customer show CUST-007

bss case open --customer CUST-007 --subject "Data not working" --category technical --priority high
bss case list --customer CUST-007
bss case show CASE-042

bss ticket open --case CASE-042 --type service_outage --subject "No data session"
bss ticket assign TKT-101 --agent AGT-004
bss ticket ack TKT-101
bss ticket start TKT-101
bss ticket resolve TKT-101 --notes "HLR re-provisioned; confirmed working"
bss ticket close TKT-101

bss order create --customer CUST-007 --offering PLAN_M
bss order show ORD-014

bss som service list --subscription SUB-007
bss subscription show SUB-007
bss subscription show SUB-007 --show-esim
bss subscription vas SUB-007 VAS_DATA_5GB

bss usage simulate --msisdn 90000005 --type data --quantity 1GB

bss prov tasks --service SVC-333
bss prov resolve PTK-444 --note "HLR manual intervention complete"
bss prov fault HLR_PROVISION fail_first_attempt --enable --probability 0.3

bss clock now
bss clock advance 30d

bss trace events --aggregate subscription --id SUB-007
```

Natural language mode:

```
bss ask "create Ck on plan M with card 4242 4242 4242 4242"
bss ask "show me Ck's bundle"
bss ask "Ck says his data stopped working, open a case and a technical ticket"
bss                 # REPL — persistent context across turns
```

### 3. `orchestrator/` — LangGraph agent

```
orchestrator/
├── bss_orchestrator/
│   ├── __init__.py
│   ├── graph.py             # LangGraph supervisor
│   ├── tools/
│   │   ├── __init__.py      # tool registry
│   │   ├── customer.py
│   │   ├── interaction.py
│   │   ├── case.py
│   │   ├── ticket.py
│   │   ├── catalog.py
│   │   ├── payment.py
│   │   ├── order.py
│   │   ├── som.py
│   │   ├── provisioning.py
│   │   ├── subscription.py
│   │   ├── usage.py
│   │   ├── billing.py
│   │   └── ops.py
│   ├── types.py             # NewType IDs, Literals, Annotated types — the semantic vocabulary
│   ├── config.py            # BSS_LLM_* env vars via pydantic-settings (+ _REPO_ROOT)
│   ├── llm.py               # openai.AsyncOpenAI → OpenRouter (OpenAI-compatible)
│   ├── prompts.py           # SYSTEM_PROMPT + few-shot examples
│   ├── safety.py            # destructive op gating
│   └── session.py           # REPL session state
└── pyproject.toml
```

### 3a. LLM provider — OpenRouter via openai SDK (no LiteLLM)

BSS-CLI uses **OpenRouter directly** via the `openai` SDK rather than LiteLLM. OpenRouter is already a provider aggregator (100+ models exposed through a single OpenAI-compatible endpoint), so adding LiteLLM in front of it would be proxying a proxy for no v0.1 benefit. This decision saves a container (motto #6), removes a moving part, and keeps the orchestrator→model path to one hop.

**Environment variables (in `.env`, 5 vars):**

```bash
# --- LLM (Phase 9) ---
BSS_LLM_BASE_URL=https://openrouter.ai/api/v1
BSS_LLM_MODEL=xiaomi/mimo-v2-flash
BSS_LLM_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxx
BSS_LLM_HTTP_REFERER=https://github.com/chiam-ck/bss-cli
BSS_LLM_APP_NAME=bss-cli
```

The `HTTP-Referer` and `X-Title` headers are OpenRouter-specific attribution headers. Optional for the API to work, recommended for dashboard visibility.

**Default dev model is MiMo v2 Flash** because it's fast, cheap ($0.09/M prompt, $0.29/M completion), has 262K context, and proved clean instruction-following in the Phase 8 pre-flight test. Swap to `anthropic/claude-sonnet-4.6` or similar via `.env` for hero scenarios if tool-calling quality matters. Code never hardcodes a model name — only `.env` does.

**Client construction in `orchestrator/bss_orchestrator/llm.py`:**

```python
from openai import AsyncOpenAI
from bss_orchestrator.config import settings


def get_llm_client() -> AsyncOpenAI:
    """Construct the OpenRouter-backed OpenAI-compatible client."""
    return AsyncOpenAI(
        base_url=settings.BSS_LLM_BASE_URL,
        api_key=settings.BSS_LLM_API_KEY,
        default_headers={
            "HTTP-Referer": settings.BSS_LLM_HTTP_REFERER,
            "X-Title": settings.BSS_LLM_APP_NAME,
        },
    )
```

LangGraph consumes this via `langchain_openai.ChatOpenAI` constructed with the same `base_url` / `api_key` / `default_headers` params. Model identifier is `settings.BSS_LLM_MODEL`.

**Unit test LLM mocking:**

Unit tests for the graph use a deterministic fake LLM — a hand-rolled class implementing the same async `chat.completions.create()` interface as the OpenAI client. Return canned `ChatCompletion` objects with the tool calls each test needs. Do not hit OpenRouter in CI tests. Integration smoke test against real OpenRouter is marked `@pytest.mark.integration` and skipped by default.

**Why not LiteLLM:** considered and rejected. OpenRouter already provides provider aggregation and cost tracking. Adding LiteLLM would be proxying a proxy. ~150 MB RAM, one more container, one more config file, one more thing to debug. Not worth it for v0.1. Reversible in Phase 11+ by swapping `BSS_LLM_BASE_URL` — zero code changes.

### 3b. Semantic layer discipline — the LLM's input surface

**This is the most important section in Phase 9.** The LLM's reliability comes from the quality of its input surface. A cheap fast model with a great semantic layer beats a frontier model with a sloppy one — every time, and at 1/100th the cost.

The BSS-CLI semantic layer consists of **six artifacts**, each contributing a specific kind of knowledge to the LLM. None is optional.

#### (1) System prompt (`prompts.py`)

The LLM's constitution. Tells it what the system is, what the rules are, which workflows exist, and how to behave. See section 6 below for the full prompt.

#### (2) Tool docstrings

Every LangGraph tool has a docstring that becomes part of the tool's schema exposed to the LLM. **This is the LLM's primary reference for picking the right tool.** Every docstring MUST include these four sections:

```python
async def subscription_purchase_vas(
    subscription_id: SubscriptionId,
    vas_offering_id: VasOfferingId,
) -> dict:
    """Purchase a VAS (value-added service) for a subscription and charge the customer's
    default card-on-file. Use this when a customer is blocked due to bundle exhaustion
    and wants to top up, or when they want to add extra allowance to an active subscription.

    Args:
        subscription_id: The subscription to top up, in SUB-NNN format (e.g. SUB-007).
            Get this from subscription.list_for_customer or subscription.get_by_msisdn.
        vas_offering_id: The VAS product offering, in VAS_xxx format (e.g. VAS_DATA_5GB).
            Get this from catalog.list_vas.

    Returns:
        The updated subscription object with new balance. Check the `state` field — if
        the subscription was blocked, it should now be active. Check `balances` for the
        updated allowance.

    Raises:
        PolicyViolationFromServer: if a policy check fails. Common rules:
            - subscription.vas_purchase.requires_active_cof: customer has no valid card
            - subscription.vas_purchase.vas_offering_sellable: the VAS offering is inactive
            - subscription.vas_purchase.not_if_terminated: subscription is terminated
          Read the `rule` field and decide: retry with corrections, or ask the user.
    """
```

**Required docstring sections:**

- **Purpose** — one paragraph. What the tool does AND when to use it. The "when to use it" sentence is critical — it's how the LLM disambiguates between similar tools.
- **Args** — every argument with its ID format (`SUB-NNN`, `CUST-NNN`, `PM-NNNN`, etc.) and where to obtain it from (which other tool produced it).
- **Returns** — the shape of the result AND which fields the LLM should pay attention to.
- **Raises** — expected exceptions with the specific rule IDs the tool can trigger and what each means.

**Docstring anti-patterns Claude Code will produce if not told otherwise** (reject on review):

- ❌ "Purchase a VAS." (too terse, no "when to use")
- ❌ `subscription_id: The subscription ID` (no format, no source)
- ❌ "Returns the updated subscription" (no mention of which fields matter)
- ❌ No `Raises:` section at all, or generic `Raises: Exception`

**The docstring is the LLM's interface. Invest in it.**

#### (3) Type hints — `orchestrator/bss_orchestrator/types.py`

The LLM sees the tool schema generated from your Python type hints. **Stronger types = smaller fabrication surface.** Define a shared vocabulary in `types.py`:

```python
# orchestrator/bss_orchestrator/types.py
from typing import Annotated, Literal, NewType

# ID types — use NewType so the LLM's schema shows the format convention
CustomerId = Annotated[str, "Customer ID in CUST-NNN format, e.g. CUST-007"]
SubscriptionId = Annotated[str, "Subscription ID in SUB-NNN format, e.g. SUB-007"]
OrderId = Annotated[str, "Product Order ID in ORD-NNN format, e.g. ORD-014"]
ServiceOrderId = Annotated[str, "Service Order ID in SO-NNN format, e.g. SO-022"]
ServiceId = Annotated[str, "Service ID in SVC-NNN format, e.g. SVC-033"]
CaseId = Annotated[str, "Case ID in CASE-NNN format, e.g. CASE-042"]
TicketId = Annotated[str, "Ticket ID in TKT-NNN format, e.g. TKT-101"]
PaymentMethodId = Annotated[str, "Payment Method ID in PM-NNNN format, e.g. PM-0042"]
PaymentAttemptId = Annotated[str, "Payment Attempt ID in PAY-NNNNNN format, e.g. PAY-000042"]
AgentId = Annotated[str, "Agent ID in AGT-NNN format, e.g. AGT-004"]
ProductOfferingId = Annotated[str, "Plan offering ID — must be PLAN_S, PLAN_M, or PLAN_L"]
VasOfferingId = Annotated[str, "VAS offering ID, e.g. VAS_DATA_5GB, VAS_DATA_DAYPASS"]
Msisdn = Annotated[str, "Mobile number in 8-digit format, e.g. 90000005"]
Iccid = Annotated[str, "eSIM ICCID, 19-20 digits starting with 8910101, e.g. 8910101000000000005"]

# Enum types — use Literal so the LLM sees the finite set
CustomerState = Literal["pending", "active", "suspended", "closed"]
SubscriptionState = Literal["pending", "active", "blocked", "terminated"]
OrderState = Literal["acknowledged", "in_progress", "completed", "failed", "cancelled"]
CaseState = Literal["open", "in_progress", "pending_customer", "resolved", "closed"]
TicketState = Literal["open", "acknowledged", "in_progress", "pending", "resolved", "closed", "cancelled"]
CasePriority = Literal["low", "medium", "high", "critical"]
CaseCategory = Literal["technical", "billing", "account", "information"]
UsageEventType = Literal["data", "voice_minutes", "sms"]
UsageUnit = Literal["mb", "gb", "minutes", "count"]
ProvisioningTaskType = Literal[
    "HLR_PROVISION",
    "HLR_DEPROVISION",
    "PCRF_POLICY_PUSH",
    "OCS_BALANCE_INIT",
    "ESIM_PROFILE_PREPARE",
]
FaultType = Literal["fail_first_attempt", "fail_always", "stuck", "slow"]
```

**Every tool function signature uses these types.** No raw `str` where a typed alias exists. Claude Code should import from `types.py` in every tool file. The spec's verification step greps for this.

Why it matters: when the LLM sees a tool schema with `subscription_id: string (Subscription ID in SUB-NNN format)` it generates valid SUB-NNN IDs. When it sees `subscription_id: string`, it fabricates `sub-abc-123` because that's also a string. The type hint IS the semantic hint.

For enum fields, `Literal["active", "blocked"]` renders as an enum in the JSON schema and the LLM respects it. `state: str` lets the LLM invent `state="frozen"` and discover it's invalid only at runtime.

#### (4) Structured error responses

When a policy violation occurs, `PolicyViolationFromServer` is raised with a structured body:

```python
{
    "rule": "subscription.vas_purchase.requires_active_cof",
    "message": "Customer has no active card on file for VAS purchase",
    "context": {
        "customer_id": "CUST-007",
        "active_methods_count": 0
    }
}
```

The rule IDs are **self-describing** — `subject.action.constraint`. The LLM doesn't need to look them up; it reads the rule ID and understands what went wrong. A smart system prompt + self-describing rule IDs means the LLM can recover from most policy failures without human help.

**The LLM's error-handling workflow (encoded in the system prompt):**

1. Read the `rule` field.
2. Decide: is this recoverable? (e.g., "card missing" → call `payment.add_card` first, retry)
3. If recoverable, attempt the fix with the same user intent.
4. If not, explain the error to the user using the `message` field.

This is how a cheap model can handle errors intelligently — not because it's clever, but because the error payload tells it exactly what to do next.

#### (5) `TOOL_SURFACE.md` — the human source of truth

`TOOL_SURFACE.md` is the reference Claude Code reads when generating the tool files. It lists every tool with: name, purpose, args with types, returns, related tools. The docstrings in the generated tool files must match `TOOL_SURFACE.md` exactly — no drift.

**Phase 9 verification:** add a test that walks every tool in `orchestrator/tools/` and confirms its function name and docstring purpose-line match the corresponding `TOOL_SURFACE.md` entry. Drift causes test failure.

If `TOOL_SURFACE.md` doesn't exist yet (from Phase 0), it needs to be created as part of Phase 9 pre-work. If it does exist but is stale, reconcile it before generating tools.

#### (6) Renderer output as in-context feedback

In REPL mode and in LLM-driven scenarios, the rendered ASCII output is fed back into the LLM's context, not just displayed to the user. This means when the LLM calls `subscription.get` → renders the hero view → the LLM sees the rendered output and can reason over it.

Example: user says "show me Ck's bundle, top up if needed." The LLM:
1. Calls `subscription.get` → JSON result
2. Renders via `subscription_renderer.render(result)` → ASCII hero view
3. Reads its own ASCII output: sees `● BLOCKED` and `Data [░░░░░░░░░] 0.0 / 30.0 GB 0%`
4. Decides: subscription is blocked, suggest VAS top-up
5. Calls `catalog.list_vas` → calls `subscription.purchase_vas`

**The renderer is semantic feedback.** A good render makes the LLM smarter. A noisy or cluttered render makes it dumber. Treat renderer quality as part of the LLM stack, not as cosmetic polish.

#### Semantic layer verification

Phase 9 adds these grep-based checks to the verification checklist:

```bash
# Every tool uses typed IDs from types.py, not raw str
grep -rn "customer_id: str\|subscription_id: str\|order_id: str" orchestrator/bss_orchestrator/tools/
# Expected: zero hits (all IDs use Annotated types from types.py)

# Every tool function has a docstring
python -c "
import ast, pathlib
for f in pathlib.Path('orchestrator/bss_orchestrator/tools').glob('*.py'):
    tree = ast.parse(f.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            assert ast.get_docstring(node), f'{f.name}::{node.name} missing docstring'
"
# Expected: silent success, no AssertionError

# No fabricated IDs in test fixtures (use the real format)
grep -rnE "SUB-[a-z]|CUST-[a-z]|ORD-[a-z]" orchestrator/bss_orchestrator/
# Expected: zero hits (all IDs use upper-case NNN format)
```

### 4. Tool implementation pattern — dumb, thin, no retries, fully typed

Every tool is a thin async wrapper over `bss-clients`. **No retries, no fallbacks, no business logic.** The supervisor handles retries and planning at the graph level.

```python
from bss_clients import SubscriptionClient
from bss_orchestrator.types import SubscriptionId, VasOfferingId


async def subscription_purchase_vas(
    subscription_id: SubscriptionId,
    vas_offering_id: VasOfferingId,
) -> dict:
    """Purchase a VAS (value-added service) for a subscription and charge the customer's
    default card-on-file. Use this when a customer is blocked due to bundle exhaustion
    and wants to top up, or when they want to add extra allowance to an active subscription.

    Args:
        subscription_id: The subscription to top up, in SUB-NNN format (e.g. SUB-007).
            Get this from subscription.list_for_customer or subscription.get_by_msisdn.
        vas_offering_id: The VAS product offering, in VAS_xxx format (e.g. VAS_DATA_5GB).
            Get this from catalog.list_vas.

    Returns:
        The updated subscription object. Check the `state` field — if the subscription
        was blocked, it should now be active. Check `balances` for the updated allowance.

    Raises:
        PolicyViolationFromServer: if a policy check fails. Common rules:
            - subscription.vas_purchase.requires_active_cof: no valid card on file
            - subscription.vas_purchase.vas_offering_sellable: VAS offering is inactive
            - subscription.vas_purchase.not_if_terminated: subscription is terminated
    """
    async with SubscriptionClient() as client:
        return await client.purchase_vas(subscription_id, vas_offering_id)
```

Every tool must map 1:1 to an existing entry in `TOOL_SURFACE.md`. No drift, no invented tools, no "helper" tools that combine multiple ops.

### 5. Safety / destructive op gating

`safety.py`:

```python
DESTRUCTIVE_TOOLS = {
    "customer.close",
    "customer.remove_contact_medium",
    "case.cancel",
    "ticket.cancel",
    "payment.remove_method",
    "order.cancel",
    "subscription.terminate",
    "provisioning.set_fault_injection",  # admin-ish
    "admin.reset_operational_data",
    "admin.force_state",
}

def wrap_destructive(tool_fn, allow_destructive: bool):
    async def wrapped(**kwargs):
        if not allow_destructive:
            return {
                "error": "DESTRUCTIVE_OPERATION_BLOCKED",
                "message": f"Tool {tool_fn.__name__} requires --allow-destructive flag. "
                           f"Ask the user to re-run with this flag if they truly intend this operation.",
                "tool": tool_fn.__name__,
            }
        return await tool_fn(**kwargs)
    return wrapped
```

The supervisor sees the structured error and can abort cleanly or ask the user to reconfirm and rerun. This pattern mirrors `PolicyViolationFromServer` — structured error, not a stack trace.

### 6. System prompt — `prompts.py`

The system prompt is the LLM's constitution. Keep it terse, factual, and opinionated. No flattery, no hedging, no "As an AI assistant". Every line exists because it prevents a specific failure mode.

```python
SYSTEM_PROMPT = """You are the BSS-CLI orchestrator, operating a lightweight TMF-compliant BSS for a mobile prepaid telco.

## Core rules
1. Plans are PLAN_S, PLAN_M, PLAN_L only. No other plans exist. Don't invent them.
2. Every customer must have a card on file before any subscription or VAS purchase.
3. Mock card: any 16-digit number works unless it contains 'FAIL' or 'DECLINE'.
4. Destructive operations require --allow-destructive. If blocked, report the error and ask the user.
5. Policy violations come back as structured errors with a `rule` field. Read it, understand the constraint, then decide: retry with corrections, or ask the user.
6. Never fabricate IDs. If you don't know an ID, call a read tool first (e.g. customer.list, subscription.list_for_customer). IDs always follow a strict format: CUST-NNN, SUB-NNN, ORD-NNN, CASE-NNN, TKT-NNN, PM-NNNN, PAY-NNNNNN, SVC-NNN, PTK-NNN, AGT-NNN.
7. Prefer one tool call at a time. Plan → call → observe → plan. Don't batch tool calls unless the plan is certain.
8. When an action affects a customer, the CRM policy layer auto-logs an interaction. Don't call interaction.log explicitly.
9. Current time comes from clock.now — don't assume.
10. Output style: terse. IDs and state, not paragraphs. When rendering results, delegate to the CLI renderer (return the IDs; the CLI will render). Don't explain what you're about to do — just do it and report what happened.

## Error recovery patterns

When a tool call raises PolicyViolationFromServer, the error payload looks like:
{"rule": "subject.action.constraint", "message": "human text", "context": {...}}

The rule ID tells you what to do next. Examples:

- rule="subscription.vas_purchase.requires_active_cof" → customer has no card. Call payment.add_card first, then retry subscription.purchase_vas.
- rule="order.create.requires_cof" → same fix path: add a card first.
- rule="customer.close.no_active_subscriptions" → not recoverable without terminating subscriptions. Ask the user; this is destructive territory.
- rule="usage.record.subscription_must_be_active" → subscription is blocked. Check the balance; if exhausted, suggest a VAS top-up; if active-but-wrong, open a ticket.
- rule="ticket.resolve.requires_resolution_notes" → pass resolution_notes argument.

If you don't recognize a rule, read the message and ask the user. Don't guess.

## Common workflows

**Customer signup:**
  customer.create → payment.add_card → order.create → (wait for order.completed via order.get polling) → subscription.list_for_customer → subscription.get

**"Why is my service not working":**
  customer.list (by name) → subscription.list_for_customer → subscription.get (check state)
  if state==blocked: subscription.get_balance → suggest VAS top-up via catalog.list_vas
  if state==active: case.open + ticket.open (escalate)

**VAS top-up on blocked subscription:**
  subscription.get → catalog.list_vas → subscription.purchase_vas → subscription.get (verify active)

**Investigate stuck provisioning:**
  order.get → service_order.list_for_order → provisioning.list_tasks (filter by state=stuck)
  if user confirms: provisioning.resolve_stuck
  otherwise: case.open + ticket.open (escalate to human)

## Context discipline

You operate on IDs, not names. When the user says "Ck", your first step is customer.list filtering by name to get CUST-NNN. Subsequent tool calls use the ID, not the name. If customer.list returns multiple matches, ask which one.

When you don't know something, call a read tool. Never guess an ID from context. Never assume a previous value is still current — if it matters, re-read.
"""
```

Add 4-6 few-shot examples in `prompts.py` showing the common workflows above. Examples must show real IDs in the correct format, real tool calls, and real error handling on a `PolicyViolationFromServer`. Claude Code should draft these during the plan phase; I'll review them before implementation.

### 7. ASCII renderers

**Six hero renderers must exist and look right:**

1. **Subscription show (the hero hero).** Bundle bars, state, countdown.

```
┌─ Subscription SUB-007 ──────────────────────────────────────┐
│                                                              │
│  Customer:    Ck (CUST-007)                                 │
│  MSISDN:      9000 0005                                     │
│  Plan:        Standard (PLAN_M) — SGD 25/mo                 │
│  State:       ● ACTIVE                                       │
│  Activated:   2026-04-10 09:15                              │
│  Renews in:   23 days (2026-05-03)                          │
│                                                              │
│  ── Bundle ──────────────────────────────────────────────    │
│  Data    [████████████░░░░░░░░░░░░░░]  12.4 / 30.0 GB  41%  │
│  Voice   [──────────────────────────]  unlimited            │
│  SMS     [──────────────────────────]  unlimited            │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

2. **Customer 360.** Status, contact, subscriptions, open cases with child tickets, recent interactions.

```
┌─ CUST-007  Ck  ─────────────────────────────────────────────┐
│  Status: ● active   since 2026-04-10                         │
│  Contact: ck@example.com · +65 9000 0005                     │
│                                                              │
│  ── Subscriptions (1) ─────────────────────────────────────  │
│  SUB-007  PLAN_M  active   MSISDN 90000005  bundle 41%       │
│                                                              │
│  ── Open Cases (1) ────────────────────────────────────────  │
│  CASE-042  "Data not working"       high    in_progress      │
│    └─ TKT-101  service_outage       high    assigned AGT-004 │
│                                                              │
│  ── Recent Interactions (3) ───────────────────────────────  │
│  2026-04-11 13:05  llm      create_customer                  │
│  2026-04-11 13:05  llm      add_payment_method               │
│  2026-04-11 13:06  llm      create_order PLAN_M              │
└──────────────────────────────────────────────────────────────┘
```

3. **Case show** with child tickets and notes.

```
┌─ CASE-042  "Data not working"  [in_progress]  high  ───────┐
│  Customer: CUST-007 Ck                                       │
│  Opened:   2026-04-11 13:15 by AGT-002 Bob                   │
│                                                              │
│  ── Tickets (2) ───────────────────────────────────────────  │
│  TKT-101  service_outage  in_progress  high  AGT-004 Dave    │
│  TKT-102  information     resolved     low   AGT-002 Bob     │
│                                                              │
│  ── Notes (1) ─────────────────────────────────────────────  │
│  [AGT-002 2026-04-11 13:18]  Customer reports no data since  │
│                              morning, voice works fine.      │
└──────────────────────────────────────────────────────────────┘
```

4. **Order show** with SOM decomposition tree.

```
┌─ ORD-014  PLAN_M  [completed]  ─────────────────────────────┐
│  Customer: CUST-007                                          │
│  Placed:   2026-04-11 13:05                                  │
│  Done:     2026-04-11 13:05 (+1.4s)                          │
│                                                              │
│  ── Service Order SO-022 [completed] ─────────────────────   │
│    └─ CFS SVC-033  MobileBroadband      activated            │
│         ├─ RFS SVC-034  DataBearer       activated           │
│         │     ├─ PTK-041 HLR_PROVISION   completed (0.5s)    │
│         │     └─ PTK-042 PCRF_POLICY     completed (0.3s)    │
│         ├─ RFS SVC-035  VoiceBearer      activated           │
│         │     └─ PTK-043 HLR_PROVISION   completed (0.5s)    │
│         └─ (CFS)  OCS_BALANCE_INIT       completed (0.2s)    │
│                                                              │
│  → Subscription SUB-007 activated                            │
└──────────────────────────────────────────────────────────────┘
```

5. **Catalog list** — three-column plan comparison.

```
┌─ Product Offerings ──────────────────────────────────────────┐
│                                                               │
│  ┌── PLAN_S  Lite ──┐  ┌── PLAN_M  Standard ─┐  ┌── PLAN_L  Max ─┐
│  │  SGD 10 /mo      │  │  SGD 25 /mo          │  │  SGD 45 /mo   │
│  │                  │  │                      │  │               │
│  │  Data    5 GB    │  │  Data    30 GB       │  │  Data   150GB │
│  │  Voice   100 min │  │  Voice   unlimited   │  │  Voice  unlim │
│  │  SMS     100     │  │  SMS     unlimited   │  │  SMS    unlim │
│  └──────────────────┘  └──────────────────────┘  └───────────────┘
│                                                               │
└───────────────────────────────────────────────────────────────┘
```

6. **eSIM activation card** with QR ASCII.

```
┌─ eSIM Activation ────────────────────────────────────┐
│                                                       │
│  ICCID:    8910 1010 0000 0000 005                  │
│  IMSI:     525 01 0000 0005                         │
│  MSISDN:   9000 0005                                 │
│                                                       │
│  ┌─────────────────────────────┐                     │
│  │ ▓▓ ▓▓▓▓  ▓▓ ▓▓▓▓▓ ▓▓▓▓ ▓▓ │  Scan with phone    │
│  │ ▓▓▓▓ ▓▓  ▓▓▓▓ ▓▓ ▓▓ ▓▓▓▓▓ │  camera to install  │
│  │  ▓▓ ▓▓▓▓ ▓▓  ▓▓▓▓ ▓▓  ▓▓ │  eSIM                │
│  │ ▓▓▓▓▓ ▓▓ ▓▓▓▓▓ ▓▓ ▓▓▓ ▓▓ │                      │
│  └─────────────────────────────┘                     │
│                                                       │
│  Or enter manually:                                   │
│  LPA:1$smdp.bss-cli.local$A4B29F81XK22M7PQ           │
│                                                       │
└──────────────────────────────────────────────────────┘
```

Use the `qrcode` Python library with text output mode. ~20 lines of renderer code. Invoked via `bss subscription show SUB-xxx --show-esim` or automatically on first-time display in a scenario.

**Ticket show and prov task list** use simpler `rich.table.Table` renderers.

**Renderer output in LLM context:** in REPL mode and in Phase 10 LLM-mode scenarios, the rendered ASCII output is fed back into the LLM's context as a tool result, not just displayed to the user. The renderer is therefore part of the semantic layer (section 3b item 6). Keep renders clean and information-dense.

### 8. Channel injection

Every CLI command sets these headers when making HTTP calls through `bss-clients`:

- Direct CLI: `X-BSS-Channel: cli`, `X-BSS-Actor: cli-user`
- LLM mode: `X-BSS-Channel: llm`, `X-BSS-Actor: llm-<model-slug>` (e.g. `llm-xiaomi-mimo-v2-flash`)
- Scenario runner (Phase 10): `X-BSS-Channel: scenario`, `X-BSS-Actor: scenario:<n>`

The LLM actor string derives from `settings.BSS_LLM_MODEL` at startup, slashes replaced with hyphens. When you swap from dev model to hero model, the audit trail reflects which model actually performed the actions. Useful for debugging.

CRM's interaction auto-logging reads these headers (wired in Phase 4). Phase 9 just ensures the CLI sets them correctly via `bss-clients`' header-propagation hook.

## Test strategy

Phase 4 lessons apply: httpx-equivalent testing through Typer's `CliRunner`, no direct function calls that bypass the CLI layer.

### Required test files

- `test_cli_customer_commands.py` — `CliRunner` tests for every customer subcommand
- `test_cli_order_flow.py` — `bss order create` → `bss order show` → `bss subscription show` end-to-end
- `test_renderers_snapshot.py` — snapshot tests for each hero renderer against canned input
- `test_orchestrator_tools.py` — every tool has a positive + policy-violation test
- `test_orchestrator_safety.py` — destructive tools blocked without flag, succeed with flag
- `test_orchestrator_graph.py` — simple two-step plan (create customer → add card), verify correct tool sequence via fake LLM
- `test_llm_policy_violation_handling.py` — fake LLM receives a structured PolicyViolation, verify it either retries with correction or asks the user
- `test_channel_injection.py` — every CLI invocation results in the right `X-BSS-Channel` and `X-BSS-Actor` on outbound calls
- `test_repl_session_state.py` — REPL retains context across turns
- `test_tool_docstring_compliance.py` — **new**: walk every tool, verify docstring has Purpose/Args/Returns/Raises sections, verify every arg matches the function signature, verify ID format examples are present
- `test_tool_surface_sync.py` — **new**: walk every tool, verify its name and purpose-line match the corresponding `TOOL_SURFACE.md` entry
- `test_types_coverage.py` — **new**: grep every tool file, confirm no raw `str` is used for ID arguments (must use `types.py` aliases)

### LLM mocking strategy

Unit tests for the graph use a deterministic fake LLM — a hand-rolled class implementing the `openai.AsyncOpenAI.chat.completions.create()` interface — that returns pre-programmed `ChatCompletion` responses with specific tool calls per test case. Integration smoke test against real OpenRouter is marked `@pytest.mark.integration` and skipped by default (same pattern as Phase 5's real-CRM integration test).

## Verification checklist

### Core CLI + renderers
- [ ] `bss --help` lists all command groups
- [ ] `bss customer create ...` works directly (no LLM)
- [ ] `bss order create ...` works, triggering Phase 7 end-to-end flow
- [ ] `bss subscription show SUB-xxx` renders the hero view correctly (snapshot)
- [ ] `bss customer show CUST-xxx` renders the 360 view (snapshot)
- [ ] `bss case show CASE-xxx` renders with child tickets (snapshot)
- [ ] `bss order show ORD-xxx` renders with SOM decomposition tree (snapshot)
- [ ] `bss catalog list` renders the 3-column plan comparison (snapshot)
- [ ] `bss subscription show SUB-xxx --show-esim` renders the eSIM activation card with QR ASCII

### LLM path
- [ ] `bss ask "create a customer named Ck on plan M with card 4242 4242 4242 4242"` produces the same end-to-end result as direct commands (uses real OpenRouter model, marked integration)
- [ ] `bss ask "show me Ck's bundle"` returns the ASCII render
- [ ] `bss ask "terminate Ck's subscription"` is blocked with `DESTRUCTIVE_OPERATION_BLOCKED`
- [ ] `bss ask "terminate Ck's subscription" --allow-destructive` succeeds
- [ ] `bss` with no args opens REPL; context persists across turns (mention customer once, refer later)
- [ ] Deliberate policy violation ("close CASE-xxx with open tickets") is reported cleanly by the LLM with the rule ID
- [ ] Recoverable policy violation (VAS top-up without card) → LLM calls `payment.add_card` first, then retries the VAS purchase automatically
- [ ] LLM tool calls log structured JSON to stdout/file
- [ ] Every CLI action shows up as an interaction in the relevant customer's log

### Semantic layer
- [ ] `grep -rnE "customer_id: str|subscription_id: str|order_id: str|case_id: str|ticket_id: str" orchestrator/bss_orchestrator/tools/` returns **zero hits** (all IDs use typed aliases from `types.py`)
- [ ] `test_tool_docstring_compliance.py` passes — every tool has Purpose/Args/Returns/Raises sections, every arg has an ID format example where applicable
- [ ] `test_tool_surface_sync.py` passes — `TOOL_SURFACE.md` and actual tool docstrings are in sync
- [ ] `grep -rn "retry\|backoff" orchestrator/bss_orchestrator/tools/` returns **zero hits** (tools stay dumb)
- [ ] `grep -rn -i "litellm" orchestrator/ cli/ pyproject.toml` returns **zero hits**
- [ ] System prompt in `prompts.py` includes the error recovery patterns section with at least 5 rule→action mappings
- [ ] 4-6 few-shot examples in `prompts.py` show real IDs, real tool calls, real error handling

### General
- [ ] `make test` — all suites green including CLI and orchestrator unit tests
- [ ] Campaign OS schemas untouched

## Out of scope

- `bss trace` ASCII swimlane (Phase 11 — needs OTel)
- Streaming token output in REPL (nice-to-have)
- Tab completion for IDs
- Color themes
- Save/load REPL sessions
- LiteLLM container (rejected — see section 3a)
- Real model in CI (integration tests only)

## Session prompt

> Read `CLAUDE.md`, `ARCHITECTURE.md`, `TOOL_SURFACE.md` (this is the source of truth for tools), `DECISIONS.md`, `phases/PHASE_07.md` (end-to-end flow), `phases/PHASE_08.md` (usage pipeline), and `phases/PHASE_09.md`.
>
> **Pay special attention to section 3b "Semantic layer discipline" in Phase 9.** This phase is not just "build the CLI and wire up an LLM." It is "build the semantic layer that makes a cheap, fast LLM reliable." The docstring quality, type hint discipline, system prompt precision, and structured error payload are the core deliverables — not add-ons.
>
> **LLM provider is OpenRouter via the `openai` SDK directly — NOT LiteLLM.** Default model is `xiaomi/mimo-v2-flash`. Verify the 5 `BSS_LLM_*` env vars exist:
>
> ```bash
> grep "^BSS_LLM" .env
> ```
>
> Before writing any code, produce a plan that includes:
>
> 1. **Typer command inventory** — every command group and subcommand with its arguments. Confirm this maps to the services built in Phases 3-8. No invented commands.
>
> 2. **LangGraph tool inventory** — every tool with its function signature using typed aliases from `types.py` (not raw `str`), the full docstring (Purpose + Args + Returns + Raises), and 1:1 mapping with `TOOL_SURFACE.md`. Flag any gaps.
>
> 3. **`types.py` contents** — paste the full NewType/Literal/Annotated aliases list. At minimum: all ID types, all state enums, usage event types, provisioning task types, fault types. This is the semantic vocabulary.
>
> 4. **TOOL_SURFACE.md reconciliation** — read the current `TOOL_SURFACE.md` (or create it if missing). For each planned tool, confirm the name, purpose, and args match. Flag any drift and propose reconciliation.
>
> 5. **Renderer mockups** — paste the ASCII mockup for each of the 6 hero renderers. These are the visual contract for the phase. Put them in `DECISIONS.md` under Phase 9.
>
> 6. **System prompt + few-shot examples** — paste the full system prompt and 4-6 few-shot examples showing (a) customer signup, (b) VAS top-up on blocked sub with recoverable PolicyViolation, (c) investigate stuck provisioning, (d) handle a non-recoverable policy violation gracefully. The system prompt must include the error recovery patterns section with concrete rule→action mappings.
>
> 7. **Safety wrapper** — paste the `DESTRUCTIVE_TOOLS` set and the `wrap_destructive` function.
>
> 8. **Channel injection mechanism** — confirm `bss-clients` propagates `X-BSS-Channel` and `X-BSS-Actor` from the CLI's `auth_context.current()`. Confirm the LLM actor string derives from `settings.BSS_LLM_MODEL` at startup.
>
> 9. **LLM client construction** — paste the contents of `orchestrator/bss_orchestrator/llm.py` showing `AsyncOpenAI` construction with `base_url`, `api_key`, and `default_headers`. Confirm no `litellm` import anywhere.
>
> 10. **LangGraph model binding** — paste the exact code that constructs the LangGraph `ChatOpenAI` from `settings.BSS_LLM_MODEL`, `base_url`, `api_key`, `default_headers`.
>
> 11. **LLM mocking strategy** — confirm unit tests use a deterministic fake client, integration tests marked `@pytest.mark.integration`.
>
> 12. **REPL session state** — paste the session object showing how captured IDs persist across turns.
>
> 13. **Tool dumbness contract** — paste one tool as an example of the canonical shape (typed args, full docstring with ID format examples, async wrapper over bss-clients, no retries).
>
> 14. **Three new test files** — paste the implementation plan for `test_tool_docstring_compliance.py`, `test_tool_surface_sync.py`, and `test_types_coverage.py`. These are what enforce the semantic layer discipline.
>
> Wait for my approval before writing any code. I will specifically review items 2, 3, 6, and 14 in detail — these are the semantic layer load-bearing pieces.
>
> After I approve, implement in this order:
>
> 1. `orchestrator/bss_orchestrator/types.py` — the semantic vocabulary, built first so every subsequent file can import from it
> 2. Direct Typer commands (no LLM) — every command works end-to-end
> 3. Channel injection wire-through via `bss-clients` hook
> 4. ASCII renderers — subscription first, then customer 360, case, order, catalog, eSIM
> 5. `TOOL_SURFACE.md` reconciliation commit if needed
> 6. Orchestrator tools — one file per domain, typed args, full docstrings, 1:1 with TOOL_SURFACE.md
> 7. Three new semantic-layer tests
> 8. Safety wrapper + destructive gating
> 9. `orchestrator/bss_orchestrator/config.py` with `BSS_LLM_*` via pydantic-settings (`_REPO_ROOT` pattern)
> 10. `orchestrator/bss_orchestrator/llm.py` with `AsyncOpenAI` → OpenRouter
> 11. `prompts.py` with SYSTEM_PROMPT and few-shot examples
> 12. LangGraph supervisor
> 13. REPL with session state
> 14. Unit tests with deterministic fake client
> 15. Integration smoke test against real OpenRouter (marked `@pytest.mark.integration`)
>
> Run full verification checklist. Do not commit.

## The trap

**The semantic layer is the product.** If you treat docstrings, type hints, and the system prompt as "documentation" to do at the end, the LLM will be flaky and you'll blame the model. A cheap fast model with a tight semantic layer beats a frontier model with a sloppy one, every time. Invest in types.py first, tool docstrings second, system prompt third.

**Build the direct CLI first.** If `bss customer create` doesn't work explicitly, the LLM version won't save you — it'll just obscure the bug.

**Tools stay dumb.** No retries, no fallbacks, no business logic. The supervisor handles retries at the graph level. Grep check enforces this.

**No LiteLLM.** Use `openai.AsyncOpenAI` directly. OpenRouter is an OpenAI-compatible endpoint; adding LiteLLM is proxying a proxy.

**Don't try to make the LLM "smart".** It only needs to be good enough to chain 3-5 tool calls with clean error handling. Catch fabrication early: if the LLM proposes a tool call with an ID that wasn't in any prior tool result, reject the call and loop back with the error.

**Snapshot-test the renderers.** They're easy to break accidentally (someone changes a field name in the TMF schema, the renderer silently renders garbage). Snapshot tests catch drift immediately.

**Channel injection is not optional.** If CLI actions don't show up in CRM's interaction log, the audit trail is broken and the "this customer called support, this agent did X" story fails at the demo.

**Use MiMo v2 Flash for dev and probably for hero scenarios too.** Pre-flight test showed clean instruction-following. Cost is ~$0.0000039 per small completion. Swap to Sonnet/Opus only if MiMo turns out to have weak tool-calling on specific cases. The model name lives in `.env` only.
