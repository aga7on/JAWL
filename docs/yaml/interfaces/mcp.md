# MCP interface

The MCP interface connects JAWL to operator-selected Model Context Protocol
servers without copying every remote tool schema into every LLM request. It is
disabled by default.

## Configuration

- `enabled`: enables the interface.
- `startup_timeout_sec`: connection and initialization deadline per server.
- `request_timeout_sec`: protocol request deadline. Tool calls are never
  automatically retried after dispatch because their external outcome may be
  unknown.
- `max_catalog_items`: hard pagination bound per remote catalogue.
- `max_result_chars`: bound for projected model-facing results.
- `max_binary_bytes`: maximum decoded image/audio/blob artifact size.
- `servers`: explicit server definitions. Disabled entries are ignored.

A `stdio` server uses an exact `command` plus `args`; no shell is involved.
`cwd` must remain below the JAWL root. The subprocess receives only the MCP SDK
safe baseline environment and names explicitly listed in `env_passthrough`.

A `streamable_http` server uses `url`. Remote endpoints require HTTPS; plain
HTTP is accepted only for localhost. Credentials are read from named environment
variables via `bearer_token_env` or `headers_from_env`, never embedded in YAML.
Redirects are disabled.

`allowed_tools` is an exact operator authorization list. An empty list permits
discovery but prohibits calls. Resources and prompts have separate
`resources_enabled` and `prompts_enabled` switches and are off by default.

## Model workflow and trust boundary

The passive context contains connection health and catalogue counts only. The
agent searches on demand with `MCPTools.search_tools`, receives the current exact
JSON schema plus its SHA-256, and must supply that hash to
`MCPTools.call_tool`. JAWL refreshes the catalogue immediately before dispatch;
a changed schema fails closed. Inputs and structured outputs are validated
locally against the advertised JSON Schema.

All remote descriptions, resources, prompts, and results are untrusted data.
Text is bounded and redacted, binary data is size-checked and stored under the
JAWL sandbox, and lifecycle health never retains call parameters or results.
Cross-server forwarding of secrets is not implied by enabling MCP and must not
be performed without explicit user authorization.

## x64dbg lifecycle workflow

The local `x64dbg-mcp` adapter can expose `LaunchDebuggee` and `AttachProcess`
in addition to the debugger inspection and mutation APIs. These two tools
remove the ambiguous GUI/bootstrap step:

1. Search the exact `x64dbg-mcp` server once for `LaunchDebuggee` or
   `AttachProcess`.
2. Call the returned allowlisted tool with its current schema hash.
3. Confirm `mcp_ready=true`, then call `GetState`; launch success alone is not
   evidence that the debuggee is paused at the intended location.
4. Persist the tool/schema and current target or PID in the Goal Task Ledger.
5. Use the returned state to set breakpoints, run, pause, step, and inspect.

`LaunchDebuggee` accepts only an existing absolute PE path and can select
x32dbg/x64dbg from the PE machine type. `AttachProcess` requires an explicit
process ID and architecture. Both open the visible debugger application; the
Edge/Qwen browser is unrelated to this MCP connection.
