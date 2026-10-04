"""Bounded lifecycle facts for explicitly requested historical retrieval."""
from collections.abc import Mapping
from typing import Any

from .query_scan import QueryScan, ScanRecord
from .vault import safe_component


def current_validities(snapshot: QueryScan) -> dict[str, str]:
    # Only validated, unambiguous current records can establish current state.
    unavailable = snapshot.ambiguous | {i.identity.casefold() for i in snapshot.issues if i.identity}
    return {r.memory.memory_id: r.memory.validity
            for r in snapshot.area("knowledge")
            if r.memory.memory_id.casefold() not in unavailable}


def lifecycle(record: ScanRecord, current: Mapping[str, str]) -> dict[str, Any]:
    historical = record.area == "history"
    result = {"historical": historical,
              "current_validity": None if historical else record.memory.validity}
    if not historical:
        return result
    extra = record.memory.extra
    links = [extra[k] for k in ("active_memory_id", "original_memory_id") if k in extra]
    if links and all(isinstance(v, str) and len(v) <= 800 and v == links[0] for v in links):
        try:
            identity = safe_component(links[0], "active memory id")
        except ValueError:
            pass
        else:
            result.update(active_memory_id=identity, current_validity=current.get(identity))
    reason = extra.get("invalidated_reason")
    if isinstance(reason, str) and 0 < len(reason) <= 120:
        result["invalidated_reason"] = reason
    return result


def project_lifecycle(value: Any) -> dict[str, Any]:
    """Do not pass arbitrary archive extras through the MCP boundary."""
    if (not isinstance(value, Mapping)
            or not {"historical", "current_validity"} <= set(value)
            or set(value) - {"historical", "current_validity", "active_memory_id", "invalidated_reason"}
            or type(value["historical"]) is not bool
            or value["current_validity"] not in (None, "valid", "retracted")):
        raise ValueError("invalid retrieval lifecycle")
    result = dict(value)
    if not value["historical"] and (value["current_validity"] is None
                                   or set(value) != {"historical", "current_validity"}):
        raise ValueError("invalid current lifecycle")
    if value["historical"] and value["current_validity"] is not None and "active_memory_id" not in value:
        raise ValueError("current validity requires an exact archive link")
    if "active_memory_id" in value:
        if not isinstance(value["active_memory_id"], str) or len(value["active_memory_id"]) > 800:
            raise ValueError("invalid active memory id")
        safe_component(value["active_memory_id"], "active memory id")
    if "invalidated_reason" in value:
        reason = value["invalidated_reason"]
        if not isinstance(reason, str) or not 0 < len(reason) <= 120:
            raise ValueError("invalid invalidation reason")
    return result
