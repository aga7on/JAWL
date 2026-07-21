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
| Atomic editing | Strong | SHA-checked exact-match patches plus parser-guarded whole-definition replacement with dual file/symbol hashes, whole-file reparse, atomic writes, and reversible checkpoints | No project-wide transactional rename/refactor primitive |
| Task isolation | Strong | Persistent branch/worktree per task, dirty-base guard, recovery stash | No automatic branch publication or merge conflict assistant |
| Context navigation | Strong | Bounded map/search/range reads, syntax-aware occurrences, directed local dependency slices, and allowlisted lifecycle-managed incremental LSP sessions with document sync, process/document LRUs, explicit reset, and zero-index fallback | Workspace-wide out-of-band edits rely on server file watching or explicit session reset |
| Dynamic context | Good | Hard per-turn budget, task/event-routed skill namespaces, compact omitted-namespace index, exact signatures through `SkillCatalog`, newest-tick and current-trigger retention | Needs organic QWB latency/token validation and learned relevance ranking |
| Diff review | Strong | Per-file/page unified diff, streaming tracked-output cap, untracked preview bound, secret redaction, stable hunk hashes, add/delete counts, parser-backed symbol overlap, and explicit incomplete-analysis state | Review acknowledgement is not yet durable or bound to the exact commit state |
| Verification | Strong | Detected allowlisted profiles, hashed repository policy, process-tree timeout, exact-state fingerprint commit gate | No flaky-test classification |
| Action scheduling | Strong | Sequential default, explicit dependencies/parallel groups, shared resource locks, durable requirement-level step graph, automatic failure signals, and exact-state revision of unfinished work | Replanning strategy quality is not yet scored on live tasks |
| Delegated work | Strong | Bounded durable status registry, redacted summaries, exact-revision parent-step binding, restart reconciliation, exact-handle cancel, shutdown draining, identity-bound reports, report hashing, workspace/verification-gated acceptance, and post-persistence terminal events | Delegation strategy and result quality still need model-level comparative scoring |
| Lifecycle policy | Strong | One ordered bounded registry covers tools, actual context compaction, graceful stop, and delegated-work success/error/cancel; only tool and delegation preflight can deny; passive EventBus observations and shell-free user/repository profiles preserve compatibility | No startup/session boundary hook; delegated result reconciliation still belongs to Swarm |
| Event steering | Strong | Configurable interrupt/defer/append policy; deferred urgent events preserve in-flight provider responses, skip stale actions, persist a steer tick, and become the next primary trigger; priority-bounded sleep/realtime queues explicitly coalesce only noisy state events and expose overflow without payload leakage | No interactive mid-generation provider steering; safe boundary waits for the active response |
| Interruption recovery | Good | Durable action lifecycle, restart classification, persistent worktree state | Recovery is inspect-first but not yet an automatic reconciliation state machine |
| Transactional rewind | Strong | Exact workspace/index snapshot, plan revision, append-only tick timeline branch, optimistic guard, automatic forward checkpoint and compensation | Does not rewind vector/graph stores or external side effects by design |
| Command isolation | Strong | Disabled-by-default task runner, shell-free argv, exact-state guard, host pre-approval, optional Docker/Podman capability/network/resource isolation, exact named toolchain profiles, and expiring one-shot local approvals bound to executable/runtime policy | Approval is local CLI rather than a pushed Telegram/desktop prompt; host backend remains authorization rather than OS containment |
| LLM protocol | Good | Compatible wrapper plus bounded native/hybrid schema export, multiple-call merge, noisy Qwen payload recovery, bounded transient retries, configurable Thinking policy, persisted failures, terminal step-limit record | QWB exposes only a Boolean Thinking switch, not a token/time budget; adaptive per-task policy still needs measured validation |
| Telemetry | Good | Async-safe cycle trace links LLM calls, ticks, actions, plans, verification and commits; request/action timing and usage snapshots | No cost rollup or dashboard export |
| Evaluation | Strong substrate | Deterministic capability gate, fixed hidden-test repositories, isolated real-ReAct driver, recorded QWB calibrations, quota-free external-command preflight, cryptographic task/grader contracts, and fail-closed cross-agent comparison | No equivalent external-agent live baseline has been executed yet; the installed Codex CLI run would consume account quota |
| Planning | Strong | Persistent task-local requirements, dependency steps, revision guards, evidence history, automatic failure/replan state, objective-preserving unfinished-graph revisions, and commit gate | No automatic plan synthesis/revision quality grader |

## Priority order

1. Validate `first_step` Thinking plus adaptive context in the next organic or
   deliberately justified live run; avoid repetitive account-consuming
   calibration.
2. Add pushed Telegram/desktop approval notifications and reusable named
   container-image sets on top of the local one-shot approval CLI.
3. Where a provider supports it, add true mid-generation steering without
   reintroducing downstream cancellation of completed Thinking work.
4. Execute preflighted external CLI baselines through `drive_cli.py` before making
   comparative performance claims; it holds candidate inputs, patch limits,
   timeout handling, and grading constant. Adversarial hidden-test isolation
   still requires the candidate CLI's sandbox or an external container.
5. Persist accepted hunk-review evidence against the exact workspace fingerprint
   and require it at the final commit gate without breaking legacy ad-hoc flows.

## Current reference architecture comparison

The target is a defensible capability envelope, not feature-name parity. Current
official documentation shows recurring patterns in leading coding agents:

| Pattern | Current reference implementations | JAWL fork position |
| --- | --- | --- |
| Parallel delegated work | [Codex subagents](https://developers.openai.com/codex/subagents/), [Claude Code subagents](https://code.claude.com/docs/en/sub-agents) | Swarm roles have durable inspect/cancel/restart state and exact-revision parent-plan binding; reports require hash/fingerprint/verification-gated main-agent acceptance before a step completes |
| Lifecycle control | [Claude Code hooks](https://code.claude.com/docs/en/hooks), [GitHub Copilot hooks](https://docs.github.com/en/copilot/concepts/agents/hooks) | One stable in-process and declarative policy layer spans tool actions, context compaction, shutdown, and Swarm delegation, with denial restricted to safe preflight boundaries |
| Recovery and rewind | [Claude Code checkpointing](https://code.claude.com/docs/en/checkpointing), [Gemini CLI checkpointing](https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/commands.md) | Transactional task-workspace/index/plan rewind plus append-only context timelines, automatic forward recovery, and compensation are implemented; vector/graph memory and external side effects intentionally remain outside rewind scope |
| Enforced execution boundary | [Gemini CLI sandboxing](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/sandbox.md) | Task argv execution is disabled by default; Docker/Podman adds a real boundary with no host fallback, while exact one-shot approvals and named profiles constrain authorized host/container runs |
| Repository context economy | [Aider repository map](https://aider.chat/docs/repomap.html) | Bounded map/search/LSP/dependency tools plus the new dynamic budget are competitive substrate; relevance is deterministic rather than learned |
| Automatic verification | [Aider lint/test integration](https://aider.chat/docs/usage/lint-test.html) | Exact-state verification and commit gates are stronger than a best-effort post-edit test loop |

## Production context measurement

The captured production prompt at `G:\AI\jawl-4\logs\prompts\main_prompt.md`
was 99,520 characters (approximately 24,880 tokens): 10,215 characters of
system instructions and 89,229 characters of dynamic user context. The largest
repeat contributors were the full skill catalogue (23,343 characters),
hypothesis clusters (11,531), and roughly 40,000 characters of recent ticks.
This confirms that the repeated 25–28k-token requests were primarily a service
projection problem, not missing conversational memory.

The fork now bounds only that transient projection. SQL/vector/graph records,
events, notes, plans, and ticks remain durable at rest. The default fork policy
caps dynamic context at 60,000 characters, routes likely namespaces from the
current event/task/previous action, and exposes exact omitted signatures through
`SkillCatalog.search_skills`. The capability gate also covers the hard-limit
case where skills and heartbeat alone exceed the target.

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
