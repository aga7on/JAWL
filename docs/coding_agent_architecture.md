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

## Delegated-work control contract

- Swarm work is persisted before its `asyncio.Task` is created. The registry
  contains only bounded, redacted summaries and lifecycle metadata.
- A new process classifies unfinished records as `interrupted`; it does not
  claim that lost task handles are still running or automatically replay work.
- Inspection and cancellation are normal guarded skills. Cancellation applies
  only to an exact active handle in the current process.
- Worker report IDs and roles must match the runtime-assigned identity.
- The Swarm manager is a lifecycle component, so graceful shutdown cancels and
  awaits workers before shared transports close.
- A worker may bind to one exact task-plan revision and step. Binding moves the
  step to `in_progress` only after dependency and active-worker checks.
- Reports are proposals, not completion authority. Acceptance is a main-agent
  operation guarded by report hash, report location, result-time workspace
  fingerprint, current fingerprint, plan revision, dependencies, and exact-state
  verification.
- Completion/failure events are published only after the parent result is
  durable. Restarted workers reconcile to `interrupted` on both registry and
  parent-plan sides.

## Declarative lifecycle automation contract

- One hook registry spans tool execution, real context compaction, graceful
  system stop, and Swarm delegation; coding support does not introduce a
  parallel lifecycle subsystem.
- Only `pre_tool_use` and `pre_delegation` are policy gates. Compaction,
  shutdown, and terminal phases are observational, so an extension cannot veto
  context assembly or prevent resource cleanup.
- Lifecycle EventBus records remain passive and outside Heartbeat routing.
  Generic operation names (`Context.compaction`, `System.stop`, and
  `Swarm.delegate`) preserve the existing `tool_name` compatibility field.

- Declarative command hooks are disabled by default and use exact argv without
  a shell, interpolation, or repository-provided environment variables.
- User profiles are resolved and executable-hashed at startup. A managed
  repository can only select profiles explicitly marked `scope: repository`;
  `.jawl/hooks.json` never supplies commands or arguments.
- Repository-selected hooks run only in the resolved task worktree. Hook stdin
  contains bounded action metadata and parameter names, not parameter values or
  outcome messages, and inherits only a minimal environment allowlist.
- Output is drained under a retained bound and redacted. Timeout or cancellation
  terminates the process tree; a changed executable fails before launch.
- The adapter is an authorization boundary, not containment. Commands needing
  untrusted-code isolation remain subject to the separate container policy.

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
- `inspect_coding_symbol` returns one unique parser-backed definition plus exact
  file and symbol hashes. Python supports qualified nested identities; supported
  non-Python languages use tree-sitter. Structural write boundaries never use
  lexical fallback.
- `replace_coding_symbol` requires both hashes, preserves the definition's current
  indentation/newline style, reparses the complete result, and requires the same
  unique qualified identity and node kind before creating a normal reversible
  checkpoint and atomically writing the file.

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
- `resolve_code_symbol` optionally retains an installed allowlisted language
  server over bounded stdio JSON-RPC for project-resolved definitions or
  references. Sessions are keyed by validated project root and exact command,
  serialize requests, synchronize changed open documents, and restart once after
  a broken retained connection. It auto-discovers a nearby project manifest,
  filters locations to that validated root, handles UTF-16 LSP columns, and
  bounds messages/results/request time.
- Retained LSP processes and their open-document sets are independently capped
  by idle LRUs (`didClose` is emitted for document eviction), expose payload-free
  status, support an explicit safe reset after broad out-of-band edits, and
  participate in system startup/shutdown. Eviction, timeout, cancellation, and
  shutdown close the protocol and terminate the process tree; syntax fallback
  remains available.
- `preview_coding_symbol_rename` requests `prepareRename` and `rename` from an
  allowlisted retained server, then normalizes only bounded textual
  `WorkspaceEdit` operations inside the exact managed task workspace. UTF-16
  positions, duplicate edits, overlaps, resource operations, external paths,
  non-UTF-8 files, and transaction-size limits are handled fail-closed; lexical
  rename fallback is deliberately forbidden.
- `apply_coding_symbol_rename` recomputes the server edit and requires both the
  original workspace fingerprint and a canonical preview hash. It checkpoints
  every affected file before atomic writes, verifies each file immediately
  before its write, rolls back a partial transaction, and preserves rather than
  overwrites any concurrent external change while reporting recovery checkpoint
  IDs.
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
- Failed verification, blocked work, and failed/cancelled/interrupted or rejected
  delegations create bounded idempotent `replan_required` evidence. A plan with
  unresolved replanning evidence cannot pass the normal commit gate.
- `revise_coding_task_plan` atomically rebuilds only the unfinished graph under
  an exact plan revision and workspace fingerprint. It cannot change the
  objective or requirements, remove or rewrite completed/in-progress/actively
  delegated steps, or silently retain blocked steps. Reopened evidence and a
  bounded revision audit remain durable.
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

## Transactional recovery contract

- `create_coding_recovery_checkpoint` snapshots the managed task worktree,
  staging index, current HEAD, durable coding plan, active tick timeline cursor,
  and trace correlation without changing the task branch or working files.
- Workspace fingerprints distinguish staged from unstaged diffs and include
  untracked files. Creation and rewind accept exact fingerprint guards, so work
  produced after inspection cannot be overwritten accidentally.
- Git objects are retained under task-scoped `refs/jawl/checkpoints/...` refs.
  Rewinds affect only the isolated task worktree; they never reset the user's
  base repository checkout or delete physical episodic-memory rows.
- Before mutation, every rewind persists an automatic forward checkpoint. If
  workspace, plan, or context switching fails, compensation restores that exact
  forward state and leaves its checkpoint available for manual recovery.
- Episodic context is append-only. A rewind creates and activates a child
  timeline anchored at the saved tick cursor; abandoned futures remain in SQL
  and a later forward-checkpoint rewind can make the prior future visible again.
- Successful rewinds append an explanatory marker tick. Failure to append that
  marker is reported as a warning after the already durable transaction rather
  than pretending the workspace was rolled back.

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
- Qwen web may leak tool-format deliberation plus trial JSON fragments into the
  answer channel. After strict parsing fails, the compatibility parser examines
  at most 200 JSON starts, prefers the last structurally complete non-empty JAWL
  action payload, and unwraps OpenAI-style `execute_skill.arguments`. Embedded
  empty-action examples cannot terminate a cycle. Protocol ticks retain bounded
  redacted head and tail excerpts for reproducible diagnosis.
- Main ReAct inference uses three total attempts and two timeout attempts.
  Provider 5xx and connection errors receive bounded exponential backoff; this
  retries only the side-effect-free model request, never an already executed
  local action plan. Final failure metrics distinguish upstream unavailability
  from protocol, authentication, rate-limit, timeout, and cancellation states.
- The current OpenAI-compatible/QWB call is unary: JAWL submits one complete
  request and receives one completed response. QWB's internal Qwen SSE is a
  server-to-bridge stream, not a bidirectional request channel exposed to JAWL.
  Consequently true mid-generation input steering cannot be implemented in the
  framework alone. The `defer` policy deliberately preserves the active
  Thinking request and yields at the first safe response boundary; starting a
  second request or cancelling the first would discard work rather than steer
  it. Provider-level steering should be added only after an authenticated,
  request-ID-bound, acknowledgement-producing upstream extension exists.

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
  is persisted with pass/fail/flaky/stale/cancelled/interrupted state.
- Optional `stability_runs` from 1 through 3 repeats only test-bearing profiles;
  deterministic diff/compile checks remain single-run. Every completed attempt
  is durably summarized with outcome, timing, truncation flags, and redacted
  output hashes. The first failure remains as bounded diagnostic output.
- Mixed pass/fail outcomes classify the check and whole run as `flaky`. This is
  deliberately not a success: it requests plan revision, never becomes current
  verification, and cannot satisfy the verified-state commit gate. Uniform
  reruns become `stable_pass` or `stable_fail` evidence.
- Optional `test_selection: "affected"` narrows only pytest. It indexes a bounded
  Python import graph once, walks transitive dependents from the exact tracked
  and untracked change set, and passes only reached pytest files as shell-free
  argv targets. The default remains `full`.
- Affected selection fails closed to the full pytest suite for renames, deleted
  tests, non-Python or configuration changes, ambiguous modules, parse/read/size
  failures, index/selection limits, or when no affected test can be proven. The
  requested/effective mode, reason, bounded paths, graph counts, and decision
  hash are persisted with the exact-state verification run.
- A successful run records HEAD plus a SHA-256 fingerprint of staged changes,
  unstaged changes, and untracked files. Conventional Python caches are the only
  excluded artifacts.
- A workspace mutation during or after verification invalidates the result.
- Task commits require a current successful fingerprint by default. An explicit
  bypass remains available for justified non-executable changes and is recorded.
- Repositories may declare `.jawl/verification.json` with version, built-in
  profile names, timeout, stop-on-failure behavior, bounded stability runs, and
  `full`/`affected` test selection.
  Unknown fields,
  environment overrides, and arbitrary commands are rejected; the policy hash
  is stored with each run.

## Task command execution contract

- `run_coding_command` accepts an argv array, never a shell string, and resolves
  its working directory strictly inside one managed task worktree.
- Every invocation requires an optimistic workspace fingerprint. Output is
  drained with hard byte bounds, secrets are redacted, timeouts and cancellation
  terminate the runtime process tree, and before/after fingerprints expose all
  mutations to the subsequent diff and verification gates.
- The default policy is `disabled`. The `host` backend requires exact executable
  names pre-approved in human configuration and supplies a scrubbed environment;
  it is explicitly not an OS security boundary.
- The optional Docker/Podman backend bind-mounts only the task workspace, invokes
  argv directly, drops all capabilities, enables `no-new-privileges`, and applies
  network, memory, CPU, and PID policy from configuration. No automatic fallback
  to host execution occurs when the runtime or image is unavailable.
- Reusable named container profiles bind an exact OCI image plus network,
  memory, CPU, and PID limits. Command profiles may select one without allowing
  model-side argv or policy interpolation; omitted names preserve the global
  default policy for compatibility.
- Raw `execute_shell_command` remains ROOT-only for compatibility and is excluded
  from the normal coding-agent workflow.
- Optional `required` approval mode persists a pending request outside the task
  worktree. A local operator can approve or deny it through `jawl.py
  --approvals`; approved requests expire and are consumed exactly once.
- A new pending request publishes a passive EventBus record containing only the
  bounded public approval projection. Opt-in lifecycle consumers can display a
  native desktop toast or send it to one configured Telethon/Aiogram chat; the
  event is explicitly excluded from Heartbeat routing to avoid a self-triggered
  ReAct cycle. Delivery failure never broadens authorization or changes request
  state.
- A separate disabled-by-default Telegram decision control accepts only an
  exact 16-hex approval command from one configured numeric chat and actor. The
  connector consumes matching control syntax before state/history/agent event
  routing, delegates TTL and replay enforcement to the same protected one-shot
  store, and publishes only the bounded public decision after persistence. The
  decision wake event is routed to Heartbeat; acknowledgement delivery remains
  best-effort and cannot roll back or hide the persisted authorization state.
- The approval subject covers task, argv, workspace fingerprint, cwd, timeout,
  backend, executable/runtime hash, selected profile identity, and the resolved
  container image/network/resource policy. A renamed or modified profile never
  degrades to a broader authorization.
- Named command profiles expose exact user-declared toolchain argv without model
  interpolation and inherit every backend, fingerprint, approval, timeout, and
  output bound of the ordinary task command path.

## Startup coding reconciliation contract

- The append-only action journal distinguishes a prior-session plan from one in
  the current process and derives only bounded task IDs plus actions that have a
  durable start record but no finish/cancel/block record. Raw parameters never
  enter recovery events.
- A lifecycle-managed startup reconciler projects each interrupted coding action
  plan into its managed workspace registry together with the exact current HEAD
  and workspace fingerprint. It never invokes or replays a tool.
- An uncertain started action records `inspection_required`, adds one idempotent
  `action_interruption` reason to the durable coding plan, and therefore blocks
  commit until the remaining graph is deliberately revised. An interruption
  before any action started records `resume_required` without manufacturing a
  replanning failure.
- The journal receives a terminal `plan_reconciled` marker only after every
  referenced task was projected successfully. Repeated startup is idempotent;
  partial infrastructure failure remains `interrupted` for a future retry.
- A bounded `CODING_ACTION_RECOVERY_REQUIRED` event wakes the existing Heartbeat
  with task/plan IDs, state, counts, and fingerprints but no action arguments.
  The agent must inspect workspace, plan, diff, and journal evidence before it
  continues.

## Bounded event steering contract

- Heartbeat sleep events, active ReAct realtime events, and safe-boundary steer
  evidence share a priority-aware bounded buffer instead of silently truncating
  through `deque(maxlen=20)` or growing for the life of a long provider call.
- Only explicit noisy state notifications coalesce within a configured time
  window. User messages and requests remain individual events; repeated file,
  dashboard, tick, and chat-action notifications retain newest state, bounded
  payload samples, and an aggregate count.
- Capacity pressure evicts the oldest lowest-priority intent before a newer
  equal/higher-priority event. Every eviction/drop becomes a payload-bounded
  `EVENT_QUEUE_OVERFLOW` summary, which survives transfer from Heartbeat to
  ReAct and points to durable connector history for full payload recovery.
- `get_event_queue_status` reports sizes, names, levels, coalescing, overflow,
  primary wake state, and deferred/active flags without exposing event payloads.
- Safe `defer` behavior is unchanged: an in-flight Qwen response completes, its
  now-stale actions are skipped, and the highest-priority/newest equal-priority
  request starts the next cycle with the other retained intents as context.

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
- Every returned unified hunk has bounded add/delete counts, a stable hash of the
  reviewed redacted text, and up to three overlapping parser-backed definitions.
  The aggregate hunk hash is stable for the same review response without storing
  raw hunk text twice.
- `hunk_analysis_complete` is false whenever diff collection/text, file paging,
  hunk paging, or the final hunk is incomplete. It is an explicit instruction to
  request narrower/per-file output, not proof that the workspace was reviewed.
- `accept_coding_workspace_diff_review` recomputes the requested unstaged diff
  and accepts only a complete response whose reviewed-text hash and workspace
  fingerprint match. Complete per-file reviews accumulate under that one exact
  fingerprint; any workspace change makes the entire prior set stale.
- Durable review state stores only bounded hashes, file coverage, timestamps,
  and trace correlation, never raw diff text. Status output reports current,
  covered, and missing files without expanding the persisted scope records. A
  successful commit retains a compact aggregate evidence hash and its exact
  pre-commit workspace fingerprint next to the commit identity.
- Plans created by this fork require current review coverage at final commit.
  Plans persisted before this contract and ad-hoc workspaces retain legacy
  behavior; an explicit `require_diff_review=false` bypass remains available for
  intermediate or unusually large changes and is recorded in commit metadata.
- The normal coding loop is map -> search -> numbered range or structural symbol
  inspection -> checked patch/replacement -> exact per-file diff -> deterministic
  verification -> verified-state commit.

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
  from hidden-test patch quality. Planned runs must also retain non-bypassed diff
  review evidence for the extracted commit. It records time, steps, estimated
  tokens, and provider metrics plus bounded protocol/action diagnostics, but
  never raw chain-of-thought or the API key.
- A live report is evidence for one endpoint/model/configuration only. Claims of
  parity or superiority require versioned runs against declared baselines under
  the same task, timeout, and grading contract.
- Every new report fingerprints the selected public task definitions, visible
  fixtures, hidden oracle files, and grader source. The fingerprint reveals no
  oracle contents and makes comparisons fail closed if any evaluated contract
  component or task order differs.
- External commands can be resolved and fingerprinted in a quota-free preflight
  that never starts the candidate. The report records executable and command
  template hashes rather than raw paths or arguments.
- `compare_reports.py` ranks only patch quality under an exact shared contract.
  JAWL's verified commit lifecycle and an external CLI's process/patch lifecycle
  remain separately labelled, wall time is indicative, and absent token
  metering remains unknown rather than being silently counted as zero.
