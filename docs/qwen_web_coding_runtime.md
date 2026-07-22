# Qwen Web coding runtime

This note records the operational contract between this JAWL fork and the local
Qwen Bridge. It separates durable JAWL state from transport continuity so that
performance work does not accidentally change JAWL's memory model.

## Source of truth

JAWL remains the source of truth for conversation state: SQL ticks, Vector RAG,
tasks, notes, action journals, coding plans, and the current prompt snapshot.
Qwen Web is a reasoning provider, not the durable session database.

The bridge keeps one web chat ID per account, model, and transport for one hour
of inactivity. It deliberately sends `parent_id: null` with the complete JAWL
snapshot. Reusing the chat removes repeated chat creation and keeps operator UI
continuity, but does not rely on hidden upstream history. Chaining every full
16k-23k-token JAWL snapshot to the previous Qwen message would duplicate context
and make rotation or recovery ambiguous.

A future delta-thread mode is possible, but it needs an explicit snapshot hash,
serialized requests per account, periodic full resynchronization, and full
snapshot replay after rotation. It should be enabled only after an evaluation
shows a material quality or latency gain.

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

Current low-risk defaults:

- provider thinking on the first ReAct step only;
- temperature 0.3 for more deterministic JSON and tool selection;
- adaptive skill catalogue with on-demand exact schemas;
- batch independent operations in one action plan;
- finite script sessions for work that must be polled without blocking a step.

Recommended next optimization is a deterministic task router with two prompt
profiles:

1. `micro`: no provider thinking by default, 3-5 ReAct steps, compact recent
   state and only the relevant skill namespaces;
2. `full`: existing memory, planning, coding gates, and first-step thinking for
   ambiguous or multi-file work.

The router must be reversible: escalation from `micro` to `full` should carry a
bounded evidence summary, while high-risk file, process, desktop, Git, and MCP
operations keep the same guards in both profiles.

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
