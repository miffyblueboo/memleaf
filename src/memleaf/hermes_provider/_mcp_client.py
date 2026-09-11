"""Hermes stdio MCP transport and result-normalization internals."""
from __future__ import annotations

try:
    from ._shared import *
except (ImportError, ValueError):
    import importlib.util as _importlib_util
    import sys as _sys
    from pathlib import Path as _Path

    _name = "_memleaf_hermes_shared"
    _module = _sys.modules.get(_name)
    if _module is None:
        _spec = _importlib_util.spec_from_file_location(_name, _Path(__file__).with_name("_shared.py"))
        if _spec is None or _spec.loader is None:
            raise ImportError("Hermes provider module _shared is unavailable")
        _module = _importlib_util.module_from_spec(_spec)
        _sys.modules[_name] = _module
        _spec.loader.exec_module(_module)
    globals().update({name: getattr(_module, name) for name in getattr(_module, "__all__", ())})

class _MCPClient:
    """Small synchronous JSON-RPC client for memleaf's stdio MCP server."""

    @staticmethod
    def _creationflags() -> int:
        """Return platform-specific flags owned by the MCP transport."""

        return _mcp_creationflags()

    def __init__(
        self,
        command: str,
        vault: str,
        timeout: float,
        process_timeout: float = _DEFAULT_PROCESS_TIMEOUT,
    ) -> None:
        self.command = command
        self.vault = vault
        self.timeout = _bounded_timeout(timeout, _DEFAULT_TIMEOUT, _MAX_TIMEOUT)
        self.process_timeout = _bounded_timeout(
            process_timeout,
            _DEFAULT_PROCESS_TIMEOUT,
            _MAX_PROCESS_TIMEOUT,
        )
        self._process: Optional[subprocess.Popen[str]] = None
        self._stdout_queue: Optional[queue.Queue[object]] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._next_id = 1
        self._lock = threading.RLock()
        self.server_version: Optional[str] = None

    def _resolve_command(self) -> str:
        path = Path(self.command).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        resolved = shutil.which(self.command)
        if resolved:
            return resolved
        known = Path.home() / ".local" / "bin" / "memleaf-mcp"
        if known.is_file() and os.access(known, os.X_OK):
            return str(known)
        raise FileNotFoundError("memleaf-mcp executable is unavailable")

    def _start_stdout_reader_locked(self, process: subprocess.Popen[str]) -> None:
        """Read child stdout on a thread so Windows pipes can use timeouts."""

        output_queue: queue.Queue[object] = queue.Queue()
        self._stdout_queue = output_queue

        def reader() -> None:
            stream = process.stdout
            if stream is None:
                output_queue.put(_MCP_PIPE_EOF)
                return
            try:
                while True:
                    line = stream.readline()
                    if not line:
                        break
                    output_queue.put(line)
            except (OSError, ValueError):
                pass
            finally:
                output_queue.put(_MCP_PIPE_EOF)

        thread = threading.Thread(
            target=reader,
            name="memleaf-mcp-stdout",
            daemon=True,
        )
        self._stdout_thread = thread
        thread.start()

    def _start_locked(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        self._close_locked()
        self._process = subprocess.Popen(
            [self._resolve_command(), "--vault", str(Path(self.vault).expanduser().resolve())],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="strict",
            bufsize=1,
            creationflags=self._creationflags(),
        )
        self._start_stdout_reader_locked(self._process)
        initialize_result = self._request_locked(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "hermes-memleaf", "version": "0.1.0"},
            },
        )
        server_info = (
            initialize_result.get("serverInfo")
            if isinstance(initialize_result, Mapping)
            else None
        )
        if isinstance(server_info, Mapping):
            self.server_version = _version_value(server_info.get("version"))
        self._send_locked({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send_locked(self, message: Mapping[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("memleaf MCP process is not running")
        self._process.stdin.write(json.dumps(dict(message), ensure_ascii=False) + "\n")
        self._process.stdin.flush()

    def _read_response_locked(self, request_id: int, timeout: Optional[float] = None) -> dict[str, Any]:
        if self._process is None or self._stdout_queue is None:
            raise RuntimeError("memleaf MCP process is not running")
        output_queue = self._stdout_queue
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("memleaf MCP request timed out")
            try:
                line = output_queue.get(timeout=remaining)
            except queue.Empty as error:
                raise TimeoutError("memleaf MCP request timed out") from error
            if line is _MCP_PIPE_EOF:
                raise RuntimeError("memleaf MCP process exited unexpectedly")
            if not isinstance(line, str):
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") != request_id:
                continue
            if isinstance(message.get("error"), Mapping):
                raise RuntimeError("MCP error")
            result = message.get("result")
            return result if isinstance(result, dict) else {"result": result}

    def _request_locked(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send_locked({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)})
        return self._read_response_locked(request_id, timeout if timeout is not None else self.timeout)

    def _close_locked(self) -> None:
        process = self._process
        thread = self._stdout_thread
        self._process = None
        self._stdout_queue = None
        self._stdout_thread = None
        self.server_version = None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        if process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        with self._lock:
            try:
                self._start_locked()
                result = self._request_locked(
                    "tools/call",
                    {"name": name, "arguments": dict(arguments)},
                    timeout=self.process_timeout if name == "process" else self.timeout,
                )
            except Exception:
                self._close_locked()
                raise

            if result.get("isError"):
                error_fields = _mcp_error_fields(result) or ("model_failed", None, None, None, None)
                raise _MCPToolError(*error_fields)
            structured = result.get("structuredContent")
            # The core wraps scalar tool results as {"result": value}. A
            # process_status payload is itself a mapping that legitimately
            # contains a nested result plus status/job fields; preserve that
            # full audit record instead of unwrapping it.
            if isinstance(structured, Mapping) and set(structured) == {"result"}:
                return structured["result"]
            if structured is not None:
                return structured
            content = result.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, Mapping) and item.get("type") == "text":
                        try:
                            return json.loads(str(item.get("text", "")))
                        except ValueError:
                            break
            return None


def _resolve_vault(config: Mapping[str, Any]) -> Path:
    return Path(str(config.get("vault") or _DEFAULT_VAULT)).expanduser().resolve()


def _resolve_command(config: Mapping[str, Any]) -> Optional[str]:
    candidate = str(config.get("command") or _DEFAULT_COMMAND).strip()
    path = Path(candidate).expanduser()
    if path.is_file() and os.access(path, os.X_OK):
        return str(path)
    return shutil.which(candidate) or (
        str(Path.home() / ".local" / "bin" / "memleaf-mcp")
        if (Path.home() / ".local" / "bin" / "memleaf-mcp").is_file()
        else None
    )


def _native_execution_projection(name: str, payload: Any) -> tuple[Any, str, bool]:
    """Project documented host execution envelopes, never arbitrary JSON data.

    Native Hermes terminal/code tools wrap the observed text in ``output``.
    Keeping that envelope as escaped JSON hides its record boundaries and
    makes exact source quotation unnecessarily fragile. Execution and host
    truncation fields remain separate from the observed text. A successful
    transport says nothing about completeness of an underlying document.
    """
    error = isinstance(payload, Mapping) and payload.get("isError") is True
    partial = False
    if name not in {"terminal", "execute_code"}:
        return payload, "error" if error else "success", partial
    # Some host transports JSON-encode their envelope more than once. Decode
    # only bounded native envelope layers, not the observed output itself.
    for _ in range(3):
        if not isinstance(payload, str):
            break
        try:
            decoded = json.loads(payload)
        except (TypeError, ValueError):
            break
        if not isinstance(decoded, (str, Mapping)) or decoded == payload:
            break
        payload = decoded
    if not isinstance(payload, Mapping):
        return payload, "success", partial
    error = payload.get("isError") is True
    exit_code = payload.get("exit_code")
    status = payload.get("status")
    failed_states = {"error", "failed", "cancelled", "canceled", "timeout", "timed_out"}
    native_envelope = (type(exit_code) is int or isinstance(status, str))
    if not native_envelope:
        return payload, "error" if error else "unknown", partial
    error = error or (type(exit_code) is int and exit_code != 0) or (isinstance(status, str) and status in failed_states)
    error = error or bool(payload.get("error"))
    partial = payload.get("stdout_truncated") is True or payload.get("truncated") is True
    omitted = payload.get("stdout_bytes_omitted")
    partial = partial or (type(omitted) is int and omitted > 0)
    observed = payload.get("output")
    has_output = isinstance(observed, str) and bool(observed.strip())
    success = (type(exit_code) is int and exit_code == 0 and status in (None, "success")) or status == "success"
    state = "error" if error else "success" if success and has_output else "unknown"
    return observed if has_output else payload, state, partial


def _bounded_current_tool_evidence(messages: Optional[List[Dict[str, Any]]], *, vault_root: Optional[Path] = None) -> list[dict[str, str]]:
    """Match current-turn results strictly by call ID, not tool name/order.

    Kept standard-library-only: Hermes can load this copied provider while the
    core runs in a separate environment. The adjacent shared budget module is
    the only body/record boundary; Core redacts and validates these records
    again before persistence using the same idempotent rule.
    """
    if not isinstance(messages, list):
        return []
    start = next((index for index in range(len(messages) - 1, -1, -1)
                  if isinstance(messages[index], Mapping) and messages[index].get("role") == "user"), None)
    if start is None:
        # Without a current-turn boundary, cumulative history is not new evidence.
        return []
    current = messages[start:]
    calls = _visible_tool_calls(current)
    results = _visible_tool_results(current)
    output = []
    seen = set()
    for call in calls:
        cid, name = call.get("call_id"), call.get("name")
        if not isinstance(cid, str) or not cid or not isinstance(name, str) or cid in seen:
            continue
        if sum(other.get("call_id") == cid for other in calls) != 1:
            continue
        matches = [result for result in results if result.get("call_id") == cid]
        if len(matches) != 1:
            continue
        payload = matches[0].get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (ValueError, TypeError):
                pass
        kind = "retrieved_memory" if (re.search(r"(?:^|[_.:/-])memleaf(?:$|[_.:/-])", name, re.I)
            or _path_is_within(vault_root, _path_from_tool_arguments(call.get("arguments"))) is True) else "external_observation"
        payload, execution_state, execution_partial = _native_execution_projection(name, payload)
        execution_error = execution_state == "error"

        def record(value: Any, record_id: Optional[str] = None) -> Optional[dict[str, str]]:
            try:
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            except (TypeError, ValueError):
                return None
            if not text or "\x00" in text:
                return None
            item = {"tool_name": name[:320], "call_id": cid[:320], "kind": kind,
                    "execution_status": execution_state,
                    "completeness": "partial" if execution_partial else "complete", "schema_version": "2",
                    "result_status": "error" if execution_error else "truncated" if execution_partial else execution_state,
                    "content": text,
                    "source_type": (
                        "attachment" if _has_attachment_arguments(call.get("arguments"))
                        else "document" if _has_document_arguments(call.get("arguments"))
                        else "tool_result"
                    )}
            if isinstance(value, Mapping):
                for key in ("record_id", "title", "message_id", "subject", "sender", "domain"):
                    field = value.get(key)
                    if isinstance(field, str) and field.strip() and not any(ch in field for ch in "\x00\r\n"):
                        item[key] = field[:320]
            if record_id is not None:
                item["record_id"] = record_id
            return item

        original = record(payload)
        if original is None:
            continue
        collection, context = None, {}
        if not execution_error:
            if isinstance(payload, list):
                collection = payload
            elif isinstance(payload, Mapping):
                keys = [key for key in ("items", "records", "results") if isinstance(payload.get(key), list)]
                if len(keys) == 1:
                    collection = payload[keys[0]]
                    context = {key: value for key, value in payload.items() if key != keys[0]}
        if collection:
            for index, value in enumerate(collection):
                item = record({"context": context, "record": value}, f"result-record-{index}")
                if item is not None:
                    for field in ("message_id", "subject", "sender", "domain", "title"):
                        field_value = value.get(field) if isinstance(value, Mapping) else None
                        if not isinstance(field_value, str) or not field_value.strip():
                            field_value = original.get(field)
                        if isinstance(field_value, str) and field_value.strip() and not any(ch in field_value for ch in "\x00\r\n"):
                            item[field] = field_value[:320]
                    output.append(item)
        else:
            output.append(original)
        seen.add(cid)
    return apply_evidence_budget(output)

__all__ = [name for name in globals() if not name.startswith('__')]
