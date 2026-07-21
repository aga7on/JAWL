# Event Acceleration (Wakeup Multipliers)

The JAWL architecture is based on saving computing resources: in the absence of active tasks, the agent resides in an idle sleep state (Heartbeat). The `heartbeat_interval` parameter defines the maximum time (in seconds) between scheduled wakeups.
The `Event Acceleration` system defines how aggressively incoming external events (triggers) reduce the current sleep time, forcing the agent to wake up ahead of schedule.

## Multiplier Mathematics

Each event priority level (from `CRITICAL` to `BACKGROUND`) possesses a corresponding decimal multiplier.
**Formula:** `New_Sleep_Time_Remaining = Current_Sleep_Time_Remaining * Event_Level_Multiplier`

### Calculation Example:
Assume the `heartbeat_interval` is set to 600 seconds (10 minutes).
The agent went to sleep. 2 minutes passed, 8 minutes remaining.
An event of `MEDIUM` level arrives (for example, a group mention in Telegram) with a `0.6` multiplier.
- *Result:* 8 minutes * 0.6 = 4.8 minutes. The agent will wake up in 4.8 minutes instead of 8. The remaining sleep time was accelerated (reduced) by 40%.

If a `LOW` priority event (multiplier `0.8`) arrives immediately after, the remaining 4.8 minutes is multiplied by 0.8, leaving 3.84 minutes.

### Active-cycle policy

`active_cycle_policy` controls a near-zero multiplier received while ReAct is
already running:

- `interrupt`: legacy behavior; cancel the current task immediately.
- `defer`: keep the provider request alive, yield at the next safe ReAct
  boundary before executing a stale answer, and immediately start the queued
  event as the next primary trigger.
- `append`: add the event to the current cycle's realtime context without
  cancelling or forcing a new cycle.

The coding fork uses `defer`, which avoids downstream cancellation of long Qwen
Thinking responses while retaining urgent-event ordering.

### Bounded event queues

`queue_max_events` bounds both events retained while Heartbeat sleeps and events
appended to an active ReAct cycle. When full, the oldest lowest-priority event is
evicted for an event of equal or greater priority; a lower-priority arrival is
discarded. Neither case is silent: an `EVENT_QUEUE_OVERFLOW` summary preserves
counts by level and event name and directs the agent to connector history for
omitted payloads. `get_event_queue_status` exposes payload-free queue sizes,
coalescing counts, overflow counts, the current primary wake reason, and whether
a deferred wakeup is pending.

Coalescing is opt-in by exact event name through `coalesce_event_names` and is
limited by `coalesce_window_sec`. The retained event carries the newest payload,
the total count, and up to `coalesce_payload_samples` samples. The defaults cover
noisy file/dashboard/tick/chat-action state notifications only. Incoming
Telethon/Aiogram messages, mentions, terminal input, email, and webhook requests
are deliberately not coalesced and remain separate intents.

### CRITICAL Events (Immediate Wakeup)
The `CRITICAL` event level defaults to a `0.0` multiplier.
This is a special system-level priority. If an event multiplier is zero or near-zero (< 0.01), the system behaves differently:
1. The remaining sleep time is reset to zero (wakeup happens instantly).
2. If the agent is active, `active_cycle_policy` determines whether the current
   cycle is interrupted, safely deferred, or only receives appended context.
3. Under `interrupt` or `defer`, the urgent event becomes the primary trigger of
   the replacement cycle.
