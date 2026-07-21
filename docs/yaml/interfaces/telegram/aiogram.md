# Telegram Configuration (Aiogram)

The `aiogram` interface is designed for the classic Bot API. It is ideal if the agent needs to act as a classic support assistant, bot manager, or chat moderator.

## Authorization
Create a bot with [@BotFather](https://t.me/BotFather), copy the token, and add it to the `.env` file:
* `AIOGRAM_BOT_TOKEN="123456789:ABCDefgh..."`

Unlike Telethon, bots do not have access to full chat histories. The agent will only "see" messages that arrived after its startup.

## Parameters (`telegram.aiogram`)

* **`enabled`**: `true` / `false`.
* **`recent_chats_limit`**: Maximum number of active chats displayed on the dashboard (L0 State). If the bot is added to 500 groups, this limit prevents the list from burning your entire LLM token quota.
* **`coding_approval_chat_id`**: Optional chat ID or channel username for passive
  coding-approval pushes. The Bot API receives only the bounded public record
  with a redacted argv preview. Without remote mode, decisions remain in the
  local operator CLI.
* **`coding_approval_remote_decisions`**: Disabled by default. When enabled,
  exact `/jawl_approve <16-hex-id>` and `/jawl_deny <16-hex-id>` messages are
  intercepted before the bot updates chat state or publishes an agent-facing
  incoming-message event. A successful decision emits only the bounded public
  approval projection through `CODING_APPROVAL_DECIDED`.
* **`coding_approval_actor_id`**: Exact numeric Telegram user ID authorized to
  decide requests. Remote mode also requires a numeric
  `coding_approval_chat_id`. Wrong-chat and wrong-actor commands cannot mutate
  or inspect the approval; TTL and one-shot replay protection are enforced by
  the same protected store as the local CLI.
