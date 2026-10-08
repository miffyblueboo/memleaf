"""Profile selection and permission projection for the host MCP executor."""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
from typing import Any

from .host_v2 import HostMemory
from .host_v2_common import V2Error, failure
from .host_v2_schema import tool_definitions, HOST_INSTRUCTIONS
from .models import Memory

READ_TOOLS = {"search", "read", "list_todos"}
HOST_TOOLS = {t["name"] for t in tool_definitions()}


def attach(service: Any, *, token: str | None = None, token_file: str | None = None) -> HostMemory:
    """Credentials are startup-only and never become model tool arguments."""
    if token_file:
        path = Path(token_file).expanduser()
        if path.is_symlink() or not path.is_file():
            raise V2Error("AUTH_REQUIRED")
        token = path.read_text(encoding="utf-8").strip()
    host = HostMemory(service, token or os.environ.get("MEMLEAF_HOST_TOKEN", ""))
    host.auth.authenticate(host.token)
    service._host_v2 = host
    return host


def definitions(legacy: Any) -> list[dict[str, Any]]:
    result = tool_definitions() + [deepcopy(t) for t in legacy if t["name"] in READ_TOOLS | {"capture"}]
    for tool in result:
        readonly = tool["name"] in READ_TOOLS | {"memory_capabilities", "memory_work"}
        tool["annotations"] = {"readOnlyHint": readonly, "destructiveHint": tool["name"] in {"submit_memory", "resume_memory", "forget_memory"},
                               "idempotentHint": readonly or tool["name"] in HOST_TOOLS, "openWorldHint": False}
    return result


def invoke(host: HostMemory, name: str, arguments: Any, request_id: Any, legacy_invoke: Any) -> dict[str, Any]:
    from .mcp_server import _tool_result, _validate_tool_arguments
    host.service._host_v2 = host
    if name in HOST_TOOLS:
        value = host.call(name, arguments)
        return _tool_result(value, is_error=not value["ok"])
    try:
        args = _validate_tool_arguments(name, arguments)
        # Resolve continuation under the retrieval gate's own lock BEFORE the
        # host authorization lock; never nest two non-reentrant Vault locks.
        if name in {"read", "list_todos"} and args.get("continue"):
            host._context()
            from .retrieval_gate import body_continuation, todo_continuation, validate_turn
            retrieval_id = args["retrieval_id"]
            turn = validate_turn(host.vault, retrieval_id)
            if name == "read":
                expected = body_continuation(host.vault, retrieval_id, current_source=turn["source"])
                if expected is None:
                    raise V2Error("RESOURCE_UNAVAILABLE")
                if args.get("restart"):
                    expected = {**expected, "offset": 0}
                    expected.pop("expected_version", None)
            else:
                expected = todo_continuation(host.vault, retrieval_id, current_source=turn["source"])
            args = {**expected, "retrieval_id": retrieval_id}
        with host.vault.lock():
            context = host._context()
            if name == "capture":
                host.auth.check(context, "source.capture")
                if len(args.get("content", "").encode("utf-8")) > 1000000:
                    raise V2Error("INPUT_TOO_LARGE")
                if args.get("visible", True) is False or args.get("record", True) is False or args["role"] not in {"user", "assistant"}:
                    return _tool_result({"stored": False, "suppressed": True, "source_ids": []})
                # v2 captures visible sources in its durable ledger. Arbitrary
                # source/session/message metadata never upgrade caller trust.
                state = host._load()
                source = host._source(state, context, args["role"], args["content"])
                host._save(state)
                return _tool_result({"stored": True, "source_ids": [source["source_id"]], "trust": source["trust"], "model_calls": 0})
            host.auth.check(context, "memory.search" if name in {"search", "list_todos"} else "memory.read")
            if args.get("include_history"):
                host.auth.check(context, "memory.read_history")
            if name in {"search", "list_todos"}:
                scopes = args.get("scope", context["read_scopes"])
                scopes = [scopes] if isinstance(scopes, str) else list(scopes or context["read_scopes"])
                if not scopes or not set(scopes).issubset(context["read_scopes"]):
                    raise V2Error("RESOURCE_UNAVAILABLE")
                args["scope"] = scopes
            elif args.get("memory_id"):
                # The actual result is rechecked again after retrieval; use a
                # read-only scan here, never a mutation/recovery boundary.
                from .query_scan import scan_memories
                snapshot = scan_memories(host.vault, args.get("include_history", False))
                snapshot.require_identity(args["memory_id"])
                record = next((r for r in snapshot.records if r.memory.memory_id == args["memory_id"]), None)
                if record:
                    host.auth.check(context, "memory.read", scopes=record.memory.scopes)
        result = legacy_invoke(host.service, name, args, request_id=request_id)
        with host.vault.lock():
            # Never disclose a result after a revocation/epoch change while the
            # legacy read API was running under its own lock.
            host.auth.check(context, "memory.read" if name == "read" else "memory.search")
            data = result.get("structuredContent", {})
            if not result.get("isError"):
                if name == "read" and isinstance(data, dict) and data.get("scopes"):
                    host.auth.check(context, "memory.read", scopes=data["scopes"])
                elif name in {"search", "list_todos"} and isinstance(data, dict):
                    from .query_scan import scan_memories
                    scan = scan_memories(host.vault, args.get("include_history", False))
                    records = {r.memory.memory_id: r.memory for r in scan.records}
                    rows = []
                    for row in data.get("results", []):
                        memory = records.get(row.get("memory_id"))
                        scopes = memory.scopes if memory else row.get("scopes", ["global"])
                        if set(scopes).issubset(context["read_scopes"]):
                            rows.append(row)
                    data = {**data, "results": rows}
                    # Counters and completeness must describe the authorized
                    # projection, never an unfiltered index or title directory.
                    if len(rows) != len(result["structuredContent"].get("results", [])):
                        data.update(can_claim_complete=False)
                    result = _tool_result(data)
        return result
    except Exception as exc:
        error = exc if isinstance(exc, V2Error) else V2Error("INVALID_SCHEMA")
        return _tool_result(failure(error), is_error=True)
