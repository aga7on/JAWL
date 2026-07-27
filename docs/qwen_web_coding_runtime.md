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

QWB starts a fresh full chain when the model/static prompt/tool schema changes,
the checkpoint is incomplete, the delta is too large, the pinned account fails,
or downstream cancellation makes the outcome ambiguous. A JAWL restart also
increments the durable goal lane epoch and forces a clean bootstrap.

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

The coding fork uses `qwen3.8-max-preview` for both coding and vision.

Each ordinary ReAct cycle has an isolated `X-Session-Id`. An active durable Goal
keeps the same lane across cycles and reconstructs every step from bounded JAWL
state. QWB keeps that Qwen branch and sends changed snapshot lines; JAWL never
depends on the hidden branch as its sole memory.

For this preview model, `enable_thinking: false` is a downstream presentation
preference only. Qwen Web rejects literal `thinking_enabled:false`, so QWB
keeps upstream Thinking enabled and strips its private phases before returning
the OpenAI-compatible answer.
