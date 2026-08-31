# Qwen Web coding runtime

This note records the operational contract between this JAWL fork and the local
Qwen Bridge. It separates durable JAWL state from transport continuity so that
performance work does not accidentally change JAWL's memory model.

## Source of truth

JAWL remains the source of truth for conversation state: SQL ticks, Vector RAG,
tasks, notes, action journals, coding plans, and the current prompt snapshot.
Qwen Web is a reasoning provider, not the durable session database.

Ordinary ReAct cycles use isolated trace lanes. An active Goal instead supplies
one stable `X-Session-Id` lane across bounded cycles. Its first request sends a
complete authoritative bootstrap; subsequent requests remain on the accepted
Qwen `parent_id` chain and send only the snapshot delta. JAWL still persists the
objective, evidence, tool results, and continuation state locally.

The active Goal also owns a versioned local Task Ledger. Unlike chat history,
the ledger is an authoritative operational checkpoint: phase, acceptance
criteria, completed/pending stages, evidence-backed facts, failed approaches
with retry conditions, artifacts, known tool/session state, blockers, last
action batch, and the exact next action. Each sparse model-authored update is
persisted before dispatching the associated action batch; physical outcomes
then add bounded evidence pointers and failed-action records automatically.

QWB starts a fresh full chain when the model/static prompt/tool schema changes,
the checkpoint is incomplete, the delta is too large, the pinned account fails,
or downstream cancellation makes the outcome ambiguous. A JAWL restart also
increments the durable goal lane epoch and forces a clean bootstrap.

Goal lanes allow a larger bounded snapshot delta than disposable trace lanes
(90% versus 60%, with the same 32k absolute ceiling). This avoids throwing
away a useful warm Goal branch merely because its compact 36k projection
changed substantially after a tool result.

When provider-reported prompt context reaches the configured Goal threshold
(65k tokens by default), JAWL increments the durable lane epoch. QWB then
creates a clean upstream conversation and bootstraps it from the local Goal and
Task Ledger instead of carrying an indefinitely growing web-chat chain.

## Web tool transport

Qwen Web cannot invoke JAWL's caller-defined OpenAI function directly. The
bridge therefore asks it for a literal `<tool_call>` JSON envelope and converts
that envelope into a native downstream `tool_calls` response.

The boundary accepts strict JSON first, then bounded repair for common model
mistakes such as missing commas, missing closing braces, a missing closing XML
tag, or a bare unambiguous `execute_skill` argument object. Parse misses are
logged by length, structural counts, and a short SHA-256 rather than leaking the
possibly sensitive response body.

## Small-task performance

JAWL can execute multiple independent actions in one response through explicit
parallel groups and dependencies. That is efficient for a batch of reads,
searches, or checks. It is less efficient when every tiny action causes another
large general-purpose prompt and provider round trip.

Current low-risk mechanisms:

- configurable provider Thinking policy;
- adaptive skill catalogue with on-demand exact schemas;
- batch independent operations in one action plan;
- finite script sessions for work that must be polled without blocking a step.
- active Goal projection capped independently from general dynamic context;
- compact Goal Protocol v2, which omits repeated chain-of-thought fields;
- provider-vs-local token telemetry to measure QWB delta reuse.

### Live commands and monitoring boundary

QWB is intentionally a provider transport, not an operating-system monitor.
JAWL owns application state, shell sessions, desktop/UIA observations, and MCP
connections. Finite script sessions are monitored locally after the initiating
LLM call returns; completion publishes a `HOST_OS_SANDBOX_EVENT`, so the next
step reads the exact saved session instead of polling the process table.
MCP application state is read through allowlisted tools; arbitrary automatic
polling is not enabled because an MCP tool is not inherently read-only.

For an explicitly small live command, terminal or Telegram input may start with
`/quick` (alias `/fast`). JAWL retains SOUL and all static safety/tool protocol
rules, but projects only the current trigger, active Goal checkpoint, agent/MCP/
host state, and a bounded skill catalogue. The request uses a separate warm QWB
lane keyed to the channel identity, so it neither replaces nor bloats the main
Goal conversation. JAWL still sends the bounded authoritative snapshot over
localhost; when the warm parent chain is healthy, QWB sends only its delta to
Qwen. This preserves cold-recovery correctness while avoiding a full upstream
bootstrap for each short command.

## Long-running delegated work

Swarm workers are durable, role-restricted background tasks. The parent can now
poll a worker with a bounded wait, retrieve the exact durable report, cancel the
exact current-process task, or send a bounded steering message that is injected
at the next worker step boundary.

This covers the controlled core of an agent-team runtime without changing
JAWL's philosophy. Useful follow-up work inspired by agent-team systems is a
shared dependency board, bounded stall detection/nudges, peer messages with
deduplication, and optional per-worker Git worktrees. Automatic replay of an
interrupted worker is deliberately excluded until task side effects can be
proven idempotent.

## Observed bottlenecks

The July 22 runtime showed that the main constraint is interaction efficiency,
not raw Qwen reasoning quality:

- repeated steps carried roughly 16k-21k input tokens;
- first-step thinking commonly took about 1.5-3 minutes;
- non-thinking tool steps commonly took about 15-40 seconds;
- malformed textual JSON and guessed obsolete skill names wasted whole steps;
- a 15-step cycle could be exhausted on a sequence of small experiments.

Sticky account chats, repaired tool envelopes, deterministic sampling, bounded
background sessions, and controlled workers address reliability. The largest
remaining latency win is the micro/full prompt router plus better batching and
catalogue-name feedback, not adding more memory to every request.

# Qwen 3.8 preview context and Thinking

The coding fork uses `qwen3.8-max` for both coding and vision.

Each ordinary ReAct cycle has an isolated `X-Session-Id`. An active durable Goal
keeps the same lane across cycles and reconstructs every step from bounded JAWL
state. QWB keeps that Qwen branch and sends changed snapshot lines; JAWL never
depends on the hidden branch as its sole memory.

For this preview model, `enable_thinking: false` is a downstream presentation
preference only. Qwen Web rejects literal `thinking_enabled:false`, so QWB
keeps upstream Thinking enabled and strips its private phases before returning
the OpenAI-compatible answer.

## Failure and retry contract

Retries are reason-aware rather than based on HTTP status alone:

- stale/deleted Qwen chat state is an upstream session failure; QWB discards
  the account-local chat ID, rotates away from a failed pinned account, creates
  a fresh chat, and returns HTTP 503 if bounded recovery is exhausted;
- network, TLS, SSE-abort, inactivity, quota, and other transient upstream
  failures remain retryable under the existing bounded backoff;
- ambiguous provider HTTP 400 input rejections receive one configurable fresh
  JAWL retry (`llm.invalid_request_retries`, default `1`);
- deterministic model/parameter/context configuration errors are not replayed.

If a retryable rejection still exhausts a ReAct call, an active Goal is left
active with a scheduled continuation. Only a deterministic configuration error
blocks it for operator correction. This prevents an otherwise durable Goal
from ending because one Qwen web chat disappeared.

## UI and MCP routing

Adaptive context always exposes the small MCP broker surface and routes desktop
skills when the objective mentions windows, dialogs, screenshots, or GUI work.
For a named target such as x64dbg, discovery is constrained to the configured
`x64dbg-mcp` server. Cross-server results prioritize the server explicitly
named in the query and allowlisted tools, preventing unrelated Ghidra debugger
matches from consuming the bounded result first.

The bundled local x64dbg workflow uses explicit `LaunchDebuggee` and
`AttachProcess` tools. The agent should call one of them and verify `GetState`,
not repeatedly search for paraphrases such as "open/create debuggee".

Windows UI work follows an evidence loop: identify the target window, observe
its exact UI Automation controls, act using the short-lived element/hash pair,
then wait or re-observe. Screenshot capture retries one transient OS failure;
capture failure alone is not interpreted as proof that a working desktop is
headless.
