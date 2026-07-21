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
`fail_closed=True` when constructing `LifecycleHooks` for strict policy. Only a
pre-tool hook can deny execution. Observational hooks cannot rewrite a tool
result.

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
