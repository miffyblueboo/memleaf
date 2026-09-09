from __future__ import annotations

from pathlib import Path
import re


def read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one match in {path}: {old[:120]!r}, got {count}")
    write(path, text.replace(old, new, 1))


def replace_count(path: str, old: str, new: str, expected: int) -> None:
    text = read(path)
    count = text.count(old)
    if count != expected:
        raise SystemExit(f"expected {expected} matches in {path}: {old[:120]!r}, got {count}")
    write(path, text.replace(old, new))


def regex_once(path: str, pattern: str, replacement: str, *, flags: int = re.S) -> None:
    text = read(path)
    updated, count = re.subn(pattern, replacement, text, count=1, flags=flags)
    if count != 1:
        raise SystemExit(f"expected one regex match in {path}: {pattern[:160]!r}, got {count}")
    write(path, updated)


# ---------------------------------------------------------------------------
# 1. One project-Scope grounding boundary for registered and new projects.
# ---------------------------------------------------------------------------
path = "src/memleaf/memory_planner.py"
regex_once(
    path,
    r"\ndef _unregistered_model_project_scopes\(.*?\n\ndef _candidate_bound_source_text\(",
    "\n\ndef _candidate_bound_source_text(",
)
regex_once(
    path,
    r"def _model_project_scope_is_source_grounded\(.*?\n\n\nclass MemoryPlanner:",
    '''def _model_project_scope_is_source_grounded(
    candidate: Mapping[str, Any],
    batch_units: Iterable[Any],
    scope_registry: Mapping[str, Any] | None,
    authorized_scopes: Iterable[str] = (),
) -> bool:
    """Validate every model-selected project Scope against exact bound source.

    Registered and newly named projects intentionally use the same rule. Core
    verifies that the selected project's canonical name or configured alias is
    present in this candidate's immutable bound source, unless the caller
    explicitly authorized that project Scope. Core does not infer whether some
    other mentioned name is an owner, implementation platform, product,
    notification source, or comparison context; semantic review owns that
    relationship judgment.
    """

    if candidate.get("scope_source") != "model" or not candidate.get("worth"):
        return True
    scopes = tuple(
        scope
        for scope in candidate.get("scopes", ())
        if isinstance(scope, str) and scope.partition(":")[0] == "project"
    )
    if not scopes:
        return True
    authorized = {
        value.casefold()
        for value in authorized_scopes
        if isinstance(value, str) and value.partition(":")[0] == "project"
    }
    source_text = _candidate_bound_source_text(candidate, batch_units)
    for scope in scopes:
        if scope.casefold() in authorized:
            continue
        if not source_text:
            return False
        owners, matches = _model_scope_grounding_evidence(
            source_text, (scope,), scope_registry
        )
        owner = owners.get(scope.casefold(), scope.casefold())
        if owner not in matches:
            return False
    return True


class MemoryPlanner:''',
)
replace_once(
    path,
    "from .prompts import COVERAGE_ALREADY_COMPLETED_CORRECTION, COVERAGE_CANDIDATE_CORRECTION, COVERAGE_CORRECTION, GATE_SYSTEM, SUMMARIZE_SYSTEM, gate_prompt, summarize_prompt",
    "from .prompts import COVERAGE_ALREADY_COMPLETED_CORRECTION, COVERAGE_CANDIDATE_CORRECTION, COVERAGE_CORRECTION, GATE_COVERAGE_SYSTEM, GATE_SYSTEM, SUMMARIZE_SYSTEM, coverage_repair_prompt, gate_prompt, summarize_prompt",
)
regex_once(
    path,
    r'''(?P<indent>\s+)prepared_candidates: list\[dict\[str, Any\]\] = \[\]\n(?P=indent)for candidate in parsed\["candidates"\]:\n(?P=indent)    item = dict\(candidate\)\n(?P=indent)    if self\.inputs\._scope_evidence_conflict\(item, turn, validation_scope_registry\):\n(?P=indent)        item\["_defer_reason"\] = "scope_conflict"\n(?P=indent)    plan = self\.inputs\._scope_correction_plan''',
    lambda match: (
        f'{match.group("indent")}prepared_candidates: list[dict[str, Any]] = []\n'
        f'{match.group("indent")}for candidate in parsed["candidates"]:\n'
        f'{match.group("indent")}    item = dict(candidate)\n'
        f'{match.group("indent")}    # Exact candidate-bound grounding was already validated above.\n'
        f'{match.group("indent")}    # Do not reinterpret a second mentioned project/product/platform name\n'
        f'{match.group("indent")}    # as contradictory ownership with a registry-only name scan.\n'
        f'{match.group("indent")}    plan = self.inputs._scope_correction_plan'
    ),
)
regex_once(
    path,
    r'''(?P<indent>\s+)correction_raw = self\.model\._complete\(backend,\n(?P=indent)    "Coverage correction: classify ONLY the supplied unresolved evidence units\. ".*?\n(?P=indent)    system=GATE_SYSTEM, purpose="gate"\)''',
    lambda match: (
        f'{match.group("indent")}correction_raw = self.model._complete(\n'
        f'{match.group("indent")}    backend,\n'
        f'{match.group("indent")}    coverage_repair_prompt(\n'
        f'{match.group("indent")}        evidence_prompt(\n'
        f'{match.group("indent")}            missing,\n'
        f'{match.group("indent")}            batch_index=batch_index,\n'
        f'{match.group("indent")}            batch_count=batch_count,\n'
        f'{match.group("indent")}            todo_witnesses=todo_witnesses,\n'
        f'{match.group("indent")}        ),\n'
        f'{match.group("indent")}        related_memories=gate_related,\n'
        f'{match.group("indent")}        scope_background=scope_background,\n'
        f'{match.group("indent")}        scope_registry=scope_registry,\n'
        f'{match.group("indent")}        already_handled_candidate_ids=[\n'
        f'{match.group("indent")}            item["candidate_id"] for item in batch_gate["candidates"]\n'
        f'{match.group("indent")}        ],\n'
        f'{match.group("indent")}    ),\n'
        f'{match.group("indent")}    system=GATE_COVERAGE_SYSTEM,\n'
        f'{match.group("indent")}    purpose="gate",\n'
        f'{match.group("indent")}    metric_stage="gate",\n'
        f'{match.group("indent")}    metric_operation="gate_coverage_repair",\n'
        f'{match.group("indent")} )'
    ).replace(f'{match.group("indent")} )', f'{match.group("indent")}'),
)

path = "src/memleaf/planning_context.py"
text = read(path)
text = text.replace("from .admission import analyze_turn_evidence, supporting_units\n", "")
text = text.replace(", _event_payload", "")
text = text.replace(", _project_scope_occurrences", "")
write(path, text)
regex_once(
    path,
    r"\n    def _scope_evidence_conflict\(.*?\n    @staticmethod\n    def _scope_terms_present",
    "\n    @staticmethod\n    def _scope_terms_present",
)

# ---------------------------------------------------------------------------
# 2. Remove the duplicated Gate tail; use a compact coverage-only second pass.
# ---------------------------------------------------------------------------
path = "src/memleaf/prompts.py"
regex_once(
    path,
    r"# Applied to both normal and correction calls\..*?\n\n\n# Same policy for the INNER summary",
    '''# Final reminders not already stated in the main Gate contract. Keep this
# tail intentionally small because it is paid on every Gate request.
GATE_SYSTEM += """\nFinal enforcement reminders: section headings scope only their own children;
a different heading ends that context. Do not inherit a preceding project's
ownership. Every supplied evidence unit still requires coverage even when
candidates is empty. Model-selected project Scope must be grounded by the
candidate's own exact source binding; a product/platform/system mentioned as
implementation context is not project ownership by name alone. Existing or
related memories remain comparison context, never authority for a new project
relationship. Return strict JSON only.\n"""


GATE_COVERAGE_SYSTEM = """You are memleaf's bounded Gate coverage-repair reviewer.
Return exactly one strict JSON object with top-level candidates, coverage, and
evidence_bindings. Classify ONLY the unresolved Evidence units supplied in this
call. Never re-emit or change an already-handled candidate.

For every supplied unit, return exactly one coverage row. decision is CANDIDATE,
NO_CHANGE, or DEFERRED. CANDIDATE must list every candidate_id from this response
that cites that unit. candidates=[] still requires one NO_CHANGE/DEFERRED row
per supplied unit and evidence_bindings=[].

Each candidate has exactly: candidate_id (string), memory (string), duplicate
(boolean), worth (boolean), type (preference|fact|project|todo|event|identity|other
or null), scopes (non-empty string list), scope_source
(model|user|session_context|insufficient_context), plus optional reason,
evidence_event_ids, duplicate_memory_id, update_memory_id. A candidate with
validated evidence_bindings may omit evidence_event_ids so Core derives them.
Do not add status, due_date, claims, or evidence_bindings inside a candidate.

Bindings are top-level. Copy unit_id exactly from the supplied list and bind a
short exact contiguous quote with role assertion|source_excerpt|user_confirmation,
or use whole_unit:true only when the whole unit is homogeneous for one topic.
Never guess offsets or IDs. Related memories, scope background, and the registry
are comparison/grounding context, not new evidence.

Apply the same semantic boundary as primary Gate: retain concrete future-use
facts, requests, constraints, commitments, or state changes; split deliverables
that can be tracked or completed independently; preserve named subject,
deliverable/action/state, necessary business context, polarity, uncertainty,
and each number/code with its stated role. Do not invent owner, deadline, status,
or completion. A query, generic acknowledgement, suggestion, plan, or pure
restatement is not a new fact by itself.

For Scope, a model-selected project must be named (or matched by a registered
alias) in that candidate's exact source. Distinguish ownership/affiliation from
implementation product/platform/system context. A mentioned platform does not
become the owning project by name alone. If ownership cannot be resolved safely,
use unscoped/insufficient_context or DEFERRED rather than guessing. Return JSON
only.\n"""


def coverage_repair_prompt(
    evidence_text: str,
    *,
    related_memories: list[dict] | None = None,
    scope_background: object = None,
    scope_registry: list[dict] | None = None,
    already_handled_candidate_ids: list[str] | None = None,
) -> str:
    """Build the narrow second-pass prompt for unresolved coverage only."""

    return (
        "Mode: coverage repair only. Classify only the unresolved units below.\n"
        + evidence_text
        + "\nNecessary related-memory comparison context:\n"
        + _json(related_memories or [])
        + "\nSession scope background:\n"
        + _json(scope_background if scope_background is not None else [])
        + "\nCurrent scope registry (safe projection; no paths):\n"
        + _json(scope_registry if scope_registry is not None else [])
        + "\nAlready handled candidate IDs (do not re-emit):\n"
        + _json(already_handled_candidate_ids or [])
        + "\n"
        + COVERAGE_CORRECTION
        + "\n"
        + COVERAGE_ALREADY_COMPLETED_CORRECTION
        + "\nReturn the strict coverage-repair Gate JSON object."
    )


# Same policy for the INNER summary''',
)

# Scope metadata itself is an affiliation assertion, not just a label.
path = "src/memleaf/update_review.py"
old = "implementation context. Existing"
new = (
    "implementation context. When proposed_summary carries a project:<name> Scope with "
    "scope_source=model, that Scope is itself a claimed project affiliation: ACCEPT only when "
    "the admitted source supports that affiliation for this candidate. A mere mention of the "
    "same name as a product, platform, system, notification source, comparison, or "
    "implementation location is insufficient. If admitted source explicitly assigns the item "
    "to another project, the fixed proposed Scope cannot be repaired in this review; use "
    "DEFERRED rather than ACCEPT or silently changing Scope. Existing"
)
replace_count(path, old, new, 2)

# ---------------------------------------------------------------------------
# 3. Thinking controls. Default is LOW (user-requested), not disabled.
# ---------------------------------------------------------------------------
path = "src/memleaf/config.py"
replace_once(
    path,
    "MAX_MODEL_CONCURRENCY = 8\n",
    "MAX_MODEL_CONCURRENCY = 8\nTHINKING_PURPOSES = (\"gate\", \"summarize\", \"compact\")\nTHINKING_MODES = frozenset({\"default\", \"disabled\", \"low\", \"high\", \"max\"})\nDEFAULT_THINKING = {purpose: \"low\" for purpose in THINKING_PURPOSES}\n",
)
replace_once(
    path,
    '''def _normalize_model_concurrency(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("invalid memleaf process.model_concurrency")
    if not MIN_MODEL_CONCURRENCY <= value <= MAX_MODEL_CONCURRENCY:
        raise ValueError("invalid memleaf process.model_concurrency")
    return value
''',
    '''def _normalize_model_concurrency(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("invalid memleaf process.model_concurrency")
    if not MIN_MODEL_CONCURRENCY <= value <= MAX_MODEL_CONCURRENCY:
        raise ValueError("invalid memleaf process.model_concurrency")
    return value


def _normalize_thinking_settings(value: Any) -> dict[str, str]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ValueError("invalid memleaf llm.thinking settings")
    if set(value) - set(THINKING_PURPOSES):
        raise ValueError("invalid memleaf llm.thinking settings")
    result = dict(DEFAULT_THINKING)
    for purpose, mode in value.items():
        if not isinstance(mode, str) or mode not in THINKING_MODES:
            raise ValueError("invalid memleaf llm.thinking settings")
        result[purpose] = mode
    return result
''',
)
replace_once(
    path,
    '        "diagnostic_logging": False,\n',
    '        "diagnostic_logging": False,\n        "thinking": dict(DEFAULT_THINKING),\n',
)
replace_once(
    path,
    '''    llm["request_timeout"] = _normalize_request_timeout(llm.get("request_timeout", DEFAULT_REQUEST_TIMEOUT))
    if type(llm.get("diagnostic_logging", False)) is not bool:
''',
    '''    llm["request_timeout"] = _normalize_request_timeout(llm.get("request_timeout", DEFAULT_REQUEST_TIMEOUT))
    llm["thinking"] = _normalize_thinking_settings(llm.get("thinking"))
    if type(llm.get("diagnostic_logging", False)) is not bool:
''',
)
replace_once(
    path,
    '''    normalized_llm["request_timeout"] = _normalize_request_timeout(
        normalized_llm.get("request_timeout", DEFAULT_REQUEST_TIMEOUT)
    )
    diagnostic_logging = normalized_llm.get("diagnostic_logging", False)
''',
    '''    normalized_llm["request_timeout"] = _normalize_request_timeout(
        normalized_llm.get("request_timeout", DEFAULT_REQUEST_TIMEOUT)
    )
    normalized_llm["thinking"] = _normalize_thinking_settings(normalized_llm.get("thinking"))
    diagnostic_logging = normalized_llm.get("diagnostic_logging", False)
''',
)

path = "src/memleaf/llm/base.py"
replace_once(path, "import socket\n", "import socket\nimport threading\n")
replace_once(
    path,
    '''        self._opener = opener or urllib.request.urlopen
        # The built-in stateless urllib transport can be used concurrently.
''',
    '''        self._opener = opener or urllib.request.urlopen
        self._call_metrics_local = threading.local()
        # The built-in stateless urllib transport can be used concurrently.
''',
)
replace_once(
    path,
    '''    @staticmethod
    def _is_timeout_reason(value: Any) -> bool:
''',
    '''    def _set_call_metrics(self, value: Mapping[str, Any] | None) -> None:
        self._call_metrics_local.value = dict(value) if isinstance(value, Mapping) else {}

    def consume_call_metrics(self) -> dict[str, Any]:
        value = getattr(self._call_metrics_local, "value", {})
        self._call_metrics_local.value = {}
        return dict(value) if isinstance(value, Mapping) else {}

    @staticmethod
    def _is_timeout_reason(value: Any) -> bool:
''',
)

path = "src/memleaf/llm/openai_compatible.py"
replace_once(
    path,
    '''        json_mode: bool = False,
        provider_name: str = "openai",
    ):
''',
    '''        json_mode: bool = False,
        provider_name: str = "openai",
        thinking: Mapping[str, Any] | None = None,
    ):
''',
)
replace_once(
    path,
    '''        self.json_mode = bool(json_mode)
        self.provider_name = provider_name.casefold() if isinstance(provider_name, str) else "openai"
''',
    '''        self.json_mode = bool(json_mode)
        self.provider_name = provider_name.casefold() if isinstance(provider_name, str) else "openai"
        self.thinking = dict(thinking) if isinstance(thinking, Mapping) else {}
''',
)
replace_once(
    path,
    '''    def complete(self, prompt: str, *, system: str = "", purpose: str = "", temperature: float = 0.0) -> str:
''',
    '''    def _thinking_mode(self, purpose: str) -> str:
        value = self.thinking.get(purpose, "low")
        return value if value in {"default", "disabled", "low", "high", "max"} else "low"

    @staticmethod
    def _usage_metrics(value: Mapping[str, Any], *, thinking_mode: str) -> dict[str, Any]:
        usage = value.get("usage")
        result: dict[str, Any] = {"thinking_mode": thinking_mode}
        if not isinstance(usage, Mapping):
            return result
        for key in (
            "prompt_tokens", "completion_tokens", "total_tokens",
            "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
        ):
            item = usage.get(key)
            if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 10_000_000:
                result[key] = item
        details = usage.get("completion_tokens_details")
        reasoning = details.get("reasoning_tokens") if isinstance(details, Mapping) else None
        if isinstance(reasoning, int) and not isinstance(reasoning, bool) and 0 <= reasoning <= 10_000_000:
            result["reasoning_tokens"] = reasoning
        return result

    def complete(self, prompt: str, *, system: str = "", purpose: str = "", temperature: float = 0.0) -> str:
''',
)
replace_once(
    path,
    '''        messages = []
        if system:
''',
    '''        self._set_call_metrics({})
        messages = []
        if system:
''',
)
replace_once(
    path,
    '''        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
''',
    '''        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        thinking_mode = self._thinking_mode(purpose)
        if self.provider_name == "deepseek" and thinking_mode != "default":
            if thinking_mode == "disabled":
                payload["thinking"] = {"type": "disabled"}
            else:
                payload["thinking"] = {"type": "enabled"}
                payload["reasoning_effort"] = thinking_mode
''',
)
replace_once(
    path,
    '''        if not isinstance(message, Mapping):
            raise ModelError(
                "model response has no message",
                code="model_invalid_response",
                stage=purpose,
                validation_reason="response_shape",
            )
        try:
''',
    '''        if not isinstance(message, Mapping):
            raise ModelError(
                "model response has no message",
                code="model_invalid_response",
                stage=purpose,
                validation_reason="response_shape",
            )
        self._set_call_metrics(self._usage_metrics(value, thinking_mode=thinking_mode))
        try:
''',
)

path = "src/memleaf/llm/router.py"
replace_once(path, "import os\n", "import os\nimport threading\n")
replace_once(
    path,
    '''        self.api = self._coerce_api(api) if api is not None else self._build_api()
        self.diagnostics: list[dict[str, str]] = []
''',
    '''        self.api = self._coerce_api(api) if api is not None else self._build_api()
        self.diagnostics: list[dict[str, str]] = []
        self._call_metrics_local = threading.local()
''',
)
replace_once(
    path,
    '''                return OpenAICompatibleBackend(
                    **kwargs,
                    json_mode=provider in _JSON_MODE_PROVIDERS,
                    provider_name=provider or "openai",
                )
''',
    '''                return OpenAICompatibleBackend(
                    **kwargs,
                    json_mode=provider in _JSON_MODE_PROVIDERS,
                    provider_name=provider or "openai",
                    thinking=config.get("thinking") if isinstance(config.get("thinking"), Mapping) else None,
                )
''',
)
regex_once(
    path,
    r"    def _call\(self, backend: ModelBackend, prompt: str, \*, system: str, purpose: str, temperature: float\) -> str:.*?\n    def complete\(",
    '''    def _call(self, backend: ModelBackend, prompt: str, *, system: str, purpose: str, temperature: float) -> str:
        self._call_metrics_local.value = {}
        try:
            value = backend.complete(prompt, system=system, purpose=purpose, temperature=temperature)
        except ModelError as error:
            error.with_stage(purpose)
            raise
        except Exception as error:
            raise ModelError("model backend failed", stage=purpose) from error
        finally:
            consume = getattr(backend, "consume_call_metrics", None)
            if callable(consume):
                try:
                    metrics = consume()
                except Exception:
                    metrics = {}
                self._call_metrics_local.value = dict(metrics) if isinstance(metrics, Mapping) else {}
        if not isinstance(value, str):
            raise ModelError("model backend returned non-text output", code="model_invalid_response", stage=purpose)
        return value

    def consume_call_metrics(self) -> dict[str, Any]:
        value = getattr(self._call_metrics_local, "value", {})
        self._call_metrics_local.value = {}
        return dict(value) if isinstance(value, Mapping) else {}

    def complete(''',
)

# ---------------------------------------------------------------------------
# 4. Per-call structural telemetry (no prompt/response/key retention).
# ---------------------------------------------------------------------------
path = "src/memleaf/model_execution.py"
replace_once(
    path,
    '''_METRIC_STAGE_NAMES = frozenset({
    "gate",
    "summarize",
    "semantic_review",
    "coordination",
    "target_reconciliation",
})
''',
    '''_METRIC_STAGE_NAMES = frozenset({
    "gate",
    "summarize",
    "semantic_review",
    "coordination",
    "target_reconciliation",
})
_PROVIDER_METRIC_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "reasoning_tokens",
)
_METRIC_OPERATION_SUFFIXES = ("primary", "format_repair")
_MAX_METRIC_CALLS = 256
''',
)
replace_once(
    path,
    '''        "max_in_flight": 0,
        "_first_started": None,
''',
    '''        "max_in_flight": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 0,
        "reasoning_tokens": 0,
        "cache_hit_calls": 0,
        "_first_started": None,
''',
)
replace_once(
    path,
    '''        self._metric_stages: dict[str, dict[str, Any]] = {}
        self._active_calls = 0
''',
    '''        self._metric_stages: dict[str, dict[str, Any]] = {}
        self._metric_operations: dict[str, dict[str, Any]] = {}
        self._metric_calls: list[dict[str, Any]] = []
        self._next_metric_call_index = 0
        self._active_calls = 0
''',
)
regex_once(
    path,
    r"    def _metric_begin\(.*?\n    @staticmethod\n    def _public_metric_bucket",
    '''    @staticmethod
    def _safe_metric_operation(stage: str, value: Any) -> str:
        if value == "gate_coverage_repair":
            return "gate_coverage_repair"
        if isinstance(value, str) and value in {
            f"{stage}_{suffix}" for suffix in _METRIC_OPERATION_SUFFIXES
        }:
            return value
        return f"{stage}_primary"

    @staticmethod
    def _safe_provider_metrics(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {}
        result: dict[str, Any] = {}
        for key in _PROVIDER_METRIC_FIELDS:
            item = value.get(key)
            if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 10_000_000:
                result[key] = item
        mode = value.get("thinking_mode")
        if mode in {"default", "disabled", "low", "high", "max"}:
            result["thinking_mode"] = mode
        return result

    @staticmethod
    def _consume_provider_metrics(backend: Any) -> dict[str, Any]:
        consume = getattr(backend, "consume_call_metrics", None)
        if not callable(consume):
            return {}
        try:
            return ModelExecutor._safe_provider_metrics(consume())
        except Exception:
            return {}

    def _metric_begin(
        self,
        *,
        stage: str,
        operation: str,
        input_chars: int,
        input_bytes: int,
    ) -> tuple[float, int]:
        started = time.perf_counter()
        with self._metrics_lock:
            self._active_calls += 1
            self._next_metric_call_index += 1
            call_index = self._next_metric_call_index
            for bucket in (
                self._metrics,
                self._metric_stages.setdefault(stage, _metric_bucket()),
                self._metric_operations.setdefault(operation, _metric_bucket()),
            ):
                if bucket["_first_started"] is None:
                    bucket["_first_started"] = started
                bucket["call_count"] += 1
                bucket["input_chars"] += input_chars
                bucket["input_bytes"] += input_bytes
                bucket["max_in_flight"] = max(bucket["max_in_flight"], self._active_calls)
        return started, call_index

    def _metric_finish(
        self,
        *,
        stage: str,
        operation: str,
        call_index: int,
        started: float,
        input_chars: int,
        input_bytes: int,
        output: Any,
        failed: bool,
        retry: bool,
        provider_metrics: Mapping[str, Any] | None,
    ) -> None:
        finished = time.perf_counter()
        elapsed_ms = max(0, round((finished - started) * 1000))
        output_chars = len(output) if isinstance(output, str) else 0
        output_bytes = len(output.encode("utf-8")) if isinstance(output, str) else 0
        provider_metrics = self._safe_provider_metrics(provider_metrics)
        with self._metrics_lock:
            for bucket in (
                self._metrics,
                self._metric_stages.setdefault(stage, _metric_bucket()),
                self._metric_operations.setdefault(operation, _metric_bucket()),
            ):
                bucket["_last_finished"] = finished
                bucket["request_duration_ms"] += elapsed_ms
                bucket["output_chars"] += output_chars
                bucket["output_bytes"] += output_bytes
                if failed:
                    bucket["failed_calls"] += 1
                if retry:
                    bucket["retry_count"] += 1
                for field in _PROVIDER_METRIC_FIELDS:
                    bucket[field] += int(provider_metrics.get(field, 0))
                if int(provider_metrics.get("prompt_cache_hit_tokens", 0)) > 0:
                    bucket["cache_hit_calls"] += 1
            if len(self._metric_calls) < _MAX_METRIC_CALLS:
                call = {
                    "call_index": call_index,
                    "stage": stage,
                    "operation": operation,
                    "retry": bool(retry),
                    "failed": bool(failed),
                    "request_duration_ms": elapsed_ms,
                    "input_chars": input_chars,
                    "input_bytes": input_bytes,
                    "output_chars": output_chars,
                    "output_bytes": output_bytes,
                }
                call.update(provider_metrics)
                self._metric_calls.append(call)
            self._active_calls = max(0, self._active_calls - 1)

    @staticmethod
    def _public_metric_bucket''',
)
regex_once(
    path,
    r"    @staticmethod\n    def _public_metric_bucket\(bucket: Mapping\[str, Any\]\) -> dict\[str, int\]:.*?\n    def _complete\(",
    '''    @staticmethod
    def _public_metric_bucket(bucket: Mapping[str, Any]) -> dict[str, int]:
        first = bucket.get("_first_started")
        last = bucket.get("_last_finished")
        wall_clock_ms = (
            max(0, round((last - first) * 1000))
            if isinstance(first, (int, float)) and isinstance(last, (int, float)) and last >= first
            else 0
        )
        result = {
            "call_count": int(bucket.get("call_count", 0)),
            "retry_count": int(bucket.get("retry_count", 0)),
            "failed_calls": int(bucket.get("failed_calls", 0)),
            "request_duration_ms": int(bucket.get("request_duration_ms", 0)),
            "wall_clock_ms": wall_clock_ms,
            "input_chars": int(bucket.get("input_chars", 0)),
            "input_bytes": int(bucket.get("input_bytes", 0)),
            "output_chars": int(bucket.get("output_chars", 0)),
            "output_bytes": int(bucket.get("output_bytes", 0)),
            "max_in_flight": int(bucket.get("max_in_flight", 0)),
            "cache_hit_calls": int(bucket.get("cache_hit_calls", 0)),
        }
        for field in _PROVIDER_METRIC_FIELDS:
            result[field] = int(bucket.get(field, 0))
        return result

    def metrics(self) -> dict[str, Any]:
        """Return structural telemetry only; never prompts, responses or credentials."""

        with self._metrics_lock:
            total = self._public_metric_bucket(dict(self._metrics))
            stages = {
                key: self._public_metric_bucket(dict(value))
                for key, value in sorted(self._metric_stages.items())
            }
            operations = {
                key: self._public_metric_bucket(dict(value))
                for key, value in sorted(self._metric_operations.items())
            }
            calls = [
                dict(value)
                for value in sorted(self._metric_calls, key=lambda item: item["call_index"])
            ]
        return {"total": total, "stages": stages, "operations": operations, "calls": calls}

    def _complete(''',
)
regex_once(
    path,
    r"    def _complete\(\n        self,\n        backend: Any,\n        prompt: str,.*?\n        return value\n\n    @staticmethod\n    def _set_stage_diagnostics",
    '''    def _complete(
        self,
        backend: Any,
        prompt: str,
        *,
        system: str,
        purpose: str,
        metric_stage: str | None = None,
        metric_operation: str | None = None,
        retry: bool = False,
    ) -> str:
        stage = self._safe_metric_stage(metric_stage or purpose)
        operation = self._safe_metric_operation(stage, metric_operation)
        input_chars = len(prompt) + len(system)
        input_bytes = len(prompt.encode("utf-8")) + len(system.encode("utf-8"))
        started, call_index = self._metric_begin(
            stage=stage,
            operation=operation,
            input_chars=input_chars,
            input_bytes=input_bytes,
        )
        value: Any = None
        failed = False
        try:
            value = backend.complete(prompt, system=system, purpose=purpose, temperature=0.0)
        except ModelError as error:
            failed = True
            error.with_stage(purpose)
            raise
        except Exception as error:
            failed = True
            raise ModelError("model backend failed", stage=purpose) from error
        finally:
            provider_metrics = self._consume_provider_metrics(backend)
            self._metric_finish(
                stage=stage,
                operation=operation,
                call_index=call_index,
                started=started,
                input_chars=input_chars,
                input_bytes=input_bytes,
                output=value,
                failed=failed,
                retry=retry,
                provider_metrics=provider_metrics,
            )
        if not isinstance(value, str):
            with self._metrics_lock:
                self._metrics["failed_calls"] += 1
                self._metric_stages.setdefault(stage, _metric_bucket())["failed_calls"] += 1
                self._metric_operations.setdefault(operation, _metric_bucket())["failed_calls"] += 1
            raise ModelError(
                "model backend returned non-text output",
                code="model_invalid_response",
                stage=purpose,
                validation_reason="response_shape",
            )
        return value

    @staticmethod
    def _set_stage_diagnostics''',
)
replace_once(
    path,
    '''                raw = self._complete(
                    backend,
                    prompt if attempt_count == 1 else correction_prompt,
                    system=system,
                    purpose=purpose,
                    metric_stage=metric_stage,
                    retry=attempt_count > 1,
                )
''',
    '''                operation_stage = self._safe_metric_stage(metric_stage or purpose)
                raw = self._complete(
                    backend,
                    prompt if attempt_count == 1 else correction_prompt,
                    system=system,
                    purpose=purpose,
                    metric_stage=metric_stage,
                    metric_operation=(
                        f"{operation_stage}_primary"
                        if attempt_count == 1
                        else f"{operation_stage}_format_repair"
                    ),
                    retry=attempt_count > 1,
                )
''',
)

path = "src/memleaf/process_jobs.py"
replace_once(
    path,
    '''    "max_in_flight",
)
''',
    '''    "max_in_flight",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "reasoning_tokens",
    "cache_hit_calls",
)
''',
)
replace_once(
    path,
    '''_MODEL_METRIC_STAGES = frozenset({
    "gate",
    "summarize",
    "semantic_review",
    "coordination",
    "target_reconciliation",
    "other",
})
''',
    '''_MODEL_METRIC_STAGES = frozenset({
    "gate",
    "summarize",
    "semantic_review",
    "coordination",
    "target_reconciliation",
    "other",
})
_MODEL_METRIC_OPERATIONS = frozenset(
    {f"{stage}_{suffix}" for stage in _MODEL_METRIC_STAGES for suffix in ("primary", "format_repair")}
    | {"gate_coverage_repair"}
)
_MODEL_CALL_INT_FIELDS = frozenset({
    "call_index", "request_duration_ms", "input_chars", "input_bytes",
    "output_chars", "output_bytes", "prompt_tokens", "completion_tokens",
    "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
    "reasoning_tokens",
})
_MAX_MODEL_CALL_ROWS = 256
''',
)
replace_once(
    path,
    '''        if bounded:
            result["stages"] = bounded
    return result
''',
    '''        if bounded:
            result["stages"] = bounded
    operations = value.get("operations")
    if isinstance(operations, Mapping):
        bounded_operations: dict[str, dict[str, int]] = {}
        for operation, bucket in operations.items():
            if not isinstance(operation, str) or operation not in _MODEL_METRIC_OPERATIONS:
                continue
            projected = _safe_metric_bucket(bucket)
            if projected:
                bounded_operations[operation] = projected
        if bounded_operations:
            result["operations"] = bounded_operations
    calls = value.get("calls")
    if isinstance(calls, list):
        bounded_calls: list[dict[str, Any]] = []
        for raw in calls[:_MAX_MODEL_CALL_ROWS]:
            if not isinstance(raw, Mapping):
                continue
            stage = raw.get("stage")
            operation = raw.get("operation")
            if stage not in _MODEL_METRIC_STAGES or operation not in _MODEL_METRIC_OPERATIONS:
                continue
            row: dict[str, Any] = {"stage": stage, "operation": operation}
            for key in ("retry", "failed"):
                if isinstance(raw.get(key), bool):
                    row[key] = raw[key]
            for key in _MODEL_CALL_INT_FIELDS:
                item = raw.get(key)
                if type(item) is int and item >= 0:
                    row[key] = item
            mode = raw.get("thinking_mode")
            if mode in {"default", "disabled", "low", "high", "max"}:
                row["thinking_mode"] = mode
            bounded_calls.append(row)
        if bounded_calls:
            result["calls"] = bounded_calls
    return result
''',
)
replace_once(
    path,
    '''    if stages:
        result["stages"] = stages
    return result
''',
    '''    if stages:
        result["stages"] = stages
    operation_names = sorted({
        operation
        for value in safe_values
        for operation in value.get("operations", {})
        if isinstance(value.get("operations"), Mapping) and operation in _MODEL_METRIC_OPERATIONS
    })
    operations: dict[str, dict[str, int]] = {}
    for operation in operation_names:
        buckets = [
            value["operations"][operation]
            for value in safe_values
            if isinstance(value.get("operations"), Mapping)
            and isinstance(value["operations"].get(operation), Mapping)
        ]
        if buckets:
            operations[operation] = _aggregate_metric_buckets(buckets)
    if operations:
        result["operations"] = operations
    calls: list[dict[str, Any]] = []
    for value in safe_values:
        rows = value.get("calls")
        if not isinstance(rows, list):
            continue
        for raw in rows:
            if isinstance(raw, Mapping) and len(calls) < _MAX_MODEL_CALL_ROWS:
                row = dict(raw)
                row["call_index"] = len(calls) + 1
                calls.append(row)
    if calls:
        result["calls"] = calls
    return result
''',
)

# ---------------------------------------------------------------------------
# 5. Deterministic regressions.
# ---------------------------------------------------------------------------
write(
    "tests/test_gate_scope_latency_v038.py",
    '''from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from memleaf.admission import analyze_turn_evidence
from memleaf.config import DEFAULT_THINKING, load_config, save_config
from memleaf.llm.openai_compatible import OpenAICompatibleBackend
from memleaf.memory_planner import _model_project_scope_is_source_grounded
from memleaf.model_execution import ModelExecutor
from memleaf.prompts import GATE_COVERAGE_SYSTEM, GATE_SYSTEM, coverage_repair_prompt
from memleaf.update_review import CREATE_SEMANTIC_REVIEW_SYSTEM, UPDATE_SEMANTIC_REVIEW_SYSTEM


class _Response:
    def __init__(self, payload):
        self.payload = payload
    def read(self):
        return json.dumps(self.payload).encode("utf-8")
    def close(self):
        pass


class _Vault:
    def config(self):
        return {"process": {"model_concurrency": 1}, "llm": {"diagnostic_logging": False}}


class _Service:
    vault = _Vault()


def _candidate(unit, scope):
    return {
        "candidate_id": "c1",
        "memory": unit.text,
        "worth": True,
        "duplicate": False,
        "type": "project",
        "scopes": [scope],
        "scope_source": "model",
        "_evidence_bindings": [{
            "unit_id": unit.unit_id,
            "quote": unit.text,
            "role": "assertion",
        }],
    }


class V038ScopeGateLatencyTests(unittest.TestCase):
    def test_new_project_plus_registered_platform_is_not_a_name_conflict(self):
        units = analyze_turn_evidence([{
            "role": "user",
            "content": "Alpha 项目的监管改造属于 Alpha 项目，实际功能在 Orion 平台实施。",
            "event_key": "u1",
        }])
        self.assertTrue(_model_project_scope_is_source_grounded(
            _candidate(units[0], "project:Alpha"),
            units,
            {"project:Orion": {"aliases": ["Orion"]}},
        ))

    def test_registered_model_scope_also_requires_name_or_alias_in_bound_source(self):
        units = analyze_turn_evidence([{
            "role": "user", "content": "Alpha 项目的批量导入仍待确认。", "event_key": "u1"
        }])
        self.assertFalse(_model_project_scope_is_source_grounded(
            _candidate(units[0], "project:Orion"),
            units,
            {"project:Orion": {"aliases": ["OR-1"]}},
        ))
        alias_units = analyze_turn_evidence([{
            "role": "user", "content": "OR-1 项目的批量导入仍待确认。", "event_key": "u2"
        }])
        self.assertTrue(_model_project_scope_is_source_grounded(
            _candidate(alias_units[0], "project:Orion"),
            alias_units,
            {"project:Orion": {"aliases": ["OR-1"]}},
        ))

    def test_unsupported_new_project_stays_rejected(self):
        units = analyze_turn_evidence([{
            "role": "user", "content": "Orion 平台需要升级。", "event_key": "u1"
        }])
        self.assertFalse(_model_project_scope_is_source_grounded(
            _candidate(units[0], "project:Alpha"), units, {"project:Orion": {}}
        ))

    def test_semantic_review_treats_project_scope_as_affiliation_claim(self):
        for system in (CREATE_SEMANTIC_REVIEW_SYSTEM, UPDATE_SEMANTIC_REVIEW_SYSTEM):
            flattened = " ".join(system.split())
            self.assertIn("Scope is itself a claimed project affiliation", flattened)
            self.assertIn("implementation location is insufficient", flattened)
            self.assertIn("use DEFERRED", flattened)

    def test_gate_duplicate_tail_removed_and_coverage_protocol_is_narrow(self):
        self.assertLess(len(GATE_SYSTEM), 21000)
        self.assertLess(len(GATE_COVERAGE_SYSTEM), 7000)
        self.assertLess(len(GATE_COVERAGE_SYSTEM), len(GATE_SYSTEM))
        prompt = coverage_repair_prompt(
            "Evidence units:\\nONLY-UNRESOLVED",
            related_memories=[{"memory_id": "m1", "title": "comparison"}],
            scope_background=["project:Alpha"],
            scope_registry=[{"scope": "project:Alpha"}],
            already_handled_candidate_ids=["done-1"],
        )
        self.assertIn("ONLY-UNRESOLVED", prompt)
        self.assertIn("done-1", prompt)
        self.assertNotIn("Complete turn events", prompt)

    def test_thinking_defaults_to_low_and_config_is_strict(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.yaml"
            config = load_config(path, vault=Path(temporary) / "vault")
            self.assertEqual(config["llm"]["thinking"], DEFAULT_THINKING)
            self.assertEqual(DEFAULT_THINKING, {
                "gate": "low", "summarize": "low", "compact": "low"
            })
            config["llm"]["thinking"]["gate"] = "high"
            save_config(path, config)
            self.assertEqual(load_config(path)["llm"]["thinking"]["gate"], "high")
            config = load_config(path)
            config["llm"]["thinking"]["gate"] = "turbo"
            with self.assertRaises(ValueError):
                save_config(path, config)

    def test_deepseek_low_thinking_and_usage_metrics_are_safe(self):
        captured = []
        def opener(request, timeout):
            del timeout
            captured.append(json.loads(request.data.decode("utf-8")))
            return _Response({
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "{\\\"ok\\\":true}", "reasoning_content": "hidden"},
                }],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                    "total_tokens": 150,
                    "prompt_cache_hit_tokens": 80,
                    "prompt_cache_miss_tokens": 40,
                    "completion_tokens_details": {"reasoning_tokens": 22},
                },
            })
        backend = OpenAICompatibleBackend(
            base_url="https://example.invalid/v1",
            api_key="SECRET-KEY",
            model="deepseek-v4-flash",
            opener=opener,
            json_mode=True,
            provider_name="deepseek",
            thinking={"gate": "low"},
        )
        executor = ModelExecutor(_Service())
        value = executor._complete(
            backend,
            "SECRET-PROMPT",
            system="SECRET-SYSTEM",
            purpose="gate",
            metric_stage="gate",
            metric_operation="gate_primary",
        )
        self.assertEqual(value, '{"ok":true}')
        self.assertEqual(captured[0]["thinking"], {"type": "enabled"})
        self.assertEqual(captured[0]["reasoning_effort"], "low")
        call = executor.metrics()["calls"][0]
        self.assertEqual(call["prompt_tokens"], 120)
        self.assertEqual(call["prompt_cache_hit_tokens"], 80)
        self.assertEqual(call["reasoning_tokens"], 22)
        self.assertEqual(call["thinking_mode"], "low")
        serialized = json.dumps(executor.metrics(), ensure_ascii=False)
        self.assertNotIn("SECRET-PROMPT", serialized)
        self.assertNotIn("SECRET-SYSTEM", serialized)
        self.assertNotIn("SECRET-KEY", serialized)

    def test_deepseek_disabled_thinking_payload(self):
        captured = []
        def opener(request, timeout):
            del timeout
            captured.append(json.loads(request.data.decode("utf-8")))
            return _Response({
                "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]
            })
        backend = OpenAICompatibleBackend(
            base_url="https://example.invalid/v1",
            api_key="key",
            model="deepseek-v4-flash",
            opener=opener,
            provider_name="deepseek",
            thinking={"gate": "disabled"},
        )
        self.assertEqual(backend.complete("x", purpose="gate"), "ok")
        self.assertEqual(captured[0]["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", captured[0])


if __name__ == "__main__":
    unittest.main()
''',
)

path = "tests/test_new_scope_source_grounding.py"
regex_once(
    path,
    r"    def test_registered_project_alias_keeps_existing_scope_behavior\(self\) -> None:.*?\n\n\nif __name__ == \"__main__\":",
    '''    def test_registered_project_alias_requires_alias_in_bound_source(self) -> None:
        units = analyze_turn_evidence([{
            "role": "user",
            "content": "OR-1 release plan is pending.",
            "event_key": "source",
        }])
        candidate = {
            "scope_source": "model",
            "worth": True,
            "scopes": ["project:Orion"],
            "_evidence_bindings": [{
                "unit_id": units[0].unit_id,
                "quote": units[0].text,
                "role": "source_excerpt",
            }],
        }
        self.assertTrue(_model_project_scope_is_source_grounded(
            candidate, units, {"project:Orion": {"aliases": ["OR-1"]}},
        ))
        other = analyze_turn_evidence([{
            "role": "user",
            "content": "A source without the configured name or alias.",
            "event_key": "other",
        }])
        candidate["_evidence_bindings"] = [{
            "unit_id": other[0].unit_id,
            "quote": other[0].text,
            "role": "source_excerpt",
        }]
        self.assertFalse(_model_project_scope_is_source_grounded(
            candidate, other, {"project:Orion": {"aliases": ["OR-1"]}},
        ))


if __name__ == "__main__":''',
)

path = "tests/test_v023_scope_correction.py"
replace_once(
    path,
    '''        processor = Processor(self.service)
        self.assertIsNone(processor.inputs._turn_evidence_project_scope(turn, self.service.vault.config()))
        self.assertFalse(processor.inputs._scope_evidence_conflict(candidate, turn, self.service.vault.config()))
''',
    '''        processor = Processor(self.service)
        self.assertIsNone(processor.inputs._turn_evidence_project_scope(turn, self.service.vault.config()))
        self.assertFalse(hasattr(processor.inputs, "_scope_evidence_conflict"))
''',
)

path = "tests/test_processing_observability_concurrency.py"
replace_once(
    path,
    '''        self.assertEqual(stage["output_chars"], len("bad") + len('{"ok":true}'))
        serialized = json.dumps(metrics, ensure_ascii=False)
''',
    '''        self.assertEqual(stage["output_chars"], len("bad") + len('{"ok":true}'))
        self.assertEqual(metrics["operations"]["semantic_review_primary"]["call_count"], 1)
        self.assertEqual(metrics["operations"]["semantic_review_format_repair"]["call_count"], 1)
        self.assertEqual([row["operation"] for row in metrics["calls"]], [
            "semantic_review_primary", "semantic_review_format_repair"
        ])
        serialized = json.dumps(metrics, ensure_ascii=False)
''',
)

# ---------------------------------------------------------------------------
# 6. Version metadata and release notes.
# ---------------------------------------------------------------------------
replace_once("pyproject.toml", 'version = "0.2.37"', 'version = "0.2.38"')
replace_once("src/memleaf/__init__.py", '__version__ = "0.2.37"', '__version__ = "0.2.38"')
replace_once("README.md", '> **版本：0.2.37。**', '> **版本：0.2.38。**')
replace_once("README.en.md", '> **Version: 0.2.37.**', '> **Version: 0.2.38.**')
manifest_count = 0
for manifest in Path(".").rglob("*.yaml"):
    if ".github" in manifest.parts:
        continue
    text = manifest.read_text(encoding="utf-8")
    if "Hermes-native external MemoryProvider" in text and "version: 0.2.37" in text:
        manifest.write_text(text.replace("version: 0.2.37", "version: 0.2.38", 1), encoding="utf-8")
        manifest_count += 1
if manifest_count != 1:
    raise SystemExit(f"expected one Hermes provider manifest, got {manifest_count}")

changelog = read("CHANGELOG.md")
marker = "All notable changes to memleaf are documented here.\n\n"
if marker not in changelog:
    raise SystemExit("CHANGELOG header not found")
section = '''## 0.2.38 — 2026-09-09

- Unify automatic project-Scope grounding: registered and newly named model-selected projects now use the same exact candidate-bound source check. Remove the later registered-name occurrence conflict scan that could misclassify an implementation platform/product mention as ownership and reject the correct new project.
- Strengthen final CREATE/UPDATE semantic review so a `project:<name>` Scope with `scope_source=model` is itself treated as an affiliation claim; product/platform/system/notification/implementation mentions cannot authorize project ownership, and an explicit contradictory owner defers instead of silently changing Scope.
- Add safe per-model-call telemetry with fixed operation classes (`gate_primary`, `gate_coverage_repair`, format repair, summarize/review/coordination variants), request duration, input/output size, provider token usage, DeepSeek cache-hit/miss tokens and reasoning-token counts when supplied. Prompt/response text and credentials are never persisted.
- Add explicit `llm.thinking` configuration for Gate/summarize/compact. The default is `low`, retaining reasoning at the lowest supported effort; users may select `disabled`, `default`, `high`, or `max` explicitly. DeepSeek OpenAI-format calls send the corresponding thinking controls.
- Reduce Gate input cost by removing a duplicated system-policy tail and replace coverage re-checks with a narrow unresolved-evidence protocol instead of rerunning the full Gate prompt. Deterministic validation does not claim a specific real-provider latency reduction.

'''
write("CHANGELOG.md", changelog.replace(marker, marker + section, 1))

# Self-delete so the patch helper never appears in the final source tree.
Path(__file__).unlink()
