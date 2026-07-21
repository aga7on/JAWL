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
| Context navigation | Good | Bounded map, ripgrep/Python search, numbered ranges with hashes | Symbol extraction is heuristic; no LSP/tree-sitter reference graph |
| Diff review | Strong | Per-file/page unified diff, untracked preview bound, secret redaction | Tracked Git output is bounded after collection rather than during process streaming |
| Verification | Strong | Detected allowlisted profiles, process-tree timeout, exact-state fingerprint commit gate | No repository-owned declarative verification policy or flaky-test classification |
| Action scheduling | Strong | Sequential default, explicit dependencies/parallel groups, shared resource locks | No cross-step dependency graph or requirement-level plan |
| Interruption recovery | Good | Durable action lifecycle, restart classification, persistent worktree state | Recovery is inspect-first but not yet an automatic reconciliation state machine |
| LLM protocol | Improving | Multiple wrapper calls merged, invalid output persisted, terminal step-limit record | Skills are still described in prompt text behind one wrapper instead of native per-skill schemas |
| Telemetry | Basic-good | Request IDs, attempts, latency, finish reason, usage snapshot, per-action duration | No end-to-end cycle/task trace ID, cost rollup, or dashboard export |
| Evaluation | Basic-good | Versioned deterministic capability gate with JSON reports | No fixed-repository end-to-end patch benchmark with quality/token/time grading |
| Planning | Weak | Global SQL tasks and durable ticks preserve intent/history | No task-local requirement/checklist/evidence ledger with explicit completion gates |

## Priority order

1. Add a persistent coding plan per task workspace: requirements, ordered steps,
   dependencies, state transitions, verification evidence, and completion gates.
2. Introduce cycle/task trace IDs that link LLM calls, actions, ticks,
   verification runs, and commits without replacing JAWL's event model.
3. Make Git diff collection byte-bounded while streaming and add a repository
   verification policy file with an explicit command allowlist.
4. Add an adapter that can expose selected registered skills as native tool
   schemas while retaining `execute_skill` for Qwen and legacy providers.
5. Upgrade code navigation with optional tree-sitter/LSP-backed definitions,
   references, and dependency slices; retain the current zero-index fallback.
6. Add fixed-task end-to-end evaluation repositories and grade patch correctness,
   regression safety, tool calls, wall time, and context/token use separately.

## Compatibility guardrail

None of these steps should replace Heartbeat, EventBus, vector/graph memory,
personality, interfaces, or proactive autonomy. Coding state is a task-scoped
durable projection consumed by those systems. Existing skill names and the
`ActionCall(tool_name, parameters)` payload remain supported throughout.
