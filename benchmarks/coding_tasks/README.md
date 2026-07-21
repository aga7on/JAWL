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
