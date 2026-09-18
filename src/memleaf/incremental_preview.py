"""Read-only integration of the experimental incremental protocol.

This is deliberately not a second production writer. It prepares a single model
request and compiles a supplied response against a rechecked snapshot. It never
calls an LLM, captures messages, changes a budget or marks evidence settled.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from .incremental_protocol import PlanningSnapshot, compile_incremental, MAX_BYTES
from .incremental_prompts import INCREMENTAL_SYSTEM
from .index import turn_key, normalize_term
from .inbox import parse_inbox_file, source_ordered_turns, captured_turn_selector
from .models import Memory
from .incremental_native import read_comparison
from .process_common import _read_processed
from .retrieval import candidate_matches_query, fulltext_score
from .scope_state import normalize_scopes
from .vault import safe_component


def _prepare_incremental_unlocked(service: Any, *, source: str, session_id: str, turn_id: str,
                        scope: Any = None, priority_memory_ids: Iterable[str] = (),
                        candidate_limit: int = 12, allow_new_scopes: bool = False,
                        selection: dict[str, Any] | None = None,
                        retention_request: str | None = None,
                        captured_turn_key: str | None = None) -> PlanningSnapshot:
    """Build a bounded current-source/target snapshot under the existing lock.

    At this stage evidence units are complete visible messages, not per-sentence
    heuristic fragments. Caller-provided target IDs are required context and are
    never dropped to fit the budget. Other candidates share the budget by query.
    """
    source = safe_component(source, "source")
    session_id = safe_component(session_id, "session id")
    if not isinstance(turn_id, str) or not turn_id:
        raise ValueError("invalid_turn_id")
    if type(candidate_limit) is not int or not 1 <= candidate_limit <= 20:
        raise ValueError("invalid_candidate_limit")
    from .incremental_scopes import registry_view, resolve_scope
    registry, scope_guard, scope_aliases = registry_view(service.vault.config())
    boundary = normalize_scopes(scope) if scope is not None else None
    if boundary is not None:
        boundary = list(dict.fromkeys(resolve_scope(s, {k: k for k in registry}, scope_aliases) or s
                                      for s in boundary))
    if isinstance(priority_memory_ids, (str, bytes)):
        raise ValueError("invalid_priority_ids")
    priority = list(dict.fromkeys(priority_memory_ids))
    if len(priority) > candidate_limit:
        raise ValueError("blocked_context")
    for identity in priority:
        safe_component(identity, "memory id")
    # Do not use the mutation boundary: it would resume compaction writes.
    path = service.vault._inside("inbox", source, f"{session_id}.md")
    turns = source_ordered_turns(parse_inbox_file(path))
    selected = next((t for t in turns if t.turn_key == captured_turn_selector(turn_id, captured_turn_key)), None)
    from .explicit_text_source import explicit_turn, check_origin
    if selected is None or not (selected.complete or explicit_turn(selected)):
        raise ValueError("source_not_complete")
    if explicit_turn(selected):
        from .incremental_journal import digest
        if not selection or digest(selection.get("intent_id")) != selected.events[0].explicit_input["intent_hash"]:
            raise ValueError("explicit_text_requires_matching_intent")
        from .remember_route import _REQUEST
        origin = selected.events[0].explicit_input
        if (digest(normalize_scopes(scope) if scope is not None else None) != origin["scope_hash"]
                or selection.get("request_hash") != digest(_REQUEST)):
            raise ValueError("explicit_text_binding_changed")
        check_origin(service, selected)
    selected_index = turns.index(selected)
    later = turns[selected_index + 1:]
    if any(not t.complete for t in later):
        raise ValueError("blocked_context")
    processed = _read_processed(service.vault.processed_state_path)
    state = processed.get("sessions", {}).get(f"{source}/{session_id}", {})
    available = {t.turn_key for t in turns}
    # Missing subsequent raw source cannot be replaced by an invented summary.
    missing_window = any(
        isinstance(entry, dict) and type(entry.get("turn_index")) is int
        and entry["turn_index"] > selected.turn_index and entry.get("turn_key") not in available
        for entry in state.get("processed_turns", [])
    )
    if missing_window:
        raise ValueError("blocked_context")
    # Normal automatic extraction is one complete turn: the selected user
    # input plus its final assistant reply. Later turns are retained only as
    # delayed/recovery safety context; never pull the previous turn into a
    # normal model request merely to resolve conversational shorthand.
    contexts = later
    evidence = []
    for t, use in [(selected, "new"), *[(t, "context") for t in contexts]]:
        for event in t.events:
            if not event.content.strip() or event.role not in {"user", "assistant"}:
                continue
            evidence.append({
                "ref": f"e{len(evidence) + 1}", "use": use, "role": event.role,
                "text": event.content, "source": event.source, "session_id": event.session_id,
                "event_key": event.event_key, "message_id": event.message_id,
                "message_revision": event.message_revision, "source_time": event.source_time,
                "source_sequence": event.source_sequence,
                **({"explicit_input": event.explicit_input} if event.explicit_input is not None else {}),
            })
    from .incremental_selection import validate_selection, validate_request, select_evidence
    selection = validate_selection(selection)
    if selection is not None:
        retention_request = validate_request(retention_request, selection)
        evidence = select_evidence(evidence, selection)
    elif retention_request is not None:
        raise ValueError("unexpected_retention_request")
    from .query_scan import scan_memories, ensure_scan_current
    library = scan_memories(service.vault)
    if library.ambiguous:
        raise ValueError("duplicate_memory_id")
    if library.issues:
        raise ValueError("incomplete_library")
    records = {record.memory.memory_id.casefold(): record.memory for record in library.records}
    native = read_comparison(service, source)
    native_keys = {key.casefold() for key in native.memories}
    if native_keys.intersection(records):
        raise ValueError("duplicate_memory_id")
    records.update({key.casefold(): memory for key, memory in native.memories.items()})
    # A real source revision must still be compared to prior affected identities.
    prior_ids = set()
    for entry in state.get("revised_turns", []):
        if isinstance(entry, dict) and entry.get("turn_key") == selected.turn_key:
            prior_ids.update(x for x in entry.get("memory_ids", []) if isinstance(x, str))
    from .incremental_journal import KEY, load_work
    works = processed.get(KEY, {})
    if not isinstance(works, dict):
        raise ValueError("invalid_incremental_ledger")
    for key in works:
        work = load_work(processed, key)
        if work["source"] == source and work["session_id"] == session_id and work["turn_key"] == selected.turn_key:
            prior_ids.update(op["memory_id"] for op in work["operations"] if op.get("memory_id"))
    priority += sorted(x for x in prior_ids if x.casefold() in records and x not in priority)
    if len(priority) > candidate_limit:
        raise ValueError("blocked_context")
    chosen = []
    for identity in priority:
        if identity.casefold() not in records:
            raise ValueError("required_target_unavailable")
        chosen.append(records[identity.casefold()])
    def update_candidate_score(memory, query):
        # Public directory search deliberately rejects short local substrings.
        # Update planning has a different recall obligation: if this complete
        # turn explicitly contains an existing non-ASCII title, the full title
        # is strong target evidence even inside a longer sentence. Keep generic
        # two-character titles on the stricter rule; never pull a previous turn
        # merely to make target recall succeed.
        title = normalize_term(memory.title)
        text = normalize_term(query)
        exact_title = bool(title and len(title) >= 3 and not title.isascii() and title in text)
        if not exact_title and not candidate_matches_query(memory, query):
            return None
        return fulltext_score(memory, query) + (1000 + len(title) if exact_title else 0)

    query_rows = []
    for event in evidence:
        query = event["text"]
        # Separate local/native lanes so a long local topic cannot consume all
        # candidate slots before a short native match gets a chance.
        for is_native in (False, True):
            scored = []
            for key, memory in records.items():
                if (key in native_keys) != is_native:
                    continue
                score = update_candidate_score(memory, query)
                if score is not None:
                    scored.append((score, memory))
            scored.sort(key=lambda item: (-item[0], item[1].memory_id))
            query_rows.append([memory for _, memory in scored[:candidate_limit]])
    # Reuse the pure retrieval functions, not service._search_unlocked(),
    # whose index accessor may rebuild files or recover compaction.
    seen = {memory.memory_id.casefold() for memory in chosen}
    for rank in range(candidate_limit):
        for candidates in query_rows:
            if len(chosen) >= candidate_limit:
                break
            if rank < len(candidates):
                candidate = candidates[rank]
                key = candidate.memory_id.casefold()
                if key not in seen:
                    chosen.append(candidate)
                    seen.add(key)
    scopes = sorted({s for m in chosen for s in m.scopes} | set(boundary or []) | set(registry))
    scope_refs = {f"s{i}": s for i, s in enumerate(scopes, 1) if s not in {"global", "unscoped"}}
    targets = {f"m{i}": memory for i, memory in enumerate(chosen, 1)}
    writable = {ref: memory.memory_id not in native.bindings and
                (boundary is None or set(memory.scopes) <= set(boundary)) for ref, memory in targets.items()}
    native_targets = {ref: native.bindings[memory.memory_id] for ref, memory in targets.items()
                      if memory.memory_id in native.bindings}
    from .incremental_protocol import applied_revision_index, basis_status
    proofs = applied_revision_index(processed)
    statuses = {ref: basis_status(memory, proofs) for ref, memory in targets.items() if ref not in native_targets}
    ensure_scan_current(service.vault, library)
    binding = service.vault.identity_status()
    return PlanningSnapshot.build(evidence=evidence, targets=targets, scopes=scope_refs,
                                  write_scopes=boundary, writable=writable,
                                  allow_new_scopes=allow_new_scopes, native_targets=native_targets,
                                  native_guard=native.guard if native.guard["sources"] else None,
                                  request_kind="explicit_remember" if selection else "automatic",
                                  retention_request=retention_request, scope_guard=scope_guard,
                                  scope_aliases=scope_aliases, basis_statuses=statuses,
                                  vault_binding=binding if binding["status"] == "bound" else None)


def prepare_incremental(service: Any, **arguments: Any) -> PlanningSnapshot:
    """Prepare only; never resume writes, settle evidence or call a model."""
    with service.vault.lock():
        return _prepare_incremental_unlocked(service, **arguments)


def preview_incremental(service: Any, *, response: str | None = None,
                        expected_snapshot: str | None = None, **arguments: Any) -> dict[str, Any]:
    """Prepare a model request, or compile a response only if the snapshot agrees.

    ``expected_snapshot`` is a comparison token, NOT authorization to commit. The
    supplied response may come from an explicitly authorized isolated evaluation.
    There is no automatic retry or connection to process()/remember() here.
    """
    snapshot = prepare_incremental(service, **arguments)
    if response is not None:
        if expected_snapshot != snapshot.snapshot_id:
            raise ValueError("stale_planning_snapshot")
        return compile_incremental(response, snapshot)
    payload = json.dumps(snapshot.model_input(), ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode("utf-8")) + len(INCREMENTAL_SYSTEM.encode("utf-8")) > MAX_BYTES:
        raise ValueError("blocked_context")
    return {"mode": "preview", "snapshot_id": snapshot.snapshot_id,
            "source_refs": [{"ref": e["ref"], "source_ref": e["event_key"], "role": e["role"]}
                            for e in snapshot.state()["evidence"] if e["use"] == "new"],
            "request": {"system": INCREMENTAL_SYSTEM, "user": payload},
            "model_calls": 0, "memories_written": 0,
            "native_comparison": {"status": "available" if snapshot.state().get("native_guard") else "no_eligible_sources",
                                  "selected_fragments": sum("native" in t for t in snapshot.state()["targets"].values()),
                                  "selection": "bounded_candidates", "read_only": True},
            "limitations": ["native_conflict_coordination_not_automatic", "commit_not_integrated", "coverage_is_not_semantic_quality"]}
