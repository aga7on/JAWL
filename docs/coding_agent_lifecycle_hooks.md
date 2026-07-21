# Coding agent lifecycle hooks

The fork exposes one deterministic lifecycle policy layer for Main, Swarm, and
Subconscious actions. It is available after L3 assembly as
`container.lifecycle_hooks`.

Supported phases:

* `pre_tool_use`: may return `HookDecision.deny(reason)` or `False` before the
  physical skill runner starts.
* `post_tool_use`: observes successful outcomes.
* `tool_error`: observes failed outcomes and uncaught runner errors.
* `tool_cancelled`: observes cancellation during interruption or shutdown.
* `pre_context_compaction` / `post_context_compaction`: observe an actual
  dynamic-context reduction. They are not emitted when no provider is trimmed.
* `pre_system_stop` / `post_system_stop`: bracket graceful resource shutdown;
  hook failure cannot stop cleanup.
* `pre_delegation`: may deny a Swarm worker before its background task exists.
* `post_delegation`, `delegation_error`, and `delegation_cancelled`: record the
  terminal state of delegated work.

Example:

```python
from src.l3_agent.hooks import HookDecision, HookPhase

async def protect_production(context):
    if context.parameters.get("path", "").startswith("production/"):
        return HookDecision.deny("Production edits require operator approval.")

container.lifecycle_hooks.subscribe(
    HookPhase.PRE_TOOL_USE,
    protect_production,
    priority=100,
)
```

Handlers run by descending priority and then registration order. Each handler
has a timeout. Failures are isolated and fail open by default; a runtime may set
`fail_closed=True` when constructing `LifecycleHooks` for strict policy. Only
`pre_tool_use` and `pre_delegation` can deny execution. Compaction, shutdown,
and terminal hooks are observational and cannot rewrite an outcome.

Decisions and hook failures are added to the durable action journal. The same
lifecycle transitions are published as non-attention EventBus observations and
are intentionally outside `Events.all()`, so they do not wake or interrupt the
Heartbeat/ReAct cycle.

Hook contexts contain raw action parameters so trusted policy can inspect exact
targets. Treat handlers as privileged local code. Do not log or forward the
context without applying the framework's credential redaction. Synchronous
handlers are moved off the event loop, but Python cannot forcibly stop a worker
thread after timeout; long-running hooks should therefore be asynchronous or
use a killable subprocess adapter.

## Declarative command profiles

`system.lifecycle_hooks` can register exact, shell-free argv profiles without a
Python extension. The feature is disabled by default. Executables are resolved
once at startup, hashed, invoked by absolute path, and re-hashed before every
run. Commands receive a minimal environment and bounded JSON metadata on stdin;
parameter values and outcome messages are intentionally omitted.

```yaml
system:
  lifecycle_hooks:
    enabled: true
    fail_closed: true
    handler_timeout_seconds: 15
    command_timeout_seconds: 10
    max_output_chars: 8000
    commands:
      - name: python-tests
        phase: post_tool_use
        argv: [python, -m, pytest, -q]
        tool_patterns: ["HostOSCodingFiles.*"]
        scope: repository
        working_directory: workspace
```

A managed task repository opts into that pre-approved profile with:

```json
{"version": 1, "hooks": ["python-tests"]}
```

The file must be `.jawl/hooks.json` inside the managed worktree. It can select
only names already declared by the user; it cannot provide argv, environment,
timeouts, interpolation, or a framework working directory. Missing task IDs or
manifests simply skip workspace-scoped hooks. Invalid manifests become hook
failures and therefore deny pre-tool execution when `fail_closed` is enabled.

Exit code `0` succeeds. For `pre_tool_use` or `pre_delegation`, the configured
`deny_exit_code` (default `10`) denies the operation without classifying the
hook as broken. Other non-zero codes are failures. Output is drained with a hard retained bound,
redacted before it reaches the action journal, and the whole process tree is
terminated on timeout or cancellation.

These profiles are authorization, not an OS sandbox. A repository profile runs
only because the user has approved the exact command; use the container task
execution policy when the command itself needs filesystem or network isolation.
