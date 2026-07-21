# Host OS Access Levels (`host.os.access_level`)

The agent's access to the file system is controlled by the built-in `Gatekeeper` component. It intercepts all path-related calls, prevents Path Traversal attacks (escaping directory bounds via `../` sequences), and blocks attempts to read or modify `.env` files containing API keys (if the `env_access` parameter is set to `false`).

* **0 (SANDBOX):** Maximum safety. The agent has the right to read and write files strictly inside the `sandbox/` directory.
* **1 (OBSERVER):** Tester mode. The agent can read (Read) the framework's source code, but write (Write) operations are allowed strictly inside the `sandbox/` directory.
* **2 (OPERATOR):** Developer mode. The agent receives read and write privileges inside the entire JAWL project directory. Modifying the system's source code requires an active Deploy Session.
* **3 (ROOT):** Full access. The agent receives read, write, and delete privileges on any files on the host machine (within the bounds of the user who started the script), as well as the right to execute raw shell commands. **It is highly discouraged to activate this level on your primary workstation.**

### Deploy Sessions (Secure Self-Modification)
If the agent possesses `OPERATOR` access level or higher, and the `require_deploy_sessions` parameter is set to `true`, the system protects itself from fatal syntax crashes.
Before modifying system code, the agent must open a "Deploy Session". The system creates a Copy-on-Write backup of the modified files. Upon committing the changes, the framework automatically runs a syntax analyzer and `pytest`. If any tests fail, the agent receives the Traceback error and consumes one retry attempt. If the attempts limit (`deploy_max_retries`) is exhausted, the system automatically triggers a Rollback (restores the backed-up files to their initial state).

# Host OS Limits (`host.os`)

These parameters protect the system prompt from being overloaded by giant directory trees and verbose logs while the agent is operating on the host machine.

* **`framework_tree_depth`**: The depth to which the agent can view the directory tree of the framework itself (JAWL). `1` — root folder only, `2` — root and nested folders, etc.
* **`monitoring_interval_sec`**: Frequency (in seconds) of polling telemetry (CPU/RAM) and file system changes.
* **`file_read_max_chars`**: Character limit when reading files (the `read_file` skill). If a file is larger, it will be truncated.
* **`file_list_limit`**: Maximum number of files/folders displayed when scanning directories.
* **`file_diff_max_chars`**: Character limit for the `git-diff` log injected into the dashboard after file modifications.
* **`top_processes_limit`**: Number of active processes (sorted by memory consumption) displayed in the telemetry block.
* **`workspace_max_opened_files`**: Maximum number of "editor tabs" (files currently held open by the agent in its context).
* **`recent_file_changes_limit`**: How many of the latest file diffs are preserved in memory (MRU cache).
* **`workspace_max_file_chars`**: Maximum size of a file (in characters) that can be held open in the agent's editor tabs.

## Coding command policy

`run_coding_command` never invokes a shell and operates only in a managed task
worktree. It requires the exact fingerprint returned by
`get_coding_workspace_status` or the diff skill, so a command cannot start from
stale inspected state.

When `desktop_interactions: true`, every newly pending one-shot coding approval
also produces a native desktop notification. The notification is a passive
projection of the already-redacted public approval record; it cannot approve a
command and is not routed back into Heartbeat as a new agent trigger.

* **`coding_execution_backend: disabled`** is the safe default. No ad-hoc task
  commands run; built-in verification profiles remain available.
* **`coding_execution_backend: host`** runs directly on the workstation, but
  only when `argv[0]` exactly matches a human-provided executable name or the
  basename of an absolute pinned path in `coding_host_allowed_commands`.
  Absolute configured paths provide stronger PATH-hijack resistance while the
  model still supplies only a name. It strips credentials from the child
  environment and does not accept command paths or shell strings. This is
  pre-authorization, not OS isolation: an approved interpreter can still access
  resources available to the JAWL process.
* **`coding_execution_backend: container`** uses the configured `docker` or
  `podman` runtime. Only the task worktree is bind-mounted, capabilities are
  dropped, `no-new-privileges` is enabled, and memory, CPU, and PID limits are
  applied. Network defaults to `none`; selecting `bridge` is an explicit human
  policy decision. The configured image is trusted infrastructure and should be
  pinned by digest for stronger reproducibility.

Both executable output streams are byte-bounded while being drained, timeout or
cancellation terminates the complete local runtime process tree, and the result
returns before/after workspace fingerprints. Legacy `execute_shell_command`
remains a separate ROOT-only compatibility path and is not used by the coding
workflow.

### One-shot approvals and command profiles

Set `coding_approval_mode: required` to require an operator decision for every
`run_coding_command` and `run_coding_profile` invocation. The agent first calls
`request_coding_command_approval`; the request is stored in protected
`sandbox/_system/` state and expires after `coding_approval_ttl_sec`.

Review requests from another terminal:

```powershell
python jawl.py --approvals list --status pending
python jawl.py --approvals show <approval-id>
python jawl.py --approvals approve <approval-id>
python jawl.py --approvals deny <approval-id>
```

An approval is one-shot. It is bound to the task, argv, workspace fingerprint,
relative working directory, timeout, backend, executable/runtime hash, and the
container image/network/resource policy. Any difference is rejected without
consuming a matching approval. The registry retains only a bounded redacted argv
preview, never raw command arguments.

`coding_command_profiles` gives frequently used toolchains stable names and
exact user-declared argv. `run_coding_profile` does not let the model append or
replace arguments; profiles inherit the same host allowlist or container policy,
workspace fingerprint check, output/process bounds, and approval mode. The agent
can discover redacted profile metadata with `list_coding_command_profiles` and,
when approval is required, use `request_coding_profile_approval` without ever
reconstructing the configured argv.

`coding_container_profiles` defines reusable named OCI policies containing an
exact image, network mode, memory, CPU, and PID limits. A command profile may
select one through `container_profile`; ad-hoc calls may pass
`container_profile_name` after discovering policies with
`list_coding_container_profiles`. Omitting the name preserves the existing
global `coding_container_*` defaults. Unknown or duplicate names fail during
configuration validation, a named profile is rejected by the host backend, and
the complete resolved policy is part of the one-shot approval fingerprint. A
policy changed after approval therefore cannot reuse that approval.

## Coding plan quality

`initialize_coding_task_plan` accepts `quality_policy` as `advisory` (the
backward-compatible default) or `enforce`. Coding-agent prompts use `enforce`.
Strict plans must contain only outcome requirements and must bind every one to
one or more step `requirement_ids`; process bookkeeping such as repository
inspection, test execution, diff review, plan updates, and commit stays inside
the implementation action/evidence flow.

The deterministic grader detects duplicate requirements/steps, uncovered
requirements, orphan/bookkeeping steps, outcome-disproportionate step counts,
and fully serialized graphs. Its persisted report contains counts, item IDs,
finding codes, graph/report SHA-256 values, and no objective, requirement, or
step text. Strict rejection occurs before registry mutation. Advisory plans
remain executable and expose recommendations without changing the commit gate.

When an explicitly mapped step completes, its evidence automatically satisfies
all still-pending covered requirements. Blocked or already satisfied
requirements are never overwritten. Revisions recompute the report, preserve
coverage on protected work, and reject a strict graph before persistence if it
drops coverage or exceeds the outcome-based budget.

## Coding verification stability

`run_coding_verification` accepts only built-in verification profile names. Set
`stability_runs` from `1` to `3` to repeat test profiles (`pytest`, npm, Cargo,
Go, .NET, Maven, or Gradle) while keeping deterministic Git diff and compile
checks single-run. The default remains `1`, preserving prior runtime cost.

Repositories can make this policy durable without adding executable text:

```json
{
  "version": 1,
  "checks": ["git_diff_check", "python_compile", "pytest"],
  "timeout_sec": 300,
  "stop_on_failure": true,
  "stability_runs": 2,
  "test_selection": "affected",
  "pytest_workers": 4
}
```

The file must be `.jawl/verification.json`. Unknown fields, commands, and
environment overrides fail closed. Each completed attempt is persisted before
the next starts. Uniform repeated outcomes are labelled `stable_pass` or
`stable_fail`; mixed outcomes make the entire run `flaky`. A flaky run is never
treated as current verification, triggers the same replanning path as a failed
run, and cannot authorize a commit. Workspace mutation during any attempt still
takes precedence and marks the result `stale`.

`test_selection` is `full` by default. The opt-in `affected` mode currently
narrows only pytest: JAWL collects the exact tracked and untracked changes,
builds a bounded Python import graph, follows transitive dependents, and passes
the reached pytest files as argv targets. It never constructs shell text.

Selection automatically falls back to the full pytest suite when a changed path
is configuration or non-Python content, a rename or deleted test is present, a
source cannot be parsed/read, module resolution is ambiguous, an index bound is
reached, or no affected test is statically proven. Each run persists requested
and effective mode, fallback reason, bounded changed/selected paths, graph
counts, and a SHA-256 decision fingerprint. The normal exact-workspace commit
gate still applies to the resulting run.

`pytest_workers` accepts `1` through `8` and defaults to `1`. Values above one
activate only for a proven affected selection containing 2 through 64 pytest
files. JAWL runs one file per process and balances deterministic worker lanes
with repository-scoped historical EMA durations. The repository identity is
stored only as SHA-256; history is capped at 50 repositories and 500 targets per
repository. Every completed shard is persisted before the next safe boundary,
so cancellation retains completed outcomes and kills remaining process trees.
Selections outside these bounds keep the compatible single-process pytest
command.

## Durable coding diff review

`get_coding_workspace_diff` returns a `reviewed_diff_sha256`, structured hunk
hashes, and `hunk_analysis_complete`. Only a complete response can be accepted
with `accept_coding_workspace_diff_review`; the bridge recomputes it and binds
the evidence to the exact workspace fingerprint. Large changes can be accepted
one complete file at a time, but any subsequent workspace change invalidates all
prior coverage. Raw diff text is not persisted.

New durable coding plans require every changed file to have current review
coverage before the final commit. Old persisted plans and ad-hoc workspaces keep
their existing behavior. `require_diff_review=false` is an explicit escape hatch
for intermediate or unusually large commits and is retained as bypass metadata.
