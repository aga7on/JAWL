# Coding agent gap audit

Audit date: 2026-07-21. Scope: the isolated `codex/coding-agent` fork, including
the preserved local QWB and Telethon reliability changes.

This is a capability audit, not a marketing comparison. Proprietary agents
change rapidly and cannot be ranked honestly from architecture alone. The useful
standard is whether JAWL provides the substrate needed to complete repository
tasks safely, recoverably, and with measurable evidence.

## Current position

| Capability | State | Evidence | Remaining gap |
| --- | --- | --- | --- |
| Atomic editing | Strong | SHA-checked exact-match patches, atomic writes, reversible checkpoints | No syntax-aware patch primitive |
| Task isolation | Strong | Persistent branch/worktree per task, dirty-base guard, recovery stash | No automatic branch publication or merge conflict assistant |
| Context navigation | Strong | Bounded map/search/range reads, syntax-aware occurrences, directed local dependency slices, and optional allowlisted LSP definition/reference resolution with zero-index fallback | LSP uses safe one-shot processes rather than a cached incremental workspace session |
| Diff review | Strong | Per-file/page unified diff, streaming tracked-output cap, untracked preview bound, secret redaction | No syntax-aware hunk grouping |
| Verification | Strong | Detected allowlisted profiles, hashed repository policy, process-tree timeout, exact-state fingerprint commit gate | No flaky-test classification |
| Action scheduling | Strong | Sequential default, explicit dependencies/parallel groups, shared resource locks, durable requirement-level step graph | No learned replanning policy |
| Interruption recovery | Good | Durable action lifecycle, restart classification, persistent worktree state | Recovery is inspect-first but not yet an automatic reconciliation state machine |
| LLM protocol | Good | Compatible wrapper plus bounded native/hybrid schema export, multiple-call merge, noisy Qwen payload recovery, bounded transient retries, configurable Thinking policy, persisted failures, terminal step-limit record | QWB exposes only a Boolean Thinking switch, not a token/time budget; adaptive per-task policy still needs measured validation |
| Telemetry | Good | Async-safe cycle trace links LLM calls, ticks, actions, plans, verification and commits; request/action timing and usage snapshots | No cost rollup or dashboard export |
| Evaluation | Strong substrate | Deterministic capability gate, fixed hidden-test repositories, isolated real-ReAct driver, recorded QWB calibrations, and a shell-free external CLI driver using the identical grader | No equivalent external-agent baseline has been executed yet |
| Planning | Strong | Persistent task-local requirements, dependency steps, revision guards, evidence history, and commit gate | No automatic plan synthesis quality grader |

## Priority order

1. Validate `first_step` Thinking and proportional planning in one deliberate
   fixed-task live run before expanding the benchmark set; avoid repetitive
   account-consuming calibration.
2. Execute declared external CLI baselines through `drive_cli.py` before making
   comparative performance claims; it holds candidate inputs, patch limits,
   timeout handling, and grading constant. Adversarial hidden-test isolation
   still requires the candidate CLI's sandbox or an external container.
3. If live traces show repeated navigation startup cost, add lifecycle-managed
   incremental LSP sessions without weakening process and output bounds.

## Recorded live calibration

`benchmarks/coding_tasks/results/2026-07-21-qwb-qwen3.8-max-preview-wrapper.json`
records the first real QWB run on revision `f8ae5f8`. The one-task patch passed
public tests, hidden tests, allowed-file scope, and patch economy with score
`1.0`. The agent nevertheless failed the lifecycle gate: four protocol errors,
nine ReAct steps, approximately 83.8k input and 24.9k output tokens, and no
verified commit before the 1,200-second watchdog. This isolates the next work to
transport/protocol efficiency rather than patch-generation ability.

A second run on `ad1a04b` tested task-relative batching with a 600-second cap.
No action reached the registry: bounded protocol excerpts proved that Qwen was
emitting answer-channel format deliberation and trial bare JAWL payloads inside
`tool_call` tags. That evidence motivated the bounded multi-candidate parser;
the run is recorded in
`benchmarks/coding_tasks/results/2026-07-21-qwb-qwen3.8-max-preview-taskhandles.json`.

The third run on `820a949` validated the parser fix: zero protocol errors, with
workspace creation, plan initialization, and task-scoped search all executed in
the first action tick after a 110-second model call. Step two then failed on a
QWB `500 quota_limit/high demand`, exposing that ReAct still used one total
inference attempt. The executor now retries bounded 5xx/connection failures; the
run is recorded in
`benchmarks/coding_tasks/results/2026-07-21-qwb-qwen3.8-max-preview-parserfix.json`.

The fourth run on `97f6054` validated bounded transient retries. It again had
zero protocol errors, and its second model call succeeded on attempt two after
a temporary QWB 500. The 600-second watchdog nevertheless cancelled call three
after 289 seconds. The first two calls took 162 and 149 seconds, while the model
expanded a localized one-file task into five requirements and five steps. This
isolates the current performance gap to repeated Qwen Web Thinking latency and
disproportionate planning. JAWL now supports a compatibility-safe
`thinking_policy`; the local fork uses `first_step` so the initial decision can
think deeply while subsequent action-followup turns ask QWB to skip Thinking.
The run is recorded in
`benchmarks/coding_tasks/results/2026-07-21-qwb-qwen3.8-max-preview-retryfix.json`.

## Compatibility guardrail

None of these steps should replace Heartbeat, EventBus, vector/graph memory,
personality, interfaces, or proactive autonomy. Coding state is a task-scoped
durable projection consumed by those systems. Existing skill names and the
`ActionCall(tool_name, parameters)` payload remain supported throughout.
