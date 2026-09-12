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
