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
| LLM protocol | Good | Compatible wrapper plus bounded native/hybrid schema export, multiple-call merge, persisted protocol failures, terminal step-limit record | Live QWB calibration exposed repeated format failures and expensive recovery; provider-specific auto-probing/policy is absent |
| Telemetry | Good | Async-safe cycle trace links LLM calls, ticks, actions, plans, verification and commits; request/action timing and usage snapshots | No cost rollup or dashboard export |
| Evaluation | Strong substrate | Deterministic capability gate, fixed hidden-test repositories, isolated real-ReAct driver, and a recorded QWB calibration | No equivalent baseline run yet; first QWB run passed patch quality but failed lifecycle/time budget |
| Planning | Strong | Persistent task-local requirements, dependency steps, revision guards, evidence history, and commit gate | No automatic plan synthesis quality grader |

## Priority order

1. Reduce QWB wrapper protocol failures and excessive ReAct round trips, then
   rerun the same fixed task before expanding the benchmark set.
2. Execute equivalent declared baseline agents before making comparative
   performance claims.
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

## Compatibility guardrail

None of these steps should replace Heartbeat, EventBus, vector/graph memory,
personality, interfaces, or proactive autonomy. Coding state is a task-scoped
durable projection consumed by those systems. Existing skill names and the
`ActionCall(tool_name, parameters)` payload remain supported throughout.
