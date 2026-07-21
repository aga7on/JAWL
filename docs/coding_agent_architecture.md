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
