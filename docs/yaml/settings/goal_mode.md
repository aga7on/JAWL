# Goal Mode

Goal Mode gives JAWL one durable long-running objective that survives ReAct
cycle boundaries and framework restarts without replaying the full operational
history on every step.

```yaml
system:
  goal_mode:
    enabled: true
    compact_context: true
    compact_max_chars: 24000
    suppress_waiting_heartbeats: true
```

`enabled` registers the lifecycle skills and durable store. Only one goal may
be active at a time.

`compact_context` replaces unrelated volatile cognition with a bounded
projection containing the objective, budget, continuation state, recent
evidence, latest tool result, current event, and relevant skill catalogue.
SOUL and safety instructions remain in the static system message.

`compact_max_chars` is the hard target for the dynamic Goal projection. It does
not truncate the static system instructions.

`suppress_waiting_heartbeats` skips only deterministic timer heartbeats when an
active goal explicitly waits without a due wakeup. User messages, Telegram,
file events, and other external events still wake the agent.

## Lifecycle

The model can create, inspect, resume, complete, block, cancel, or schedule a
wakeup through `GoalSkills`. State is atomically stored in
`src/utils/local/data/agent/goals.json`. A malformed store is preserved and
mutations fail closed instead of overwriting recovery evidence.

Each goal owns a stable QWB `X-Session-Id` lane. QWB can bootstrap the complete
snapshot once and send bounded deltas on later ReAct steps/cycles. A JAWL
restart increments the lane epoch and forces a fresh full bootstrap, so hidden
provider context is never the only source of truth.

## Token budget

An optional positive `token_budget` accounts provider-reported total tokens
when available and falls back to JAWL estimates. Reaching the budget blocks the
goal after the last valid action batch; it does not silently continue spending.

JAWL logs both the local full-snapshot estimate and provider prompt usage. With
QWB delta continuity, the latter represents the smaller prompt actually sent
upstream.

## Coding verification

Set `linked_task_id` when a goal owns a managed coding workspace. The default
`verification_policy: auto` then requires current passing evidence from
`HostOSCodingVerification.run_coding_verification` before completion.

The model follows a verification ladder:

1. Run the smallest test that exercises the changed behavior.
2. Fix and rerun any failed layer.
3. Broaden to the affected module or profile.
4. Run the repository-declared exact-state verification gate.

Not-run, failed, timed-out, interrupted, stale, skipped-only, and flaky results
are never treated as green.
