# Lifecycle and Proactivity (`system`)

Manages the agent's operating rhythm (Heartbeat) and its proactivity.

* **`heartbeat_interval`**: Base sleep interval (in seconds) in the absence of external incoming events. For example, `600` means that the agent is guaranteed to wake up once every 10 minutes to verify its motivators (Drives) and run background routines.
* **`continuous_cycle`**: `true` / `false`. If `true`, the agent does not sleep at all. As soon as it concludes one ReAct reasoning loop, it instantly starts the next one. **Warning:** burns API tokens at an astronomical rate.
* **`proactive_guidance`**: `true` / `false`. Injects an insistent prompt instruction into scheduled Heartbeat wakeups, urging the agent to find productive tasks (refactoring, information harvesting, database maintenance) in the absence of direct user commands.

Active-cycle interruption behavior is configured by
`system.event_acceleration.active_cycle_policy`; see
[`event_acceleration.md`](event_acceleration.md).

## Empty-cycle backoff

Production logs may show a periodic Heartbeat repeatedly returning no actions
while paying for the same large context. JAWL backs off only after a ReAct
cycle is proven to have completed without executing any action:

```yaml
system:
  idle_heartbeat_backoff:
    enabled: true
    no_op_threshold: 2
    max_multiplier: 8
    max_interval_sec: 3300
```

The interval grows exponentially after `no_op_threshold` consecutive empty
timer cycles and resets on an action, failure, cancellation, or external
event. `max_interval_sec` defaults to 55 minutes, below QWB's one-hour chat
expiry, so the same Qwen branch stays warm. Telegram, user, file, and other
events are never delayed by this backoff. `continuous_cycle: true` disables it.
