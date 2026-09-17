"""Read-only candidate context, Scope grounding and target resolution."""
from __future__ import annotations
import json
import re
from typing import Any, Iterable, Mapping, Optional
from .inbox import InboxTurn
from .memory_writer import MemoryWriter
from .turn_plan import revision_digest
from .models import Memory
from .native_index import NativeIndexer
from .retrieval import candidate_matches_query, filter_by_scope, normalize_term
from .scope_state import project_scopes_for_domains
from .scope_maintenance import ScopeMaintenanceError, scope_registry_projection
from .process_common import ProcessingError, _RELATED_MAX_BODY_CHARS, _RELATED_MAX_CHARS, _SCOPE_CORRECTION_MARKER_RE, _SCOPE_DIRECTORY_MAX_CHARS, _SCOPE_DIRECTORY_MAX_ITEMS, _SCOPE_DIRECTORY_MAX_TITLE_CHARS, _TARGET_NOT_RELATED, _TARGET_SAME_USE, _TARGET_UNKNOWN, _invoke_native, _merge_related, _native_result, _safe_scope_background, _session_key


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
            if item.get("validity", "valid") != "valid":
                continue
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
            if value.get("validity", "valid") != "valid":
                if isinstance(memory_id, str):
                    by_id.pop(memory_id.casefold(), None)
                continue
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
        native_query: Optional[str] = None,
        return_bound_status: bool = False,
    ) -> Any:
        if isinstance(query, str):
            query_value: str | list[str] = query.strip()
        else:
            query_value = [
                str(item).strip()
                for item in query
                if isinstance(item, str) and item.strip()
            ]
        visible = query_value if isinstance(query_value, str) else " ".join(query_value)
        native_visible = (
            native_query.strip()
            if isinstance(native_query, str)
            else visible
        )
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
                    # Source revision coordination is target-bound. The
                    # previously affected ID must remain visible even if the
                    # edited source now implies another conversational scope;
                    # normal target/scope authorization still runs later.
                    available = self.service._read_memories_unlocked("knowledge")
                by_id = {
                    record.memory.memory_id.casefold(): record
                    for record in available
                    if record.memory.validity == "valid"
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
                if not priority_only and native_visible:
                    indexed_native = NativeIndexer(self.service.vault).search_unlocked(
                        native_visible,
                        target_agent=turn.source,
                        for_context=False,
                        limit=None,
                    )
        native = (
            _invoke_native(getattr(self.service, "native_memory_reader", None), native_visible, scope)
            if native_visible and not priority_only
            else []
        )
        related = self._overlay_related(
            _merge_related(local + indexed_native + native),
            overlay,
            query=visible,
            scope=scope,
        )
        related, bound_complete = self._bound_related_with_status(
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
        if return_bound_status:
            return related, scope, native_refs, scope_fallback, bound_complete
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
    def _bound_related_with_status(
        cls,
        related: Iterable[Mapping[str, Any]],
        *,
        priority_memory_ids: Iterable[str] = (),
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return the legacy bounded projection plus whether it stayed complete.

        B3 needs a proof that local comparison context was not dropped before it
        may treat an absent target as CREATE-safe.  Legacy callers keep using
        ``_bound_related`` and therefore retain the exact list-only API.
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
        used = 2
        complete = True

        def _drop(item: Mapping[str, Any]) -> None:
            """Record one related record that was withheld from the projection.

            ``complete`` answers one question only: can Core still prove that no
            **local** record was withheld, so that an absent UPDATE target and a
            create-safe lookup remain provable?

            Only a withheld record counts.  A body trimmed to fit is still
            projected -- its identity, type and scopes are all present, so it can
            still be targeted and still rules out a duplicate CREATE -- and
            marking that as an unprovable lookup would make ordinary Vault growth
            fail turns.

            Native sources are read-only host files that are never an UPDATE or
            NO_CHANGE target and are only ever referenced through
            ``shadow_native_ids``, so clipping one must not make the whole lookup
            unprovable either.  Treating a long host memory file as a failed
            proof would block every automatic extraction with no way for the user
            to recover except shrinking a file they own.
            """

            nonlocal complete
            if item.get("native") is not True:
                complete = False

        for value in values:
            body = value.get("body")
            if isinstance(body, str) and len(body) > _RELATED_MAX_BODY_CHARS:
                # Trimmed, not withheld: the record keeps its identity.
                value["body"] = body[: _RELATED_MAX_BODY_CHARS - 1].rstrip() + "…"
            size = cls._related_payload_size(value)
            if size < 0:
                _drop(value)
                continue
            additional = size + (1 if selected else 0)
            if used + additional > _RELATED_MAX_CHARS:
                memory_id = value.get("memory_id")
                if not (
                    isinstance(memory_id, str)
                    and memory_id.casefold() in priority
                ):
                    _drop(value)
                    continue
                minimal = {
                    key: value[key]
                    for key in ("memory_id", "title", "body", "type", "scopes", "due_date")
                    if key in value
                }
                size = cls._related_payload_size(minimal)
                if size < 0 or used + size + (1 if selected else 0) > _RELATED_MAX_CHARS:
                    _drop(value)
                    continue
                # A priority target kept in reduced form is still projected, so
                # its identity is not withheld and the proof survives.
                value = minimal
                additional = size + (1 if selected else 0)
            selected.append(value)
            used += additional
        return selected, complete

    @classmethod
    def _bound_related(
        cls,
        related: Iterable[Mapping[str, Any]],
        *,
        priority_memory_ids: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """Keep the legacy model related-memory projection unchanged."""

        selected, _ = cls._bound_related_with_status(
            related,
            priority_memory_ids=priority_memory_ids,
        )
        return selected


    def _valid_records_unlocked(self) -> list[Any]:
        """The legacy planner cannot represent restoration of withdrawn heads."""
        return [record for record in self.service._read_memories_unlocked("knowledge")
                if record.memory.validity == "valid"]

    def _scope_records_unlocked(self, scope: Any) -> tuple[list[Any], bool]:
        """Read and rank active records once for a scoped fallback.

        ``ambiguous`` answers a question about *scope identity*: does this turn
        name more than one specific scope, so that the fallback cannot tell
        which one owns it?  It deliberately does not count how many memories the
        scope holds.  A scope carrying many memories is ordinary, and a session
        naming one project owns its turns unambiguously.

        Counting records instead made the fallback declare itself ambiguous
        whenever a scope was populated, which marked the lookup unprovable and
        deferred every CREATE in a project-scoped session -- while a
        ``global``-only session, where the fallback never runs, was unaffected.
        The catalog ceiling is what bounds how much context is safe to supply;
        ambiguity is not.
        """

        active_records = self._valid_records_unlocked()
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
        requested = [scope] if isinstance(scope, str) else list(scope or [])
        specific = {
            value
            for value in requested
            if isinstance(value, str) and value not in {"", "global", "unscoped"}
        }
        return records, len(specific) > 1


    @classmethod
    def _scope_directory_entry(cls, memory: Memory) -> tuple[dict[str, Any], bool]:
        title = memory.title
        title_truncated = len(title) > _SCOPE_DIRECTORY_MAX_TITLE_CHARS
        if title_truncated:
            title = title[: _SCOPE_DIRECTORY_MAX_TITLE_CHARS - 1].rstrip() + "…"
        entry = {
            "memory_id": memory.memory_id,
            "title": title,
            "type": memory.type,
            "scopes": list(memory.scopes),
        }
        if memory.type == "todo":
            # Gate may use a terminal todo only as a structural witness for
            # already_completed coverage.  Keep the state metadata bounded;
            # the todo body remains outside the directory projection.
            entry["status"] = memory.status if memory.status in {
                "active", "completed", "cancelled"
            } else "active"
            entry["due_date"] = memory.due_date
        return entry, title_truncated


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
        physical_units: Iterable[Any] = (),
    ) -> tuple[
        list[dict[str, Any]],
        Any,
        list[dict[str, str]],
        Optional[tuple[list[Any], bool]],
    ]:
        visible = " ".join(event.content for event in turn.events if isinstance(event.content, str)).strip()
        physical_units = tuple(physical_units or ())
        physical_queries = self._physical_query_texts(physical_units)
        scope = _safe_scope_background(state, explicit_scope)
        scoped_reply_context = self._has_specific_scope(scope) and any(
            getattr(unit, "can_support", False) is True
            and getattr(unit, "origin", None) == "assistant_report"
            for unit in physical_units
        )
        query: str | list[str] = (
            [visible, *physical_queries]
            if physical_queries and self._has_specific_scope(scope)
            else visible
        )
        # The physical projection has already passed capture retention and
        # admission provenance.  It is a local retrieval hint only.  Its
        # complete source text may contain dates/IDs that intentionally fail
        # the ordinary lexical strictness check, so allow the scoped local
        # search to return bounded existing bodies when a final assistant report is
        # present.  The native reader/index continue to receive visible text.
        revision_target_ids = self._revision_target_ids(state, turn)
        return self._related_query(
            turn,
            state,
            query,
            explicit_scope,
            overlay=overlay,
            strict_relevance=not scoped_reply_context,
            priority_memory_ids=revision_target_ids,
            native_query=visible,
        )


    @staticmethod
    def _revision_target_ids(state: Mapping[str, Any], turn: InboxTurn) -> list[str]:
        entries = state.get("revised_turns")
        if not isinstance(entries, list):
            return []
        result: list[str] = []
        for entry in entries:
            if not isinstance(entry, Mapping) or entry.get("turn_key") != turn.turn_key:
                continue
            values = entry.get("memory_ids")
            if not isinstance(values, list):
                continue
            for value in values:
                if isinstance(value, str) and value and value.casefold() not in {
                    item.casefold() for item in result
                }:
                    result.append(value)
        return result


    def _single_pass_scope_correction_context(
        self,
        turn: InboxTurn,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return bounded local context for an explicit two-project correction.

        This is only a retrieval expansion.  It does not decide which project is
        old/new or which memory is the target; the single model call proposes
        that decision and ``_scope_correction_plan`` remains the final Core
        authorization boundary.
        """

        user_text = " ".join(
            event.content for event in turn.events
            if event.role == "user" and isinstance(event.content, str)
        ).strip()
        if not user_text or not _SCOPE_CORRECTION_MARKER_RE.search(user_text):
            return [], True
        try:
            with self.service.vault.lock():
                config = self.service.vault.config()
                scopes = config.get("scopes", {}) if isinstance(config, Mapping) else {}
                if not isinstance(scopes, Mapping):
                    return [], False
                mentioned = [
                    scope for scope in scopes
                    if isinstance(scope, str)
                    and scope.startswith("project:")
                    and self._scope_terms_present(user_text, scope, config)
                ]
                mentioned = list(dict.fromkeys(mentioned))
                if len(mentioned) != 2:
                    return [], True
                records = self._valid_records_unlocked()
                values = [
                    record.memory.to_dict()
                    for record in records
                    if any(filter_by_scope([record.memory], [scope], config) for scope in mentioned)
                ]
        except (OSError, UnicodeError, ValueError, TypeError):
            return [], False
        return self._bound_related_with_status(values)


    def _single_pass_related(
        self,
        turn: InboxTurn,
        state: Mapping[str, Any],
        explicit_scope: Any = None,
        *,
        overlay: Iterable[Mapping[str, Any]] = (),
        physical_units: Iterable[Any] = (),
    ) -> tuple[
        list[dict[str, Any]],
        Any,
        list[dict[str, str]],
        Optional[tuple[list[Any], bool]],
        bool,
    ]:
        """Read B3 comparison context before the single model call.

        ``lookup_complete`` is true only when the bounded related projection is
        complete and a scoped fallback did not discover multiple ambiguous
        records. B3 permits no CREATE/UPDATE/NO_CHANGE when this proof is false.
        """

        visible = " ".join(
            event.content for event in turn.events if isinstance(event.content, str)
        ).strip()
        physical_units = tuple(physical_units or ())
        physical_queries = self._physical_query_texts(physical_units)
        scope = _safe_scope_background(state, explicit_scope)
        scoped_reply_context = self._has_specific_scope(scope) and any(
            getattr(unit, "can_support", False) is True
            and getattr(unit, "origin", None) == "assistant_report"
            for unit in physical_units
        )
        query: str | list[str] = (
            [visible, *physical_queries]
            if physical_queries and self._has_specific_scope(scope)
            else visible
        )
        related, scope_background, native_refs, scope_fallback, bound_complete = self._related_query(
            turn,
            state,
            query,
            explicit_scope,
            overlay=overlay,
            strict_relevance=not scoped_reply_context,
            priority_memory_ids=self._revision_target_ids(state, turn),
            native_query=visible,
            return_bound_status=True,
        )
        # A session scope is conversational background, not a search fence.
        # Include known projects named in this turn and this session's own
        # active memories so a project switch cannot hide maintenance targets.
        with self.service.vault.lock():
            config = self.service.vault.config()
            registry = config.get("scopes", {})
            mentioned = [key for key in registry
                         if isinstance(key, str) and key.startswith("project:")
                         and self._scope_terms_present(visible, key, config)]
            current_records = self._valid_records_unlocked()
            contextual = [record.memory.to_dict() for record in current_records
                          if (any(self._scope_terms_present(visible, key, config)
                                  for key in record.memory.scopes if key.startswith("project:"))
                              or (record.memory.extra.get("source") == turn.source and any(
                                  src.get("session_id") == turn.session_id
                                  for src in record.memory.sources if isinstance(src, Mapping))))]
        if explicit_scope is not None:
            # Explicit caller scope remains a deliberate boundary.
            contextual = [row for row in contextual
                          if filter_by_scope([Memory.from_mapping(row)], explicit_scope, config)]
        elif mentioned:
            scope_background = mentioned
        if contextual:
            contextual = self._overlay_related(contextual, overlay)
            by_id = {row["memory_id"].casefold(): row for row in contextual}
            combined = [row for row in related
                        if row.get("native") is True
                        or str(row.get("memory_id", "")).casefold() not in by_id]
            related, complete = self._bound_related_with_status(
                [*contextual, *combined], priority_memory_ids=by_id)
            bound_complete = bool(bound_complete and complete)
            # Several supplied targets are alternatives for the semantic
            # matcher, not an incomplete lookup merely because they coexist.
            scope_fallback = None
        correction_rows, correction_complete = self._single_pass_scope_correction_context(turn)
        if correction_rows:
            existing_ids = {
                item.get("memory_id").casefold()
                for item in related
                if isinstance(item, Mapping)
                and isinstance(item.get("memory_id"), str)
                and item.get("native") is not True
            }
            combined = list(related)
            combined.extend(
                row for row in correction_rows
                if isinstance(row.get("memory_id"), str)
                and row["memory_id"].casefold() not in existing_ids
            )
            related, combined_complete = self._bound_related_with_status(combined)
            bound_complete = bool(bound_complete and correction_complete and combined_complete)
        elif not correction_complete:
            bound_complete = False
        fallback_ambiguous = bool(
            scope_fallback is not None
            and len(scope_fallback) == 2
            and scope_fallback[1] is True
        )
        lookup_complete = bool(bound_complete and not fallback_ambiguous)
        return related, scope_background, native_refs, scope_fallback, lookup_complete


    @staticmethod
    def _physical_query_texts(units: Iterable[Any]) -> list[str]:
        """Project only planner-approved physical text into local retrieval."""

        result: list[str] = []
        seen: set[str] = set()
        for unit in units:
            if getattr(unit, "can_support", False) is not True:
                continue
            text = getattr(unit, "text", None)
            if not isinstance(text, str):
                continue
            text = text.strip()
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(text)
        return result


    def _active_memory_by_id(self, memory_id: Any) -> Optional[Memory]:
        """Resolve one active memory, including the processor's write overlay."""

        if not isinstance(memory_id, str) or not memory_id:
            return None
        key = memory_id.casefold()
        try:
            with self.service.vault.lock():
                for record in self._valid_records_unlocked():
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

        The cited user evidence must name the new project under explicit
        correction wording; an exact target establishes the previous owner. A model-provided target is checked
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
        bindings = candidate.get("_evidence_bindings", [])
        quotes = [binding.get("quote") for binding in bindings if isinstance(binding, Mapping)]
        cited_user_text = [quote for quote in quotes if isinstance(quote, str) and quote and quote in user_text]
        if cited_user_text:
            user_text = " ".join(cited_user_text)
        if not user_text or not _SCOPE_CORRECTION_MARKER_RE.search(user_text):
            return None
        config = config if "scopes" in config else {"scopes": config}
        scopes = dict(config.get("scopes", {})) if isinstance(config.get("scopes", {}), Mapping) else {}
        # Explicit user corrections can introduce a previously unseen owner;
        # local matching still requires that new name in the cited evidence.
        scopes.setdefault(new_scope, {})
        mentioned = [
            scope for scope in scopes
            if isinstance(scope, str)
            and scope.startswith("project:")
            and self._scope_terms_present(user_text, scope, config)
        ]
        mentioned = list(dict.fromkeys(mentioned))
        selected = self._active_memory_by_id(candidate.get("update_memory_id"))
        if selected is not None and len(selected.scopes) == 1:
            # Other projects in the same user turn do not invalidate an exact
            # correction of this selected target. The new owner must occur
            # in the cited user correction; the old owner is the target's
            # existing metadata, not an inference from another sentence.
            old_scope = selected.scopes[0]
            if new_scope not in mentioned or old_scope == new_scope:
                return None
        else:
            if len(mentioned) != 2 or new_scope not in mentioned:
                return None
            old_scope = next(scope for scope in mentioned if scope != new_scope)

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
                records = self._valid_records_unlocked()
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
        """Validate only structural properties of a model-selected target.

        The model owns semantic same-future-use judgment. Core verifies that
        the selected target still exists and remains visible in the candidate's
        selected Scope. Type/revision checks are enforced by the planner and
        commit boundary. No candidate/title/body token matching participates in
        target authorization.
        """

        del turn, scope_directory, scope_directory_complete
        target_id = next(
            (
                candidate.get(field)
                for field in ("duplicate_memory_id", "update_memory_id")
                if isinstance(candidate.get(field), str) and candidate.get(field)
            ),
            None,
        )
        scopes = candidate.get("scopes")
        if not isinstance(target_id, str) or not isinstance(scopes, list):
            return _TARGET_UNKNOWN
        target = self._active_memory_by_id(target_id)
        if target is None:
            return _TARGET_UNKNOWN
        try:
            config = self.service.vault.config()
        except (OSError, UnicodeError, ValueError, TypeError):
            return _TARGET_UNKNOWN
        if not filter_by_scope([target], scopes, config):
            return _TARGET_NOT_RELATED
        return _TARGET_SAME_USE




    @staticmethod
    def _project_scope_keys(scopes: Any) -> set[str]:
        if not isinstance(scopes, list):
            return set()
        return {
            scope.casefold()
            for scope in scopes
            if isinstance(scope, str) and scope.startswith("project:")
        }


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
