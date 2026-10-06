# Processing quality and release checks

Memleaf uses the incremental capture, planner, compiler, review and commit path.
Extraction, review and eligible maintenance share each work item's durable
five-request budget. A successful result stops further extraction requests;
missing support remains unknown or unresolved rather than being invented.

## Separate kinds of evidence

- Deterministic checks verify source binding, identity, permissions, state,
  recovery and request accounting without external model calls.
- Source-based review of actual retained memories verifies whether useful facts
  were kept, unchanged requirements survived updates and independent matters
  stayed independent. A JSON or schema pass alone is insufficient.
- Real-host acceptance verifies the loaded Core/Provider, actual session model,
  capture and background processing through the installed host.
- Artifact CI verifies source syntax, clean distribution payloads and native
  wheel/sdist installation. It does not establish model quality.

Development acceptance runners, synthetic fixtures, benchmarks and private
reports are retained locally and are not shipped as package modules, examples
or user-facing diagnostic tools. The normal runtime APIs and configuration are
used for supported product operations.

Real model acceptance records the actual route, all attempted runs, request
counts and obtainable usage metrics. Scripted replay is reported separately.
Unknown usage remains unknown; failed attempts are not relabelled as passing
checks. Normal `process --dry-run` may call the configured model and must not be
used as a free development preview.

See the [evidence guide](acceptance-evidence-guide.md),
[installed artifact checks](installed-artifact-verification.md) and
[stability policy](stability.md).
