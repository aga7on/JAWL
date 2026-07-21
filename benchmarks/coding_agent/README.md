# JAWL coding capability benchmark

This benchmark is a deterministic regression gate for the coding substrate. It
does not claim to measure model intelligence or compare JAWL with a proprietary
agent from a single score. It answers a narrower question: do the safety,
context, isolation, recovery, review, and verification properties required by a
reliable coding agent still work together at this Git revision?

Run the complete gate from the repository root:

```powershell
python benchmarks/coding_agent/run.py
```

List or select capabilities:

```powershell
python benchmarks/coding_agent/run.py --list
python benchmarks/coding_agent/run.py --capability bounded_diff_review
```

The runner creates an ignored JSON report under `.jawl-benchmarks/`. Every
report records the Git revision, Python version, per-capability timing, test
count, pass rate, and failure tail. The versioned `manifest.json` is the source
of truth for the gate. The full pass threshold is intentional: these are
infrastructure invariants rather than aggregate quality signals.

End-to-end model benchmarks belong in a separate layer. They must use fixed
repositories and tasks, record token/time/tool metrics, and grade the produced
patch rather than weakening this deterministic gate.
