"""Host proposal contracts and deterministic, dependency-free shape validation.

This implements the JSON Schema keywords used by the bundled contracts only.
Authorization, evidence, revisions and durable effects belong to the runtime.
"""
from __future__ import annotations

import copy
import json
import math
import re
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "memleaf-host-v2.0-rc1"
CONTRACT_DIRECTORY = Path(__file__).with_name("host_v2_contracts")
_KEYWORDS = frozenset(("$schema", "$id", "$defs", "$ref", "description", "title",
                      "type", "const", "enum", "allOf", "anyOf", "oneOf", "not",
                      "if", "then", "else", "required", "properties", "additionalProperties",
                      "minProperties", "minItems", "maxItems", "items", "uniqueItems",
                      "minLength", "maxLength", "pattern", "format", "minimum", "maximum"))
HOST_INSTRUCTIONS = (
    "Memleaf host-v2 uses your existing host model; Memleaf never invokes another model. "
    "Inspect memory_capabilities for supported actions and authorization. For a completed visible "
    "user/assistant turn, prepare_memory(mode=new) binds its source to one work. Inline text is "
    "caller_asserted, not proof of user authorization; never include hidden prompts, tools, mail or "
    "attachments as visible conversation. Read relevant memory_work sources/targets pages before "
    "using their content; directory titles are not evidence. Submit one proposal covering every "
    "source with exact evidence. A suggestion or assistant report is not a user decision. "
    "Use actionable independently of memory type for a project/event that is itself an action; "
    "preserve unknown responsibility and dates. Explicit requests to remember cannot use NO_MEMORY: "
    "use CREATE/UPDATE, NO_CHANGE for an already represented fact, or DEFERRED for missing evidence. "
    "Only a durable settled receipt proves saving. Read cumulative receipt and repair unresolved "
    "items in the same work: refresh its snapshot and keep item IDs; never resubmit settled items. "
    "Use resume_memory only for frozen interrupted operations; it does not re-plan or call a model. "
    "Do not create new work, source IDs or connections to bypass the three-attempt budget. "
    "Respect source trust, grants, scopes, barriers and capability limits on every call. "
    "Maintenance compaction and deletion require a precise owner approval. Batch compatible changes "
    "into one submit; fetch additional pages only when needed."
)


class SchemaValidationError(ValueError):
    """A controlled error with no source/body contents in its diagnostic."""
    code = "INVALID_SCHEMA"

    def __init__(self, path: str, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


@lru_cache(maxsize=1)
def _tools() -> dict[str, dict[str, Any]]:
    return {p.stem: json.loads(p.read_text(encoding="utf-8"))
            for p in sorted((CONTRACT_DIRECTORY / "tools").glob("*.json"))}


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "integer"
    if type(value) is float:
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def _client_schema(schema: dict[str, Any], *, shape_hints: bool) -> dict[str, Any]:
    """Inline only reachable local definitions for clients that cannot render refs.

    Inputs use a bounded flat display superset because some clients cannot
    render object unions. Core always validates the untouched original asset.
    """
    memo: dict[str, Any] = {}

    def inline(value: Any, stack: tuple[str, ...] = ()) -> Any:
        if isinstance(value, list):
            return [inline(child, stack) for child in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            ref = value["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/") or ref in stack:
                raise RuntimeError("client contract requires acyclic local references")
            if ref not in memo:
                target = schema
                for part in ref[2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
                memo[ref] = inline(target, stack + (ref,))
            target = copy.deepcopy(memo[ref])
            siblings = {key: child for key, child in value.items()
                        if key not in {"$ref", "$defs", "$schema", "$id"}}
            if not siblings:
                return target
            # A reference and sibling assertions both apply in draft 2020-12.
            return {"allOf": [target, inline(siblings, stack)]}
        result = {key: inline(child, stack) for key, child in value.items()
                  if key not in {"$defs", "$schema", "$id"}}
        if "type" not in result and ("const" in result or "enum" in result):
            options = [result["const"]] if "const" in result else result["enum"]
            kinds = list(dict.fromkeys(_json_type(option) for option in options))
            if "number" in kinds and "integer" in kinds:
                kinds.remove("integer")
            if kinds:
                result["type"] = kinds[0] if len(kinds) == 1 else kinds
        return result

    resolved = inline(schema)
    return _flat_input_schema(resolved) if shape_hints else resolved


def _flat_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """A client display schema, never an authorization or execution validator."""
    def kinds(node):
        value = node.get("type", [])
        return value if isinstance(value, list) else [value]

    def join(nodes):
        unique = {json.dumps(node, sort_keys=True, separators=(",", ":")): node for node in nodes}
        nodes = list(unique.values())
        if len(nodes) == 1:
            return copy.deepcopy(nodes[0])
        types = list(dict.fromkeys(kind for node in nodes for kind in kinds(node)))
        if not types or any(not kinds(node) for node in nodes):
            return {}
        result = {"type": types[0] if len(types) == 1 else types}
        if all("enum" in node for node in nodes):
            values = []
            for node in nodes:
                for value in node["enum"]:
                    if not any(_equal(value, previous) for previous in values):
                        values.append(value)
            result["enum"] = values
        if "object" in types:
            objects = [node for node in nodes if "object" in kinds(node)]
            properties = {}
            for node in objects:
                for name, field in node.get("properties", {}).items():
                    properties.setdefault(name, []).append(field)
            result["properties"] = {name: join(options) for name, options in properties.items()}
            result["required"] = sorted(set.intersection(*(set(node.get("required", [])) for node in objects)))
            result["additionalProperties"] = not all(node.get("additionalProperties") is False for node in objects)
        # Keep the widest branch bounds; constraints apply only to their JSON
        # type, so a nullable string still retains its string length bounds.
        for family, lower, upper in (("string", "minLength", "maxLength"),
                                      ("array", "minItems", "maxItems"),
                                      ("object", "minProperties", "maxProperties")):
            applicable = [node for node in nodes if family in kinds(node)]
            if applicable and all(lower in node for node in applicable):
                result[lower] = min(node[lower] for node in applicable)
            if applicable and all(upper in node for node in applicable):
                result[upper] = max(node[upper] for node in applicable)
        numeric = [node for node in nodes if set(kinds(node)) & {"number", "integer"}]
        for key, aggregate in (("minimum", min), ("maximum", max)):
            if numeric and all(key in node for node in numeric):
                result[key] = aggregate(node[key] for node in numeric)
        strings = [node for node in nodes if "string" in kinds(node)]
        for key in ("pattern", "format"):
            if strings and all(key in node and node[key] == strings[0].get(key) for node in strings):
                result[key] = strings[0][key]
        arrays = [node for node in nodes if "array" in kinds(node)]
        if arrays and all("items" in node for node in arrays):
            result["items"] = join([node["items"] for node in arrays])
        if arrays and all(node.get("uniqueItems") for node in arrays):
            result["uniqueItems"] = True
        return result

    def flatten(node):
        if not isinstance(node, dict):
            return node
        result = {key: copy.deepcopy(value) for key, value in node.items()
                  if key not in {"oneOf", "anyOf", "allOf", "if", "then", "else", "not"}}
        if "const" in result:
            result["enum"] = [result.pop("const")]
        if "properties" in result:
            result["properties"] = {name: flatten(field) for name, field in result["properties"].items()}
        if "items" in result:
            result["items"] = flatten(result["items"])
        branches = node.get("oneOf", node.get("anyOf", []))
        if branches:
            choices = [flatten(branch) for branch in branches]
            if all(kinds(choice) for choice in choices):
                combined = join(choices)
                # Contract unions have no independent overlapping field
                # assertions. Keep surrounding annotations/type declarations.
                combined.update(result)
                result = combined
            elif "type" not in result and all("required" in choice for choice in choices):
                result = {"type": "object", "required": sorted(set.intersection(
                    *(set(choice["required"]) for choice in choices)))}
        return result

    return flatten(schema)


_INPUT_HELP = {
    "prepare_memory": " New work: mode=new, purpose, request_id, source={kind:inline,messages:[{role:user,text:...}]}; refresh uses work_id and expected_work_revision.",
    "submit_memory": (
        " Every item needs item_id/action. Minimal UPDATE item: "
        '{"item_id":"A","action":"UPDATE","update_kind":"patch","assertion_kind":"user_decision",'
        '"target":"m1","fields":{"assignee":"Mira"},"evidence":[{"source":"eN","quote":"Mira owns this task."}],'
        '"field_evidence":[{"field":"assignee","evidence":[{"source":"eN","quote":"Mira owns this task."}]}]}.'
        " Replace refs/values/quotes with the actual snapshot/source. fields contains only changes. "
        "field_evidence is an ARRAY of {field,evidence:[{source,quote}]}; field matches a changed fields key. "
        "assertion_kind belongs only on the item. If body is unchanged, omit body_patch entirely (never []). "
        "Do not add expected_revision to an item. CREATE: assertion_kind,memory={type,title,body,scopes:[global]},evidence; todo also status. "
        "UPDATE restore: target,evidence,body; retract_memory: target,evidence. NO_CHANGE: target,evidence. "
        "NO_MEMORY: source_refs,reason=no_future_value|question_only|temporary|already_represented_in_this_work; forbidden for explicit retention. "
        "DEFERRED: source_refs,target_refs (at least one nonempty),reason=missing_evidence|ambiguous_target|needs_user_decision|insufficient_context|unsupported_action,need=short explanation. "
        "MERGE: target,duplicates,body_mode=preserve_union,evidence. COMPACT: target,new_body,mapping,approval_ref. "
        "coverage:[{source:eN,item_ids:[A]}]; basis_coverage:[] for ordinary work. Use source, not source_ref; scopes is an array. "
        "Display schema is a flat superset; Core validates action-specific fields."
    ),
    "memory_work": " sources/targets views require work_id, snapshot_id and source_refs/target_refs arrays; summary/receipt use work_id; list omits work_id.",
    "forget_memory": " mode=preview uses memory_ids; mode=apply uses deletion_plan_id, expected_plan_revision and approval_ref from the exact owner-approved plan.",
}


@lru_cache(maxsize=1)
def _client_tools() -> list[dict[str, Any]]:
    result = []
    for definition in _tools().values():
        tool = copy.deepcopy(definition)
        tool["inputSchema"] = _client_schema(tool["inputSchema"], shape_hints=True)
        tool["outputSchema"] = _client_schema(tool["outputSchema"], shape_hints=False)
        tool["description"] += _INPUT_HELP.get(tool["name"], "")
        result.append(tool)
    return result


def tool_definitions() -> list[dict[str, Any]]:
    """Return client-readable schemas, preserving original Core validation assets."""
    return copy.deepcopy(_client_tools())


def _equal(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    return a == b


def _type(value: Any, kind: str) -> bool:
    return {"object": lambda: isinstance(value, dict),
            "array": lambda: isinstance(value, list),
            "string": lambda: isinstance(value, str),
            "boolean": lambda: type(value) is bool,
            "null": lambda: value is None,
            "number": lambda: type(value) is int or (type(value) is float and math.isfinite(value)),
            "integer": lambda: type(value) is int or (
                type(value) is float and math.isfinite(value) and value.is_integer())}[kind]()


def _format(value: str, kind: str) -> bool:
    try:
        if kind == "date":
            return date.fromisoformat(value).isoformat() == value
        if kind == "date-time":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})", value):
                return False
            datetime.fromisoformat(value.replace("t", "T").replace("z", "Z").replace("Z", "+00:00"))
            return True
    except ValueError:
        return False
    raise RuntimeError(f"unsupported contract format: {kind}")


def _validate(schema: Any, value: Any, root: dict[str, Any], path: str) -> None:
    def fail(reason: str) -> None:
        raise SchemaValidationError(path, reason)

    def matches(subschema: Any) -> bool:
        try:
            _validate(subschema, value, root, path)
            return True
        except SchemaValidationError:
            return False

    def discriminate(branches: list[Any]) -> Any:
        if not isinstance(value, dict):
            return None
        candidates = branches
        for name in ("action", "mode", "view", "update_kind", "kind"):
            if name not in value:
                continue
            selected = []
            constrained = False
            for branch in candidates:
                target = branch
                while isinstance(target, dict) and "$ref" in target:
                    ref = target["$ref"]
                    if not ref.startswith("#/"):
                        return None
                    target = root
                    for token in ref[2:].split("/"):
                        target = target[token.replace("~1", "/").replace("~0", "~")]
                field = target.get("properties", {}).get(name, {}) if isinstance(target, dict) else {}
                if "const" in field:
                    constrained = True
                    if _equal(value[name], field["const"]):
                        selected.append(branch)
            if constrained:
                candidates = selected
                if len(candidates) == 1:
                    return candidates[0]
                if not candidates:
                    return None
        return None

    if schema is True:
        return
    if schema is False:
        fail("disallowed")
    if set(schema) - _KEYWORDS:
        raise RuntimeError("unsupported contract schema keyword")
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            raise RuntimeError("only bundled local references are supported")
        target = root
        for token in ref[2:].split("/"):
            target = target[token.replace("~1", "/").replace("~0", "~")]
        _validate(target, value, root, path)
    if "const" in schema and not _equal(value, schema["const"]):
        fail("constant mismatch")
    if "enum" in schema and not any(_equal(value, option) for option in schema["enum"]):
        fail("value outside enum")
    if "type" in schema:
        kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type(value, kind) for kind in kinds):
            fail("incorrect type")
    for sub in schema.get("allOf", []):
        _validate(sub, value, root, path)
    if "anyOf" in schema and not any(matches(sub) for sub in schema["anyOf"]):
        fail("no allowed alternative")
    if "oneOf" in schema:
        count = sum(matches(sub) for sub in schema["oneOf"])
        if count != 1:
            if count == 0:
                branch = discriminate(schema["oneOf"])
                if branch is not None:
                    # The selected branch's precise failure is safe schema
                    # feedback. Successful branch validation still cannot
                    # bypass the original exactly-one requirement.
                    _validate(branch, value, root, path)
            fail("expected exactly one alternative")
    if "not" in schema and matches(schema["not"]):
        fail("prohibited combination")
    if "if" in schema:
        branch = "then" if matches(schema["if"]) else "else"
        if branch in schema:
            _validate(schema[branch], value, root, path)
    if isinstance(value, dict):
        missing = [key for key in schema.get("required", []) if key not in value]
        if missing:
            fail("missing required properties: " + ", ".join(missing))
        if len(value) < schema.get("minProperties", 0):
            fail("too few properties")
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                _validate(properties[key], item, root, f"{path}.{key}")
            elif "additionalProperties" in schema:
                _validate(schema["additionalProperties"], item, root, path + ".<extra>")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
            fail("array length outside limits")
        if schema.get("uniqueItems") and any(
                _equal(value[i], value[j]) for i in range(len(value)) for j in range(i)):
            fail("duplicate array item")
        if "items" in schema:
            for index, item in enumerate(value):
                _validate(schema["items"], item, root, f"{path}[{index}]")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", math.inf):
            fail("text length outside limits")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            fail("text pattern mismatch")
        if "format" in schema and not _format(value, schema["format"]):
            fail("invalid text format")
    if type(value) in (int, float):
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            fail("number outside limits")


def validate_schema(schema: dict[str, Any], value: Any) -> None:
    """Validate a bundled schema; not a general purpose third-party validator."""
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise SchemaValidationError("$", "not a finite JSON value") from None
    try:
        _validate(schema, value, schema, "$")
    except RecursionError:
        raise SchemaValidationError("$", "nesting limit exceeded") from None


def validate_request(tool: str, args: Any, *, routable_only: bool = False) -> None:
    if tool not in _tools():
        raise SchemaValidationError("$", "unknown host tool")
    schema = _tools()[tool]["inputSchema"]
    if routable_only and tool == "submit_memory":
        schema = copy.deepcopy(schema)
        # Accepted shells consume an attempt even when proposal shape is invalid.
        for key in ("items", "coverage", "basis_coverage"):
            schema["properties"][key] = True
    validate_schema(schema, args)


def validate_response(tool: str, payload: Any) -> None:
    if tool not in _tools():
        raise SchemaValidationError("$", "unknown host tool")
    validate_schema(_tools()[tool]["outputSchema"], payload)
