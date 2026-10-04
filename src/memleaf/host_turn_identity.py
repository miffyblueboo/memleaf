"""Declared, unambiguous continuation of a host session; no latest-turn lookup."""
from __future__ import annotations

from .vault import safe_component


def ancestors(processed, source, session_id):
    sessions = processed.get("sessions", {})
    path = [session_id]
    while True:
        state = sessions.get(f"{source}/{path[-1]}", {})
        parent = state.get("lineage_parent_session_id")
        if parent is None:
            return path
        parent = safe_component(parent, "parent session id")
        if parent in path or len(path) >= 16:
            raise ValueError("invalid_host_retention_lineage")
        path.append(parent)


def continuation_path(processed, source, session_id, origin_session_id):
    path = ancestors(processed, source, session_id)
    if origin_session_id not in path:
        return None
    path = path[:path.index(origin_session_id) + 1]
    sessions = processed.get("sessions", {})
    for parent in path[1:]:
        children = [name for name, state in sessions.items()
                    if name.startswith(source + "/")
                    and state.get("lineage_parent_session_id") == parent]
        if len(children) != 1:
            raise ValueError("ambiguous_host_retention_lineage")
    return path
