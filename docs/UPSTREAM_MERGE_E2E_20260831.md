# Upstream merge and QWB E2E evidence — 2026-08-31

## Integrated revisions

- Upstream JAWL base: `4278dc8` (`upstream/main`, v0.17.0 update).
- Merge commit: `6d8b959`.
- Web/coding integration follow-up: `9a5352b`.
- Existing provider-neutral LLM contract, Durable Goal, coding tools, MCP/debug
  integration, memory formats and multi-instance support were retained.
- QWB's active model identifier is `qwen3.8-max`; the former preview name is
  accepted by QWB only as a compatibility alias.

## Automated verification

QWB (`G:\AI\QWB-JAWL`):

```text
npm test
58 passed, 0 failed
```

JAWL provider/executor focused suites:

```text
64 passed, 0 failed
```

Complete JAWL suite after committing all source changes:

```text
1631 passed, 13 skipped in 144.58s
```

The 13 skips are opt-in live integrations (external debuggers, external MCP,
live providers and live Telegram), not failures. The web ownership contract
also passed with the tracked tree clean.

## Live QWB/JAWL verification

The bridge reported three non-expired accounts and `qwen3.8-max`. A direct
request correctly classified Qwen Web's anti-bot response as
`upstream_waf_challenge` instead of consuming account retries. Refreshing the
sanitized WAF cookie file alone did not satisfy the browser-bound challenge, so
the live run used the supported CDP transport with the authenticated Edge
profile.

The bridge-level JAWL request returned one valid `execute_skill` call with all
required fields and no leaked private transport envelope.

The production JAWL provider stack then ran
`tests/integration/src/l3/llm/test_live_qwb_goal.py` against the live bridge:

```text
4 passed in 36.93s
```

This covers:

1. public health/account diagnostics without token disclosure;
2. two-turn server-side lane continuity and thinking transport;
3. strict JAWL JSON action-plan parsing;
4. a real red-to-green coding task, Durable Goal checkpoint reload,
   verification evidence and successful Goal completion.

During this run a CDP-only continuity defect was found and fixed in QWB: CDP
lanes no longer require a raw `pinnedToken`, because authentication belongs to
the browser session. The focused live continuity test passed after restart, and
the complete four-test live suite passed in one subsequent run.

## WAF/session operating note

`npm run sync:waf` copies only sanitized non-token `chat.qwen.ai` cookies from
the running CDP browser. Cookie values and account JWTs must never be printed or
committed. If Qwen binds a challenge to the browser fingerprint, cookie refresh
alone is insufficient; keep the authenticated Edge CDP profile open and run QWB
with `QWB_TRANSPORT=cdp`.
