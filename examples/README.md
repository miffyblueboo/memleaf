# Examples

`basic_usage.py` runs entirely offline. Without `--vault` it creates a
temporary vault; pass a directory explicitly when you want to inspect the
generated Markdown after the process exits.

The example shows a lightweight `context()` directory followed by an explicit
`read_page()` call for the selected entry. Directory results contain no body;
long bodies can be read in subsequent pages using `next_offset` and `version`.

The `mcp_stdio.ndjson` file contains one legacy initialization request and one
modern discovery request. Pipe it to the stdio adapter with an explicit vault:

```sh
python -m memleaf.mcp_server --vault <your-vault> < examples/mcp_stdio.ndjson
```

The NDJSON file is request-only and intentionally contains no tool call,
network endpoint, local path, or secret.

`live_core_lifecycle_acceptance.py` is an opt-in real-model check. It sends only
generated Cedar/Birch documents to the configured model and writes to a fresh
temporary Vault. Only the supplied config's `llm` route is read; credentials
are kept in memory, and existing inboxes, memories, and attachments are never read.

```sh
PYTHONPATH=src python examples/live_core_lifecycle_acceptance.py --model-config ~/.memleaf/config.yaml
```

It checks large tool inputs, cross-batch duplicate CREATEs, completion of an
existing todo, structured deadlines, public todo readback, repeat processing,
and read-only queries. It fails on incorrect business results even if processing
reports success. Up to 60 real model calls are permitted; this script is not
part of the offline test suite. The printed temporary directory contains only
the synthetic Vault, model prompts/replies, and acceptance results.
