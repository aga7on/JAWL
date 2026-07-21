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
The next adapter should provision the fixture, send the manifest prompt to JAWL,
export its committed task diff, and feed that patch plus trace metrics here.
