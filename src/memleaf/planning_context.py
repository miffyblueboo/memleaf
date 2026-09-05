"""Read-only candidate context, Scope grounding and target resolution."""
from __future__ import annotations
import json
import re
from typing import Any, Iterable, Mapping, Optional
from .admission import analyze_turn_evidence, supporting_units
from .inbox import InboxTurn
from .memory_writer import MemoryWriter
from .turn_plan import revision_digest
from .models import Memory
from .native_index import NativeIndexer
from .retrieval import candidate_matches_query, filter_by_scope, normalize_term
from .scope_state import project_scopes_for_domains
from .scope_maintenance import ScopeMaintenanceError, scope_registry_projection
from .validation import is_project_plan_text
from .process_common import ProcessingError, _RELATED_MAX_BODY_CHARS, _RELATED_MAX_CHARS, _RELATED_MAX_ITEMS, _SCOPE_CORRECTION_MARKER_RE, _SCOPE_DIRECTORY_MAX_CHARS, _SCOPE_DIRECTORY_MAX_ITEMS, _SCOPE_DIRECTORY_MAX_TITLE_CHARS, _TARGET_NOT_RELATED, _TARGET_SAME_USE, _TARGET_UNKNOWN, _event_payload, _invoke_native, _merge_related, _native_result, _project_scope_occurrences, _safe_scope_background, _session_key


class PlanningContext:
    def __init__(self, service: Any, audit: Any, journal: Any):
        self.service = service
        self.audit = audit
        self.journal = journal

    def _conversation_title(self, turn: InboxTurn) -> str:
        path = self.journal._session_path_without_create(_session_key(turn.source, turn.session_id))
        if path is not None and path.exists():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.startswith("# Session "):
                        return line[2:].strip()
            except (OSError, UnicodeError):
                pass
        return f"{turn.source}/{turn.session_id}"


    def _scope_registry_projection(self) -> list[dict[str, Any]]:
        with self.service.vault.lock():
            try:
                config = self.service.vault.config()
                return scope_registry_projection(config)
            except (OSError, UnicodeError, ValueError, TypeError, ScopeMaintenanceError) as error:
                raise ProcessingError("invalid scope registry") from error


    @staticmethod
    def _overlay_related(
        related: Iterable[Mapping[str, Any]],
        overlay: Iterable[Mapping[str, Any]] = (),
        *,
        query: str = "",
        scope: Any = None,
    ) -> list[dict[str, Any]]:
        """Overlay same-ID planned memories while retaining relevant results."""

        values = [dict(item) for item in related if isinstance(item, Mapping)]
        by_id: dict[str, dict[str, Any]] = {}
        without_id: list[dict[str, Any]] = []
        for item in values:
            memory_id = item.get("memory_id")
            if isinstance(memory_id, str) and item.get("native") is not True:
                by_id[memory_id.casefold()] = item
            else:
                without_id.append(item)
        for item in overlay:
            if not isinstance(item, Mapping):
                continue
            value = dict(item)
            memory_id = value.get("memory_id")
            if isinstance(memory_id, str) and value.get("native") is not True:
                item_scopes = value.get("scopes")
                if isinstance(scope, str):
                    requested_scopes = {scope.casefold()}
                elif isinstance(scope, (list, tuple, set)):
                    requested_scopes = {
                        item.casefold()
                        for item in scope
                        if isinstance(item, str)
                    }
                else:
                    requested_scopes = set()
                if requested_scopes and isinstance(item_scopes, list):
                    available_scopes = {
                        item.casefold()
                        for item in item_scopes
                        if isinstance(item, str)
                    }
                    if "global" in requested_scopes:
                        if "global" not in available_scopes:
                            continue
                    elif not (
                        available_scopes.intersection(requested_scopes)
                        or "global" in available_scopes
                    ):
                        continue
                normalized_query = normalize_term(query)
                haystack = normalize_term(
                    " ".join(
                        str(value.get(field, ""))
                        for field in ("title", "body")
                    )
                )
                if normalized_query and haystack:
                    query_fragments: list[str] = []
                    for fragment in re.findall(
                        r"[\u4e00-\u9fff]{2,}|[a-z0-9]+",
                        normalized_query,
                        re.UNICODE,
                    ):
                        if re.fullmatch(r"[\u4e00-\u9fff]+", fragment):
                            query_fragments.extend(
                                fragment[index : index + 2]
                                for index in range(len(fragment) - 1)
                            )
                        else:
                            query_fragments.append(fragment)
                    if normalized_query not in haystack and query_fragments and not any(
                        fragment in haystack for fragment in query_fragments
                    ):
                        continue
                by_id[memory_id.casefold()] = value
            else:
                without_id.append(value)
        return _merge_related(list(by_id.values()) + without_id)


    def _related_query(
        self,
        turn: InboxTurn,
        state: Mapping[str, Any],
        query: str | Iterable[str],
        explicit_scope: Any = None,
        *,
        overlay: Iterable[Mapping[str, Any]] = (),
        strict_relevance: bool = False,
        priority_memory_ids: Iterable[str] = (),
        priority_only: bool = False,
        scope_records: Optional[list[Any]] = None,
    ) -> tuple[
        list[dict[str, Any]],
        Any,
        list[dict[str, str]],
        Optional[tuple[list[Any], bool]],
    ]:
        if isinstance(query, str):
            query_value: str | list[str] = query.strip()
        else:
            query_value = [
                str(item).strip()
                for item in query
                if isinstance(item, str) and item.strip()
            ]
        visible = query_value if isinstance(query_value, str) else " ".join(query_value)
        scope = _safe_scope_background(state, explicit_scope)
        local: list[dict[str, Any]] = []
        indexed_native: list[dict[str, Any]] = []
        scope_fallback: Optional[tuple[list[Any], bool]] = None
        with self.service.vault.lock():
            priority_wanted = [
                value.casefold()
                for value in priority_memory_ids
                if isinstance(value, str) and value
            ]
            priority_records: list[Any] = []
            if priority_wanted:
                available = scope_records
                if available is None:
                    available, _ = self._scope_records_unlocked(scope)
                by_id = {
                    record.memory.memory_id.casefold(): record
                    for record in available
                }
                priority_records = [
                    by_id[value] for value in priority_wanted if value in by_id
                ]
            if priority_only:
                # A candidate already selected an active target from the
                # directory.  Reading that target is sufficient; a second
                # full-text search cannot change the candidate and only adds
                # cost (and unrelated context).
                records = priority_records
            else:
                records = self.service._search_unlocked(
                    query_value,
                    scope=scope if scope else None,
                    include_history=False,
                    todo_status="all",
                    limit=None,
                    # Processing needs the same candidate relevance boundary as
                    # the public directory search.  The legacy indexed-first
                    # lookup can return no record for an elliptical follow-up
                    # such as “this project's tasks”, even when the session scope
                    # identifies the project and its active memory is the only
                    # plausible maintenance target.
                    strict_candidates=True,
                ) if visible else []
                if visible and strict_relevance and self._has_specific_scope(scope):
                    records = [
                        record
                        for record in records
                        if candidate_matches_query(record.memory, query_value)
                    ]
                if visible and not records and not priority_records and self._has_specific_scope(scope):
                    scoped_records, ambiguous = self._scope_records_unlocked(scope)
                    scope_fallback = (scoped_records, ambiguous)
                    records = [] if ambiguous else scoped_records
                if priority_records:
                    priority_ids = {
                        record.memory.memory_id.casefold()
                        for record in priority_records
                    }
                    records = priority_records + [
                        record
                        for record in records
                        if record.memory.memory_id.casefold() not in priority_ids
                    ]
            local = _native_result([record.memory for record in records])
            if visible:
                if not priority_only:
                    indexed_native = NativeIndexer(self.service.vault).search_unlocked(
                        query_value,
                        target_agent=turn.source,
                        for_context=False,
                        limit=None,
                    )
        native = (
            _invoke_native(getattr(self.service, "native_memory_reader", None), visible, scope)
            if visible and not priority_only
            else []
        )
        related = self._overlay_related(
            _merge_related(local + indexed_native + native),
            overlay,
            query=visible,
            scope=scope,
        )
        related = self._bound_related(
            related,
            priority_memory_ids=priority_memory_ids,
        )
        native_refs = [
            {
                "source_id": item["native_source_id"],
                "native_id": item["native_id"],
            }
            for item in related
            if item.get("native") is True
            and isinstance(item.get("native_source_id"), str)
            and isinstance(item.get("native_id"), str)
        ]
        return related, scope, native_refs, scope_fallback


    @staticmethod
    def _has_specific_scope(scope: Any) -> bool:
        values = [scope] if isinstance(scope, str) else scope if isinstance(scope, (list, tuple, set)) else []
        return any(isinstance(value, str) and value not in {"", "global", "unscoped"} for value in values)


    @staticmethod
    def _single_specific_scope(scope: Any) -> bool:
        values = [scope] if isinstance(scope, str) else list(scope) if isinstance(scope, (list, tuple, set)) else []
        return len(values) == 1 and isinstance(values[0], str) and values[0] not in {"", "global", "unscoped"}


    @staticmethod
    def _related_payload_size(value: Mapping[str, Any]) -> int:
        try:
            return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError, OverflowError):
            return -1


    @classmethod
    def _bound_related(
        cls,
        related: Iterable[Mapping[str, Any]],
        *,
        priority_memory_ids: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """Keep model related-memory context within the processing budget.

        Update/duplicate targets are placed first, but every body is still
        bounded.  The serialized payload limit also covers metadata, so a
        large tag/alias list cannot bypass the body budget.
        """

        priority = {
            value.casefold()
            for value in priority_memory_ids
            if isinstance(value, str) and value
        }
        values: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for item in related:
            if not isinstance(item, Mapping):
                continue
            value = dict(item)
            memory_id = value.get("memory_id")
            if isinstance(memory_id, str):
                key = memory_id.casefold()
                if key in seen_ids:
                    continue
                seen_ids.add(key)
            values.append(value)
        values.sort(
            key=lambda value: (
                isinstance(value.get("memory_id"), str)
                and value["memory_id"].casefold() in priority,
            ),
            reverse=True,
        )
        selected: list[dict[str, Any]] = []
        used = 2  # The surrounding JSON array brackets.
        for value in values:
            if len(selected) >= _RELATED_MAX_ITEMS:
                break
            body = value.get("body")
            if isinstance(body, str) and len(body) > _RELATED_MAX_BODY_CHARS:
                value["body"] = body[: _RELATED_MAX_BODY_CHARS - 1].rstrip() + "…"
            size = cls._related_payload_size(value)
            if size < 0:
                continue
            additional = size + (1 if selected else 0)
            if used + additional > _RELATED_MAX_CHARS:
                # A priority target still gets a minimal, bounded view when
                # oversized metadata leaves no room for its normal payload.
                memory_id = value.get("memory_id")
                if not (
                    isinstance(memory_id, str)
                    and memory_id.casefold() in priority
                ):
                    continue
                minimal = {
                    key: value[key]
                    for key in ("memory_id", "title", "body", "type", "scopes")
                    if key in value
                }
                size = cls._related_payload_size(minimal)
                if size < 0 or used + size + (1 if selected else 0) > _RELATED_MAX_CHARS:
                    continue
                value = minimal
                additional = size + (1 if selected else 0)
            selected.append(value)
            used += additional
        return selected


    def _scope_records_unlocked(self, scope: Any) -> tuple[list[Any], bool]:
        """Read and rank active records once for a scoped fallback."""

        active_records = self.service._read_memories_unlocked("knowledge")
        scoped = filter_by_scope(
            [record.memory for record in active_records],
            scope,
            self.service.vault.config(),
        )
        ranks = {memory.memory_id.casefold(): rank for memory, rank in scoped}
        records = [
            record
            for record in active_records
            if record.memory.memory_id.casefold() in ranks
        ]
        records.sort(
            key=lambda record: (
                ranks[record.memory.memory_id.casefold()],
                record.memory.updated,
                record.memory.memory_id,
            ),
            reverse=True,
        )
        return records, len(records) > 1


    @classmethod
    def _scope_directory_entry(cls, memory: Memory) -> tuple[dict[str, Any], bool]:
        title = memory.title
        title_truncated = len(title) > _SCOPE_DIRECTORY_MAX_TITLE_CHARS
        if title_truncated:
            title = title[: _SCOPE_DIRECTORY_MAX_TITLE_CHARS - 1].rstrip() + "…"
        return (
            {
                "memory_id": memory.memory_id,
                "title": title,
                "type": memory.type,
                "scopes": list(memory.scopes),
            },
            title_truncated,
        )


    @classmethod
    def _scope_directory(
        cls,
        records: Iterable[Any],
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return a bounded metadata-only directory from scoped records."""

        records = list(records)
        complete = len(records) <= _SCOPE_DIRECTORY_MAX_ITEMS
        directory: list[dict[str, Any]] = []
        used = 2
        for record in records[:_SCOPE_DIRECTORY_MAX_ITEMS]:
            entry, title_truncated = cls._scope_directory_entry(record.memory)
            complete = complete and not title_truncated
            try:
                size = len(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
            except (TypeError, ValueError, OverflowError):
                complete = False
                continue
            additional = size + (1 if directory else 0)
            if used + additional > _SCOPE_DIRECTORY_MAX_CHARS:
                complete = False
                break
            directory.append(entry)
            used += additional
        if len(records) > _SCOPE_DIRECTORY_MAX_ITEMS:
            complete = False
        return directory, complete


    def _related(
        self,
        turn: InboxTurn,
        state: Mapping[str, Any],
        explicit_scope: Any = None,
        *,
        overlay: Iterable[Mapping[str, Any]] = (),
    ) -> tuple[
        list[dict[str, Any]],
        Any,
        list[dict[str, str]],
        Optional[tuple[list[Any], bool]],
    ]:
        visible = " ".join(event.content for event in turn.events if isinstance(event.content, str)).strip()
        return self._related_query(
            turn,
            state,
            visible,
            explicit_scope,
            overlay=overlay,
            strict_relevance=True,
        )


    def _active_memory_by_id(self, memory_id: Any) -> Optional[Memory]:
        """Resolve one active memory, including the processor's write overlay."""

        if not isinstance(memory_id, str) or not memory_id:
            return None
        key = memory_id.casefold()
        try:
            with self.service.vault.lock():
                for record in self.service._read_memories_unlocked("knowledge"):
                    if record.memory.memory_id.casefold() == key:
                        return record.memory
        except (OSError, UnicodeError, ValueError, TypeError):
            return None
        for item in self.audit._planned_related:
            if (
                isinstance(item, Mapping)
                and isinstance(item.get("memory_id"), str)
                and item["memory_id"].casefold() == key
            ):
                try:
                    return Memory.from_mapping(item)
                except (TypeError, ValueError):
                    return None
        return None


    def _turn_evidence_project_scope(
        self,
        turn: InboxTurn,
        config: Mapping[str, Any],
    ) -> str | None:
        domains: list[str] = []
        for event in turn.events:
            for item in getattr(event, "tool_evidence", ()):
                if isinstance(item, Mapping) and isinstance(item.get("domain"), str):
                    domains.append(item["domain"])
        matches = project_scopes_for_domains(domains, config if "scopes" in config else {"scopes": config})
        return matches[0] if len(matches) == 1 else None


    def _scope_evidence_conflict(
        self,
        candidate: Mapping[str, Any],
        turn: InboxTurn,
        config: Mapping[str, Any],
    ) -> bool:
        if candidate.get("worth") is not True:
            return False
        units = supporting_units(candidate, analyze_turn_evidence(_event_payload(turn)))
        selected = {value.casefold() for value in candidate.get("scopes", [])
                    if isinstance(value, str) and value.startswith("project:")}
        registry = config.get("scopes", config)
        for unit in units:
            if unit.origin != "external_observation" and not unit.section_path:
                continue
            grounded = _project_scope_occurrences("\n".join((*unit.section_path, unit.text)), registry)
            if grounded is None:
                return True
            projects = {item[2].casefold() for item in grounded}
            if selected and projects and not selected.issubset(projects):
                return True
            if selected and unit.section_path and not projects:
                return True
            mapped = project_scopes_for_domains([unit.domain] if unit.domain else [], {"scopes": registry})
            if selected and mapped and (len(mapped) != 1 or mapped[0].casefold() not in selected):
                return True
        return False


    @staticmethod
    def _scope_terms_present(text: str, scope: str, config: Mapping[str, Any]) -> bool:
        config = config if "scopes" in config else {"scopes": config}
        terms = [scope, scope.partition(":")[2]]
        node = config.get("scopes", {}).get(scope) if isinstance(config.get("scopes", {}), Mapping) else None
        if isinstance(node, Mapping) and isinstance(node.get("aliases"), list):
            terms.extend(item for item in node["aliases"] if isinstance(item, str))
        folded = text.casefold()
        return any(term.casefold() in folded for term in terms if term)


    def _scope_correction_plan(
        self,
        candidate: Mapping[str, Any],
        turn: InboxTurn,
        config: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Authorize one explicit cross-project correction without guessing.

        The current user turn must name exactly two configured project scopes
        under explicit correction wording. A model-provided target is checked
        against that evidence; when it is omitted, Core may recover exactly one
        same-type, same-topic active memory from the explicitly named old
        scope. Zero or multiple matches stay deferred rather than becoming a
        cross-scope CREATE.
        """

        if candidate.get("worth") is not True or not isinstance(candidate.get("type"), str):
            return None
        new_projects = [
            value for value in candidate.get("scopes", [])
            if isinstance(value, str) and value.startswith("project:")
        ]
        if len(new_projects) != 1:
            return None
        new_scope = new_projects[0]
        user_text = " ".join(
            event.content for event in turn.events
            if event.role == "user" and isinstance(event.content, str)
        ).strip()
        if not user_text or not _SCOPE_CORRECTION_MARKER_RE.search(user_text):
            return None
        config = config if "scopes" in config else {"scopes": config}
        scopes = config.get("scopes", {}) if isinstance(config.get("scopes", {}), Mapping) else {}
        mentioned = [
            scope for scope in scopes
            if isinstance(scope, str)
            and scope.startswith("project:")
            and self._scope_terms_present(user_text, scope, config)
        ]
        mentioned = list(dict.fromkeys(mentioned))
        if len(mentioned) != 2 or all(scope.casefold() != new_scope.casefold() for scope in mentioned):
            return None
        old_scope = next(scope for scope in mentioned if scope.casefold() != new_scope.casefold())

        topic = str(candidate.get("memory") or "")
        removable_terms: list[str] = []
        for scope in (old_scope, new_scope):
            removable_terms.extend((scope, scope.partition(":")[2]))
            node = scopes.get(scope)
            if isinstance(node, Mapping) and isinstance(node.get("aliases"), list):
                removable_terms.extend(item for item in node["aliases"] if isinstance(item, str))
        for term in sorted({item for item in removable_terms if item}, key=len, reverse=True):
            topic = re.sub(re.escape(term), " ", topic, flags=re.IGNORECASE)
        topic = re.sub(r"[\s:：，,；;。.!！?？()（）\[\]【】_-]+", " ", topic).strip()
        if len(normalize_term(topic)) < 4:
            return {
                "target_memory_id": None,
                "old_scope": old_scope,
                "new_scope": new_scope,
                "survivor_memory_id": None,
                "ambiguous": True,
                "unresolved": True,
            }

        try:
            with self.service.vault.lock():
                records = self.service._read_memories_unlocked("knowledge")
        except (OSError, UnicodeError, ValueError, TypeError):
            return None
        eligible_old: list[Memory] = []
        eligible_new: list[Memory] = []
        for record in records:
            memory = record.memory
            if memory.type != candidate.get("type"):
                continue
            if filter_by_scope([memory], [old_scope], config) and candidate_matches_query(memory, topic):
                eligible_old.append(memory)
            if filter_by_scope([memory], [new_scope], config) and candidate_matches_query(memory, topic):
                eligible_new.append(memory)

        target_id = candidate.get("update_memory_id")
        target: Memory | None = None
        if isinstance(target_id, str) and target_id:
            selected = self._active_memory_by_id(target_id)
            if (
                selected is not None
                and selected.type == candidate.get("type")
                and any(memory.memory_id.casefold() == selected.memory_id.casefold() for memory in eligible_old)
            ):
                target = selected
            else:
                return {
                    "target_memory_id": None,
                    "old_scope": old_scope,
                    "new_scope": new_scope,
                    "survivor_memory_id": None,
                    "ambiguous": True,
                    "unresolved": True,
                }
        elif len(eligible_old) == 1:
            target = eligible_old[0]
        else:
            return {
                "target_memory_id": None,
                "old_scope": old_scope,
                "new_scope": new_scope,
                "survivor_memory_id": None,
                "ambiguous": True,
                "unresolved": True,
            }

        survivors = [
            memory for memory in eligible_new
            if memory.memory_id.casefold() != target.memory_id.casefold()
        ]
        return {
            "target_memory_id": target.memory_id,
            "old_scope": old_scope,
            "new_scope": new_scope,
            "survivor_memory_id": survivors[0].memory_id if len(survivors) == 1 else None,
            "ambiguous": len(survivors) > 1,
            "unresolved": False,
        }


    def _scope_correction_request(
        self,
        candidate: Mapping[str, Any],
        turn: InboxTurn,
        plan: Mapping[str, Any],
        *,
        conversation_title: str,
        native_refs: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        survivor_id = plan.get("survivor_memory_id")
        survivor = self._active_memory_by_id(survivor_id)
        target = self._active_memory_by_id(plan.get("target_memory_id"))
        if survivor is None or target is None:
            raise ProcessingError("scope correction target disappeared")
        plan = dict(plan)
        plan.update(expected_history_id=MemoryWriter._history_id(target),
                    expected_target_revision=revision_digest(target),
                    expected_survivor_revision=revision_digest(survivor))
        summary = {
            "title": survivor.title,
            "body": survivor.body,
            "tags": list(survivor.tags),
            "type": survivor.type,
            "scopes": list(survivor.scopes),
            "scope_source": survivor.scope_source,
            "aliases": list(survivor.aliases),
            "keywords": list(survivor.keywords),
            "sources": [],
            "scope_operations": [],
            "status": survivor.status,
            "completed_at": survivor.completed_at,
            "due_date": survivor.due_date,
        }
        return {
            "summary": summary,
            "turn": turn,
            "candidate_id": str(candidate["candidate_id"]),
            "memory_id": survivor.memory_id,
            "event_key": turn.event_keys[0] if turn.event_keys else "",
            "turn_id": "",
            "conversation_title": conversation_title,
            "explicit_remember": False,
            "native_refs": [dict(item) for item in native_refs if isinstance(item, Mapping)],
            "scope_correction": dict(plan),
            "evidence_unit_ids": list(candidate.get("evidence_unit_ids", [])),
        }


    def _target_relation(
        self,
        candidate: Mapping[str, Any],
        *,
        turn: Optional[InboxTurn] = None,
        scope_directory: Optional[list[dict[str, Any]]] = None,
        scope_directory_complete: bool = True,
    ) -> str:
        """Classify a selected target as NOT_RELATED, SAME_USE, or UNKNOWN."""

        target_id = next(
            (
                candidate.get(field)
                for field in ("duplicate_memory_id", "update_memory_id")
                if isinstance(candidate.get(field), str) and candidate.get(field)
            ),
            None,
        )
        memory = candidate.get("memory")
        scopes = candidate.get("scopes")
        if not isinstance(target_id, str) or not isinstance(memory, str) or not isinstance(scopes, list):
            return _TARGET_UNKNOWN
        target = self._active_memory_by_id(target_id)
        if target is None:
            return _TARGET_UNKNOWN
        try:
            config = self.service.vault.config()
        except (OSError, UnicodeError, ValueError, TypeError):
            return _TARGET_UNKNOWN
        source = candidate.get("scope_source")
        if is_project_plan_text(memory) and self._is_adjacent_plan_record(target.title):
            return _TARGET_NOT_RELATED
        if scope_directory is not None and not scope_directory_complete:
            return _TARGET_UNKNOWN
        if not filter_by_scope([target], scopes, config):
            return _TARGET_NOT_RELATED

        scope_terms = self._project_scope_terms(scopes, config)

        if self._model_scope_is_elliptical(candidate, turn, config):
            # An inherited project scope can make an elliptical turn (for
            # example, "this project's task") unambiguous even when the
            # project name is absent from the current events.  The
            # candidate-local scope query still verifies the target's actual
            # membership before writing.
            return _TARGET_SAME_USE

        if source in {"user", "session_context"}:
            # These scopes are authoritative context.  Do not require title
            # wording to repeat the inherited project/entity name.
            return _TARGET_SAME_USE
        if source != "model":
            return _TARGET_UNKNOWN

        # A complete inherited-scope directory is an explicit, bounded target
        # selection.  The active target has already been resolved above; body
        # and topic relevance are resolved by the candidate-local context
        # below.  Detached candidates do not pass this directory and
        # therefore always use full retrieval.
        if scope_directory is not None and scope_directory_complete:
            selected = next(
                (
                    item
                    for item in scope_directory
                    if isinstance(item, Mapping)
                    and isinstance(item.get("memory_id"), str)
                    and item["memory_id"].casefold() == target.memory_id.casefold()
                ),
                None,
            )
            if selected is not None:
                return _TARGET_SAME_USE

        # Global and other non-project scopes do not expose a stable project
        # identity to compare.  The active, scope-matched target is the
        # relevant context; retain the established behavior for those targets.
        if not scope_terms:
            return _TARGET_SAME_USE

        # A plan candidate and a formal plan target represent the same
        # future-use object even when the candidate is phrased as a proposed
        # adjustment and shares few title tokens.  Ambiguous same-scope plans
        # are handled conservatively by _infer_update_target before this
        # classifier is reached; adjacent mail/meeting records were excluded
        # above.
        if is_project_plan_text(memory) and is_project_plan_text(target.title):
            return _TARGET_SAME_USE

        def without_scope_terms(value: str) -> str:
            result = value
            for term in sorted(set(scope_terms), key=len, reverse=True):
                result = re.sub(re.escape(term), "", result, flags=re.IGNORECASE)
            return result.strip()

        topic_query = without_scope_terms(memory)
        topic_title = without_scope_terms(target.title)
        if not topic_query or not topic_title:
            return _TARGET_UNKNOWN
        topic_memory = Memory(
            memory_id=target.memory_id,
            title=topic_query,
            body="",
            type=target.type,
            scopes=target.scopes,
        )
        return _TARGET_SAME_USE if candidate_matches_query(topic_memory, topic_title) else _TARGET_NOT_RELATED


    @staticmethod
    def _project_scope_terms(
        scopes: Any,
        config: Mapping[str, Any],
    ) -> list[str]:
        configured_scopes = config.get("scopes", {}) if isinstance(config, Mapping) else {}
        terms: list[str] = []
        if not isinstance(scopes, list):
            return terms
        for scope in scopes:
            if not isinstance(scope, str) or not scope.startswith("project:"):
                continue
            terms.append(scope.partition(":")[2])
            metadata = configured_scopes.get(scope) if isinstance(configured_scopes, Mapping) else None
            aliases = metadata.get("aliases") if isinstance(metadata, Mapping) else None
            if isinstance(aliases, list):
                terms.extend(alias for alias in aliases if isinstance(alias, str) and alias)
        return terms


    @classmethod
    def _model_scope_is_elliptical(
        cls,
        candidate: Mapping[str, Any],
        turn: Optional[InboxTurn],
        config: Mapping[str, Any],
    ) -> bool:
        if candidate.get("scope_source") != "model" or turn is None:
            return False
        terms = cls._project_scope_terms(candidate.get("scopes"), config)
        if not terms:
            return False
        visible_text = normalize_term(
            " ".join(
                event.content
                for event in turn.events
                if isinstance(event.content, str)
            )
        )
        return not any(normalize_term(term) in visible_text for term in terms)


    @staticmethod
    def _project_scope_keys(scopes: Any) -> set[str]:
        if not isinstance(scopes, list):
            return set()
        return {
            scope.casefold()
            for scope in scopes
            if isinstance(scope, str) and scope.startswith("project:")
        }


    @staticmethod
    def _is_project_plan_title(value: Any) -> bool:
        return is_project_plan_text(value)


    @staticmethod
    def _is_adjacent_plan_record(value: Any) -> bool:
        text = normalize_term(value) if isinstance(value, str) else ""
        return bool(text) and any(
            marker in text
            for marker in (
                "已发送", "发送", "邮件", "附件", "存档", "会议", "启动会", "纪要",
                "sent", "email", "mail", "attachment", "archive", "meeting", "minutes",
            )
        ) and not is_project_plan_text(value)


    def _infer_update_target(
        self,
        candidate: Mapping[str, Any],
        related: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Reuse one unambiguous same-scope active memory before CREATE.

        Deterministic lookup requires a full target-title match, matching type
        and matching Scope. Semantic interpretation remains model-owned.
        """

        result = dict(candidate)
        if (
            not result.get("worth")
            or result.get("duplicate")
            or any(result.get(field) for field in ("duplicate_memory_id", "update_memory_id"))
            or not isinstance(result.get("memory"), str)
        ):
            return result
        project_keys = self._project_scope_keys(result.get("scopes"))
        if len(project_keys) != 1:
            return result

        candidate_type = result.get("type")
        candidate_text = result["memory"]
        same_type_matches: list[Memory] = []
        seen_same_type: set[str] = set()
        for item in related:
            if not isinstance(item, Mapping) or item.get("native") is True:
                continue
            memory_id = item.get("memory_id")
            if (
                not isinstance(memory_id, str)
                or item.get("type") != candidate_type
                or self._project_scope_keys(item.get("scopes")) != project_keys
                or memory_id.casefold() in seen_same_type
            ):
                continue
            try:
                target = Memory.from_mapping(item)
            except (TypeError, ValueError):
                continue
            target_title = normalize_term(target.title)
            candidate_normalized = normalize_term(candidate_text)
            if (
                len(target_title) < 4
                or target_title not in candidate_normalized
                or not candidate_matches_query(target, candidate_text)
            ):
                continue
            seen_same_type.add(memory_id.casefold())
            same_type_matches.append(target)

        # Exact same-type title matching may select a lookup target. It never
        # promotes a fact to project or chooses another type by plan keywords.
        if len(same_type_matches) > 1:
            result["_defer_reason"] = "ambiguous_update_target"
        elif len(same_type_matches) == 1:
            result["update_memory_id"] = same_type_matches[0].memory_id
        return result
