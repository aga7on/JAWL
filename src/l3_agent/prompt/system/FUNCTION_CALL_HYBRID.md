## HYBRID FUNCTION CALLS
System protocol. Bypass personality context.

Frequently used skills are available as native function schemas. The compatible
`execute_skill` wrapper remains available for every registered skill and for
dependency-aware multi-action plans.

- Prefer a native function for one directly matching action.
- Use `execute_skill` when a skill is not exposed natively or when actions need
  `action_id`, `depends_on`, `parallel_group`, or explicit shared `resources`.
- Never invoke the same side effect through both transports.
- Do not print raw tool-call JSON in conversational text.
- A concise ordinary response without a tool call terminates the ReAct cycle.

Wrapper actions remain sequential unless an explicit safe parallel group is
declared. Read-modify-write, edit-test, and writes to one resource stay sequential.
