"""Read-only historical comparisons, separate from actionable memory targets."""
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .models import Memory
from .retrieval_lifecycle import project_lifecycle
from .turn_plan import revision_digest
from .vault import safe_component


def freeze_history(value: Any, targets: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("invalid_history_context")
    current = {t["memory"]["memory_id"]: t for t in targets.values()}
    identities = {identity.casefold() for identity in current}
    result = {}
    for identity, item in value.items():
        if (not isinstance(identity, str) or not isinstance(item, Mapping)
                or set(item) != {"memory", "lifecycle"}
                or not isinstance(item["memory"], Memory)
                or identity != item["memory"].memory_id):
            raise ValueError("invalid_history_context")
        safe_component(identity, "memory id")
        if identity.casefold() in identities:
            raise ValueError("duplicate_memory_id")
        identities.add(identity.casefold())
        life = project_lifecycle(item["lifecycle"])
        if not life["historical"]:
            raise ValueError("invalid_history_context")
        head = current.get(life.get("active_memory_id"))
        validity = head["memory"].get("validity", "valid") if head and "native" not in head else None
        if life["current_validity"] != validity:
            raise ValueError("invalid_history_context")
        result[identity] = {"memory": item["memory"].to_dict(),
                            "revision": revision_digest(item["memory"]), "lifecycle": life}
    return result


def thaw_history(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("invalid_history_context")
    result = {}
    for identity, item in value.items():
        if (not isinstance(item, Mapping) or set(item) != {"memory", "revision", "lifecycle"}
                or not isinstance(item["memory"], Mapping)):
            raise ValueError("invalid_history_context")
        memory = Memory.from_mapping(item["memory"])
        if revision_digest(memory) != item["revision"]:
            raise ValueError("invalid_history_context")
        result[identity] = {"memory": memory, "lifecycle": item["lifecycle"]}
    return result


def history_signature(state: Mapping[str, Any]) -> dict[str, Any]:
    # Current validity is derived from targets, whose changes (including this
    # work's own writes) are already checked by partial recovery.
    return {identity: {"revision": item["revision"],
                       "lifecycle": {k: v for k, v in item["lifecycle"].items()
                                     if k != "current_validity"}}
            for identity, item in state.get("history_context", {}).items()}


def project_history(state: Mapping[str, Any], scope_refs: Mapping[str, str]) -> list[dict[str, Any]]:
    targets = {t["memory"]["memory_id"]: ref for ref, t in state["targets"].items()
               if "native" not in t}
    result = []
    for item in state.get("history_context", {}).values():
        memory = item["memory"]
        row = {key: deepcopy(memory[key]) for key in (
            "type", "title", "body", "status", "validity", "actionable", "assignee",
            "due_date", "due_text", "due_status", "completed_at") if key in memory}
        scopes = memory.get("scopes", ["global"])
        row["scope"] = scope_refs.get(scopes[0], scopes[0]) if len(scopes) == 1 else scopes
        life = dict(item["lifecycle"])
        row["current_target"] = targets.get(life.pop("active_memory_id", None))
        row["lifecycle"] = life
        result.append(row)
    return result
