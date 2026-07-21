# Fixed-repository coding task evaluation

This layer grades candidate patches rather than framework methods. Each task has
a clean repository visible to the candidate, hidden oracle tests stored outside
that repository, an allowed-file scope, and a changed-line budget.

Validate the versioned reference solutions:

```powershell
python benchmarks/coding_tasks/run.py
```

Evaluate patches produced by an agent (`<task-id>.patch` per task):

```powershell
python benchmarks/coding_tasks/run.py --patch-dir path/to/patches
```

The score separates public regression tests (25%), hidden behavioral tests
(45%), scope precision (20%), and patch economy (10%). The hard gate requires
all four. An optional `--tool-metrics metrics.json` payload is copied into the
report so a model driver can attach token, tool-call, retry, and trace totals.

Reference solutions validate the grader; they are not a model benchmark result.

Run the real isolated JAWL ReAct loop through an OpenAI-compatible endpoint:

```powershell
python benchmarks/coding_tasks/drive_jawl.py `
  --api-url http://127.0.0.1:8000/v1 `
  --api-key local_dummy_key `
  --model qwen3.8-max-preview `
  --transport wrapper
```

The live driver copies only the visible repository into a temporary JAWL root,
uses an in-memory tick database and coding-only skill registry, requires a clean
committed task workspace, exports the base-to-HEAD patch, then sends it through
the same hidden-test evaluator. Reports under `.jawl-benchmarks/` separate patch
quality from lifecycle compliance, wall time, ReAct steps, input/output tokens,
per-call transport metrics, and bounded protocol/action tick diagnostics. Raw
chain-of-thought is not serialized and credential-shaped text is redacted. API
keys are never written to the report.

Run any external coding CLI against the same candidate-visible repositories and
hidden grader. The driver never invokes a shell; `{repository}` and `{prompt}`
are replaced inside individual arguments, and the same values are also exposed
as `JAWL_BENCH_REPOSITORY` and `JAWL_BENCH_PROMPT`:

```powershell
python benchmarks/coding_tasks/drive_cli.py `
  --candidate my-agent `
  --candidate-version 1.2.3 `
  --timeout-seconds 900 `
  -- my-agent --cwd "{repository}" --prompt "{prompt}"
```

Resolve and fingerprint the executable/command/task contract without invoking
the candidate or consuming model quota by adding `--preflight-only`. For the
locally installed Codex CLI, a reproducible preflight is:

```powershell
python benchmarks/coding_tasks/drive_cli.py `
  --candidate codex `
  --candidate-version "<output of codex --version>" `
  --preflight-only `
  -- codex exec --ephemeral --ignore-user-config `
     --sandbox workspace-write --color never `
     -C "{repository}" "{prompt}"
```

Remove `--preflight-only` only when the account/token cost is intentional. The
CLI receives no hidden tests or reference patch, but the generic Windows driver
is not an adversarial filesystem sandbox; use an external container/VM for
publication-grade hidden-oracle claims.

Candidate output is drained with a bounded retained tail, the exact process
tree is terminated on timeout, patches are capped at 2 MiB, and neither hidden
tests nor the reference solution are copied into the candidate workspace.
The generic driver controls inputs and grading but is not itself an operating-
system filesystem sandbox. For adversarial benchmark claims, run the candidate
inside its own sandbox/container and pass that launcher as the command.

Every current report contains a `contract.fingerprint` over the selected public
task definitions, visible fixtures, hidden oracle files, and grader source. This
does not reveal hidden contents, but prevents comparisons across changed tasks
or graders. Compare two or more newly generated live reports with:

```powershell
python benchmarks/coding_tasks/compare_reports.py `
  --report path/to/jawl/report.json `
  --report path/to/codex/report.json `
  --output .jawl-benchmarks/comparison.json
```

The comparator ranks only quality measured by the identical contract. It does
not pretend JAWL's verified commit lifecycle equals an external CLI's process
exit/patch extraction, treats wall time as environment-dependent, and leaves
unreported external token usage as unknown rather than zero. Legacy reports
without a contract are intentionally rejected rather than compared loosely.
