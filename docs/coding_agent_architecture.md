# Coding Agent Architecture

This fork strengthens JAWL as a software-engineering agent without replacing its
event-driven identity, local memory, interfaces, heartbeat, or personality model.

## Compatibility invariants

1. Existing skills and `ActionCall(tool_name, parameters)` payloads remain valid.
2. L0-L3 boundaries, the EventBus, Heartbeat, RAG, Swarm RBAC, and L2 plugin
   discovery remain framework-level primitives rather than coding-only services.
3. Coding features are additive and usable by the main agent and authorized
   subagents through the same skill registry.
4. Existing local data stores and configuration files remain readable unless a
   migration is explicit, versioned, tested, and reversible.
5. Physical side effects favor deterministic execution, bounded permissions,
   reviewable patches, and recoverable state over speculative parallelism.

## Action execution contract

- Actions are sequential by default.
- Parallel execution is opt-in through a named `parallel_group`.
- `depends_on` creates explicit success dependencies between action IDs.
- Failed dependencies skip dependent actions rather than executing with invalid
  assumptions.
- File-like parameters and explicit `resources` are locked across concurrent
  plans in the same event loop.
- Cancellation propagates to running action tasks.

This contract is the foundation for later worktree isolation, durable action
journaling, checkpoint/rewind, native patch application, and coding evaluation.

## Safe editing contract

- The legacy `patch_file` skill remains available to existing prompts.
- New coding flows should use `apply_file_patch` with the SHA-256 returned by the
  preceding read whenever possible.
- Every search block must match exactly once; ambiguous edits fail without
  changing the file.
- A multi-edit file patch is computed completely before a single atomic write.
- Every successful safe patch creates a persistent checkpoint under the protected
  `sandbox/_system/` area.
- `restore_file_checkpoint` refuses to overwrite content changed after the patch.

## Symbol navigation contract

- `locate_code_symbol` scans a bounded source set without requiring a persistent
  index and returns definitions before references with file, line, column,
  preview, backend, and confidence metadata.
- Python occurrences come from the standard AST, so comments and strings do not
  become false references. Other supported languages use tree-sitter when its
  parser is available; parser/version failures degrade to an explicitly labeled
  lexical fallback instead of aborting the ReAct cycle.
- Result count, source-file size, file count, match count, serialized output,
  and fallback diagnostics are bounded. Shared cached parsers are serialized
  because concurrent tree-sitter parsing is not assumed to be thread-safe.
- `resolve_code_symbol` optionally starts an installed allowlisted language
  server over bounded stdio JSON-RPC for project-resolved definitions or
  references. It auto-discovers a nearby project manifest, filters locations to
  that validated root, handles UTF-16 LSP columns, bounds messages/results/time,
  and terminates the one-shot server after each request.
- Python/JavaScript/TypeScript/Rust/Go/C/C++ server commands are discovered from
  a fixed executable allowlist; the model cannot supply a process command. When
  no server is installed, initialization fails, or no in-project result exists,
  the same skill returns the existing AST/tree-sitter/lexical occurrence result
  with the fallback reason explicitly labeled.
- `constraints.txt` pins the tree-sitter constructor API expected by
  `tree-sitter-languages` for newly bootstrapped environments. Existing
  incompatible environments remain functional through the fallback path.
- `get_code_dependency_slice` builds a bounded local directed graph around one
  entry file. Python imports are resolved through AST module mapping; JS/TS
  relative modules and C/C++ local includes have conservative filesystem
  resolution. External packages are omitted instead of being guessed.
- Dependency and dependent traversal are independently selectable and bounded by
  depth, returned files, indexed files, source size, and serialized output. Each
  edge retains its source line and original import text for targeted inspection.

## Task workspace contract

- Non-trivial repository work can be isolated with `create_coding_workspace`.
- Each task gets a dedicated `jawl/<task>` branch and Git worktree under the
  hidden `sandbox/.jawl-worktrees/` directory.
- Workspace metadata is persisted in the protected system area and can be resumed
  after a ReAct cycle or framework restart.
- Dirty base repositories are rejected by default because their uncommitted state
  cannot be represented faithfully by a worktree base commit.
- Status and diff summaries are inspectable without switching the user's branch.
- Cleanup preserves commits and the task branch. Uncommitted work requires an
  explicit `force=true`; tracked and untracked changes are first saved to a
  recovery Git stash rather than being silently destroyed.
- `HostOSCodingFiles` resolves bounded read, search, and checked-patch operations
  from a stable `task_id` plus relative path. Models never need to copy hidden
  worktree paths between turns, and dependency plans can safely batch
  workspace→read or patch→verify→evidence→commit while preserving every gate.

## Durable coding plan contract

- A task workspace may own a persistent objective, requirements, dependency-aware
  steps, bounded evidence, and a 200-entry state-transition history.
- Plan reads return a compact status summary by default; detailed step,
  requirement, and history evidence is retrieved through bounded pages. Explicit
  replacement archives the previous plan instead of deleting its audit trail.
- Plan writes use monotonic revisions and optional optimistic concurrency guards;
  coding prompts require `expected_revision` so interrupted or parallel agents do
  not overwrite newer state.
- Steps cannot start or complete before their dependencies. Completed or blocked
  states and satisfied or blocked requirements require concrete evidence.
- Existing tasks without a plan preserve the legacy commit flow. Once a plan is
  initialized, final commits require every step completed and requirement
  satisfied; explicit bypass remains available for intermediate checkpoints.

## Durable execution contract

- Every configured action plan receives a stable plan ID and append-only JSONL
  lifecycle records for plan start, action start/finish/cancel, and plan finish.
- Journal writes are fsynced and rotated; journaling failure cannot prevent an
  authorized physical action from completing.
- Sensitive parameter fields and inline credentials are redacted before storage.
- A plan left unfinished by an older process session is reported as interrupted.
- Journal inspection never automatically replays uncertain side effects. Recovery
  begins by comparing journal evidence with the persistent task workspace.
- Action completion and cancellation records include elapsed time. ReAct protocol
  errors and max-step exhaustion are terminally recorded in ticks rather than
  silently consuming the cycle budget.

## LLM transport observability contract

- The compatible string-returning executor API remains intact, while every call
  exposes a structured metrics snapshot with request ID, model, attempts,
  latency, finish reason, tool-call count, provider usage when available, and
  terminal error state.
- If a provider emits multiple `execute_skill` calls in one response, their
  action arrays are merged in source order. Malformed multi-call output is
  surfaced to the protocol parser instead of silently discarding later calls.
- Invalid structured output is redacted, bounded, persisted as a protocol-error
  tick, and made visible to the next reasoning step for self-correction.

## Dual tool transport contract

- `wrapper` remains the default and preserves QWB/Qwen plus every legacy
  `execute_skill` payload. No migration is required for existing settings.
- `native` exports visible registry guards as typed OpenAI function schemas;
  canonical dotted skill names are encoded into stable provider-safe names and
  resolved before the existing RBAC/validation/execution layer.
- `hybrid` exposes a bounded hot path natively while retaining the wrapper for
  the complete dynamic catalogue and dependency-aware multi-action plans.
- Native export shares the exact wrapper visibility checks, fails when the
  configured tool count is exceeded, refreshes dynamically each ReAct step, and
  accepts ordinary no-tool assistant text as a terminal cycle response.

## Trace correlation contract

- Every main ReAct wakeup starts an async-context trace with a stable ID. Child
  action tasks inherit it automatically through `ContextVar`, so models do not
  need to copy correlation parameters between skills.
- The trace is attached to LLM metrics, ticks, every action-journal event,
  verification runs, plan transitions, and task commits. Action plans also infer
  task IDs from skill parameters for direct task-to-trace lookup.
- Reset tokens restore any parent trace after the cycle and prevent correlation
  state leaking into later Heartbeats or unrelated background work.

## Verification contract

- `run_coding_verification` executes bounded standard profiles inside the task
  worktree without accepting arbitrary shell text.
- Automatic detection supports Git diff checks and Python, Node, Rust, Go, .NET,
  Maven, and Gradle projects; profiles may also be selected explicitly.
- Output is tail-bounded, timeouts terminate the full process tree, and every run
  is persisted with pass/fail/stale/cancelled/interrupted state.
- A successful run records HEAD plus a SHA-256 fingerprint of tracked changes and
  untracked files. Conventional Python caches are the only excluded artifacts.
- A workspace mutation during or after verification invalidates the result.
- Task commits require a current successful fingerprint by default. An explicit
  bypass remains available for justified non-executable changes and is recorded.
- Repositories may declare `.jawl/verification.json` with version, built-in
  profile names, timeout, and stop-on-failure behavior. Unknown fields,
  environment overrides, and arbitrary commands are rejected; the policy hash
  is stored with each run.

## Context acquisition contract

- `read_file_range` returns exact numbered lines plus a whole-file SHA-256 without
  loading irrelevant file content into the model context.
- `search_repository` provides globally bounded literal/regex results, Unicode
  columns, nearby lines, glob filters, and a Python fallback when ripgrep is absent.
- `get_repository_map` provides a bounded map of files, line counts, signatures,
  and symbols across common programming languages without requiring prior indexing.
- Lightweight maps complement the persistent Code Graph: use the map for immediate
  navigation and the graph for semantic search and dependency history.

## Diff review contract

- `get_coding_workspace_diff` returns a paged task-wide or single-file unified
  diff, the exact workspace fingerprint, and tracked/untracked file metadata.
- Diff text is bounded and common credential forms are redacted before it enters
  model context. Untracked previews are read with a hard byte bound, including
  when an accidentally large artifact appears in the worktree.
- Tracked Git stdout/stderr is drained concurrently but retained only up to hard
  byte limits, so a huge single-file diff cannot be buffered fully before the
  character-level response bound is applied.
- A truncated task-wide diff or file-list page is only a change index. The agent
  must page through every affected file before verification; staged and unstaged
  views remain separately addressable.
- The normal coding loop is map -> search -> numbered range -> checked patch ->
  exact per-file diff -> deterministic verification -> verified-state commit.

## Capability benchmark contract

- `benchmarks/coding_agent/manifest.json` maps critical coding properties to
  deterministic integration tests rather than treating the full suite as an
  opaque pass/fail number.
- The benchmark runner isolates each capability, records timings, test counts,
  the Git revision, and failure tails in a machine-readable JSON report.
- Safety and correctness capabilities require a 100% pass rate. Model-level
  task quality, token efficiency, and patch grading are a separate evaluation
  layer and must not dilute this infrastructure gate.

## Fixed-repository patch evaluation contract

- `benchmarks/coding_tasks/manifest.json` defines versioned, reproducible tasks
  with a clean visible repository, external hidden tests, an allowed-file set,
  and a changed-line budget.
- Candidate output is a standard patch per task. The evaluator initializes a
  temporary Git repository, applies the patch, and grades public regressions,
  hidden behavior, scope precision, and patch economy independently.
- Tracked and untracked changes both count toward scope and economy. Reports
  include patch/diff hashes, timings, bounded failure tails, and optional model
  trace metrics without exposing hidden tests to the candidate workspace.
- Reference patches validate the grader only. Comparative model claims require
  a live JAWL driver and equivalent baselines feeding their patches and token,
  tool-call, retry, and wall-time metrics into this same evaluator.
- `benchmarks/coding_tasks/drive_jawl.py` provides that live JAWL path without
  starting Telegram, desktop automation, or the production Heartbeat. Each task
  receives a temporary Host OS root, candidate-visible repository, coding-only
  skill registry, in-memory ticks, real `ReactLoop`, selected tool transport,
  and OpenAI-compatible client.
- The driver requires the exact benchmark task ID, extracts the managed branch
  against its recorded base commit, and gates lifecycle compliance separately
  from hidden-test patch quality. It records time, steps, estimated tokens, and
  provider metrics plus bounded protocol/action diagnostics, but never raw
  chain-of-thought or the API key.
- A live report is evidence for one endpoint/model/configuration only. Claims of
  parity or superiority require versioned runs against declared baselines under
  the same task, timeout, and grading contract.
