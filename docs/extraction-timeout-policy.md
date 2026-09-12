# Superseded by the v0.2.44 latency policy

The independent timeout-only implementation in PR #44 is superseded by main
commit `c13c6dedacfdf6206fd8ebdaa04c4d84fdc0b919` and the published v0.2.44.
Do not merge this branch over that release; doing so would discard its
additional structural timing implementation.

Use `docs/extraction-latency.md` from main for the current timeout, timing and
recovery policy. Ten seconds remains a successful-extraction performance
objective rather than a cancellation/commit deadline.

The PR conversation records independent verification against the exact released
v0.2.44 distributions. No production Vault or paid model was used.
