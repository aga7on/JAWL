# LLM Providers

JAWL is provider-neutral. Every cognitive subsystem — ReAct, Durable Goals,
Tree of Thoughts, the Subconscious and the Swarm — speaks one internal contract
and never sees a vendor's HTTP surface. QWB (the Qwen Web bridge) is still fully
supported, but it is now one adapter among others rather than the assumed
runtime.

## The internal contract

Everything crossing the provider boundary is typed and vendor-agnostic:

| Type | Purpose |
| --- | --- |
| `LLMRequest` | model, messages, tools, transport, streaming, timeout, optional session lane |
| `LLMMessage` | `system` / `developer` / `user` / `assistant` / `tool` roles, tool calls, tool results |
| `LLMToolDefinition` | one function tool plus its JSON Schema |
| `LLMResult` | content, tool calls, finish reason, usage, reasoning, provider metadata |
| `LLMToolCall` | id, name, raw JSON arguments |
| `LLMUsage` | prompt / completion / total / reasoning / cached tokens |
| `ProviderError` | a classified, already-redacted failure |

`LLMExecutor` consumes only these types. Adding a provider means implementing
`LLMProvider.complete()` — no change to the agent core.

## Available adapters

### `openai_compatible`

A standards-only Chat Completions client: configurable `base_url`, `api_key`
and `model`; streaming or buffered; native `tools` / `tool_choice`; usage
accounting; timeout and cancellation; `Retry-After` handling. It sends no QWB
headers, endpoints or payload extensions, and works against Ollama, LM Studio,
vLLM, llama.cpp, OpenRouter, and cloud vendors.

### `qwb`

Subclasses the standard adapter and adds everything Qwen Web needs, all
contained in `src/l3_agent/llm/providers/qwb.py`:

* lane continuity via `X-Session-Id` and provider rebase on context growth
* the `enable_thinking` transport extension
* empty/leaked answer detection (`empty_qwen_answer`)
* `CHAT_NOT_FOUND` mapped to the `session_state` category
* `tool_protocol_error` mapped to the bounded tool-protocol repair budget
* account-pool health diagnostics (fingerprints only, never tokens)
* media base URL exposure for image/video extensions

Because QWB owns its own account rotation, JAWL deliberately does *not* cool
down the single bridge credential on a 429.

## Capability profiles

A provider declares what it supports; JAWL never guesses from a model name:

```yaml
llm:
  provider:
    kind: openai_compatible
    capabilities:
      native_tools: true
      json_schema: true
      vision: false
      video: false
      image_generation: false
      reasoning: true
      context_window: 262144
      streaming: true
      server_side_conversation: false
```

Omit `capabilities` to use the adapter's conservative default profile. A
capability that a runtime does not declare is treated as unsupported, so an
unknown never silently enables native tools or multimodal input.

## Tool transports

| Transport | Behaviour |
| --- | --- |
| `json_envelope` | JAWL's own JSON action plan. Works on any text model; the default and the fallback. |
| `native` | Provider `tool_calls`. Requires `native_tools: true`. |
| `auto` | Resolved from the capability profile at startup. |

The legacy names `wrapper` and `hybrid` still load and migrate automatically to
`json_envelope` and `auto`. The JSON action parser remains a first-class,
independent mode — it is not a degraded path.

## JAWL stays the source of truth

A provider session is an optimization, never storage. Goals, the Task Ledger,
ticks, personality, RAG and action history live in JAWL. If a provider loses
its conversation — or the provider has none at all — progress is unaffected:
the next cycle reconstructs context from the durable local projection. Static
context is hashed for cache reuse; dynamic context is strictly budgeted.

## Retry policy

Four independent budgets, each with its own limit and telemetry, so one class
of failure cannot exhaust another's allowance:

| Bucket | Covers |
| --- | --- |
| `transport` | connection failures and timeouts |
| `provider` | 5xx, 408, 409, 429 and upstream outages |
| `invalid_response` | empty or unusable responses that a retry may repair |
| `tool_protocol` | malformed tool calls or action envelopes |

Configuration errors (unknown model, bad parameter, context overflow) are never
retried. Counters appear in `last_call_metrics["retry_counters"]`.

## Switching providers from the CLI

**Main menu → LLM Providers** offers:

* **Scan local runtimes** — probes Ollama and LM Studio, lists each model with
  its declared capabilities, suggests the best coding model, and saves a
  matching profile plus the transport that model can actually honour.
* **Configure / switch provider** — pick the adapter, base URL and key, then
  choose the model from a picker populated by the endpoint's real
  `/v1/models` list (with manual entry as a fallback).
* **Refresh health** — status, latency and, for QWB, account diagnostics.

Preflight validation runs before the agent starts and rejects a missing model,
a malformed base URL, a cloud endpoint with no key, or `native` transport on a
provider without `native_tools`. Secrets are written only to `.env`; they never
enter `settings.yaml`, logs, or runtime telemetry.

## Local runtimes

Discovery reads real metadata rather than parsing names:

* **Ollama** — `/api/show` supplies the declared `capabilities` list (tools,
  vision, thinking), parameter size, quantization and context length.
* **LM Studio** — `/api/v0/models` supplies architecture, quantization, max
  context and tool-use support; embedding models are marked non-chat.

Model names are tokenised on `-`, `_`, `.`, `/` and `:` boundaries, so a name
such as `gemma-4-12b-coder-fable5-composer2.5-v1` is not mistaken for the `e5`
embedding family. Ranking only orders suggestions; it never sets a capability.

## Testing

* `tests/unit/l3/llm/providers/test_provider_contract.py` — one shared contract
  across a deterministic fake provider, QWB and the standard adapter.
* `tests/unit/l3/llm/providers/test_openai_http_contract.py` — a real
  OpenAI-compatible HTTP server (real sockets, SSE framing and status codes)
  covering text, tool calls, JSON plans, streaming, cancellation, retries,
  malformed responses, context budgeting and usage.
* `tests/unit/l3/llm/providers/test_discovery.py` — local runtime discovery.

Live smoke tests, skipped unless explicitly enabled:

```bash
# Any OpenAI-compatible endpoint
JAWL_LIVE_PROVIDER=1 \
JAWL_LIVE_PROVIDER_URL=http://127.0.0.1:11434/v1 \
JAWL_LIVE_PROVIDER_MODEL=<model> \
pytest tests/integration/src/l3/llm/test_live_provider_goal.py -s

# QWB bridge
JAWL_LIVE_QWB=1 \
JAWL_LIVE_QWB_URL=http://127.0.0.1:8000/v1 \
JAWL_LIVE_QWB_MODEL=qwen3.8-max-preview \
JAWL_LIVE_QWB_KEY=<bridge key> \
pytest tests/integration/src/l3/llm/test_live_qwb_goal.py -s
```

Both drive the same scenario: create a Durable Goal, read and patch a real
project, run its real test suite, checkpoint, survive the loss of the provider
session, and complete only with passing verification evidence.
